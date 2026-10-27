"""CLI gateway: run commands on a lab machine over SSH and return the output.

A ContextQA API step POSTs a JSON request here; the gateway SSHes into the
target, runs the commands, and answers with each command's exit code and
output so later test steps can assert on it or chain it into UI steps.

This module is transport-agnostic. lambda_function.py wraps it for AWS Lambda,
server.py wraps it as a plain HTTP server (Cloud Run, a VM, a lab box inside
your firewall). Both share validate_request() and run().

Two modes:
  exec   One SSH exec channel per command. Real exit codes. For Linux hosts.
  shell  One interactive PTY. Optional in-band login ("Please login:"), then
         each command is sent and its output read up to the next prompt.
         For network-device CLIs that do not support exec channels. There is
         no exit code; a command fails when its output matches error_patterns.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

DEFAULT_PORT = 22
DEFAULT_TIMEOUT_SECONDS = 20
MAX_TIMEOUT_SECONDS = 60
MAX_COMMANDS = 20
MAX_COMMAND_LENGTH = 1024
MAX_OUTPUT_BYTES = 64 * 1024
SHELL_READ_CHUNK_BYTES = 4096
SHELL_POLL_SECONDS = 0.05
DEFAULT_PROMPT = r"[#>$]\s*$"
DEFAULT_ERROR_PATTERNS = (r"(?im)^\s*(error|invalid|unknown command)\b",)
MODES = ("exec", "shell")


class RequestError(ValueError):
    """The request is malformed or not allowed. Maps to HTTP 400/403."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class SessionError(RuntimeError):
    """Could not reach, authenticate to, or drive the target. Maps to HTTP 502."""


@dataclass(frozen=True)
class Policy:
    """Gateway-side guardrails, set by whoever hosts the gateway, never by callers."""

    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    allowed_hostnames: frozenset[str]
    command_patterns: tuple[re.Pattern[str], ...] = ()

    @classmethod
    def from_strings(cls, allowed_targets: str, command_patterns: str = "") -> "Policy":
        networks = []
        hostnames = set()
        for item in _split_csv(allowed_targets):
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                hostnames.add(item.lower())
        if not networks and not hostnames:
            raise ValueError("ALLOWED_TARGETS must name at least one host or CIDR")
        patterns = tuple(re.compile(p) for p in _split_csv(command_patterns, sep="\n"))
        return cls(tuple(networks), frozenset(hostnames), patterns)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Policy":
        """ALLOWED_TARGETS, plus command regexes from ALLOWED_COMMANDS and/or
        ALLOWED_COMMANDS_FILE (one regex per line; blank and # lines skipped).
        The file form exists because env files (docker --env-file, systemd
        EnvironmentFile) cannot hold multi-line values."""
        commands = env.get("ALLOWED_COMMANDS", "")
        path = env.get("ALLOWED_COMMANDS_FILE")
        if path:
            with open(path, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
            commands = "\n".join([commands, *lines])
        return cls.from_strings(env["ALLOWED_TARGETS"], commands)

    def check_target(self, host: str) -> None:
        if host.lower() in self.allowed_hostnames:
            return
        for address in _resolve(host):
            if not any(address in network for network in self.allowed_networks):
                raise RequestError(f"target {host} is not in the gateway allowlist", status=403)

    def check_command(self, command: str) -> None:
        if not self.command_patterns:
            return
        if not any(p.search(command) for p in self.command_patterns):
            raise RequestError(f"command not allowed by gateway policy: {command!r}", status=403)


@dataclass(frozen=True)
class Login:
    """In-band login some device CLIs ask for after the SSH session opens."""

    username: str
    password: str
    username_prompt: str = r"(?i)(please login|login|username)\s*:\s*$"
    password_prompt: str = r"(?i)password\s*:\s*$"


@dataclass(frozen=True)
class Request:
    host: str
    username: str
    commands: tuple[str, ...]
    port: int = DEFAULT_PORT
    password: str | None = None
    private_key: str | None = None
    mode: str = "exec"
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    prompt: str = DEFAULT_PROMPT
    login: Login | None = None
    error_patterns: tuple[str, ...] = DEFAULT_ERROR_PATTERNS


@dataclass
class CommandResult:
    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
        }


@dataclass
class Response:
    host: str
    mode: str
    results: list[CommandResult] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(r.ok for r in self.results)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "host": self.host,
            "mode": self.mode,
            "results": [r.to_dict() for r in self.results],
            # Convenience for single-command steps: assert on these directly.
            "exit_code": self.results[-1].exit_code if self.results else None,
            "output": self.results[-1].stdout if self.results else "",
            "error": self.error,
        }


class Channel(Protocol):
    def send(self, data: bytes) -> int: ...
    def recv_ready(self) -> bool: ...
    def recv(self, size: int) -> bytes: ...
    def close(self) -> None: ...


class Session(Protocol):
    """The slice of an SSH client the gateway uses. paramiko satisfies it."""

    def exec(self, command: str, timeout: float) -> tuple[int, bytes, bytes]: ...
    def open_shell(self) -> Channel: ...
    def close(self) -> None: ...


SessionFactory = Callable[[Request], Session]


def validate_request(body: object, policy: Policy) -> Request:
    if not isinstance(body, dict):
        raise RequestError("request body must be a JSON object")

    host = _required_str(body, "host")
    username = _required_str(body, "username")
    password = _optional_str(body, "password")
    private_key = _optional_str(body, "private_key")
    if not password and not private_key:
        raise RequestError("one of password or private_key is required")

    mode = body.get("mode", "exec")
    if mode not in MODES:
        raise RequestError(f"mode must be one of {', '.join(MODES)}")

    commands = body.get("commands")
    if isinstance(body.get("command"), str):
        commands = [body["command"]]
    if not isinstance(commands, list) or not commands:
        raise RequestError("commands must be a non-empty list of strings")
    if len(commands) > MAX_COMMANDS:
        raise RequestError(f"at most {MAX_COMMANDS} commands per request")
    for command in commands:
        if not isinstance(command, str) or not command.strip():
            raise RequestError("every command must be a non-empty string")
        if len(command) > MAX_COMMAND_LENGTH or "\n" in command or "\r" in command:
            raise RequestError("commands must be single lines under 1024 characters")
        policy.check_command(command)

    port = body.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise RequestError("port must be an integer between 1 and 65535")

    timeout = body.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise RequestError("timeout_seconds must be a positive number")

    policy.check_target(host)

    return Request(
        host=host,
        port=port,
        username=username,
        password=password,
        private_key=private_key,
        commands=tuple(commands),
        mode=mode,
        timeout=min(float(timeout), MAX_TIMEOUT_SECONDS),
        prompt=_regex(body, "prompt", DEFAULT_PROMPT),
        login=_login(body.get("login")),
        error_patterns=tuple(_regex_list(body, "error_patterns", DEFAULT_ERROR_PATTERNS)),
    )


def run(request: Request, connect: SessionFactory) -> Response:
    response = Response(host=request.host, mode=request.mode)
    try:
        session = connect(request)
    except SessionError as exc:
        response.error = str(exc)
        return response
    try:
        if request.mode == "exec":
            response.results = _run_exec(session, request)
        else:
            response.results = _run_shell(session, request)
    except SessionError as exc:
        response.error = str(exc)
    finally:
        session.close()
    return response


def _run_exec(session: Session, request: Request) -> list[CommandResult]:
    results = []
    for command in request.commands:
        started = time.monotonic()
        exit_code, stdout, stderr = session.exec(command, request.timeout)
        out, out_cut = _decode(stdout)
        err, err_cut = _decode(stderr)
        results.append(
            CommandResult(
                command=command,
                ok=exit_code == 0,
                exit_code=exit_code,
                stdout=out,
                stderr=err,
                duration_ms=_elapsed_ms(started),
                truncated=out_cut or err_cut,
            )
        )
    return results


def _run_shell(session: Session, request: Request) -> list[CommandResult]:
    channel = session.open_shell()
    reader = _PromptReader(channel, request.timeout)
    prompt = re.compile(request.prompt, re.MULTILINE)
    try:
        if request.login:
            reader.read_until(re.compile(request.login.username_prompt, re.MULTILINE))
            channel.send(request.login.username.encode() + b"\n")
            reader.read_until(re.compile(request.login.password_prompt, re.MULTILINE))
            channel.send(request.login.password.encode() + b"\n")
        reader.read_until(prompt)

        errors = [re.compile(p) for p in request.error_patterns]
        results = []
        for command in request.commands:
            started = time.monotonic()
            channel.send(command.encode() + b"\n")
            raw = reader.read_until(prompt)
            output, truncated = _decode(raw)
            output = _strip_echo_and_prompt(output, command, prompt)
            results.append(
                CommandResult(
                    command=command,
                    ok=not any(e.search(output) for e in errors),
                    exit_code=None,
                    stdout=output,
                    duration_ms=_elapsed_ms(started),
                    truncated=truncated,
                )
            )
        return results
    finally:
        channel.close()


class _PromptReader:
    """Reads a shell channel until a regex matches the tail of what arrived."""

    def __init__(self, channel: Channel, timeout: float):
        self._channel = channel
        self._timeout = timeout

    def read_until(self, pattern: re.Pattern[str]) -> bytes:
        deadline = time.monotonic() + self._timeout
        buffer = b""
        while time.monotonic() < deadline:
            if self._channel.recv_ready():
                chunk = self._channel.recv(SHELL_READ_CHUNK_BYTES)
                if not chunk:
                    raise SessionError("remote shell closed the connection")
                buffer += chunk
                if pattern.search(buffer.decode("utf-8", "replace")):
                    return buffer
                continue
            time.sleep(SHELL_POLL_SECONDS)
        tail = buffer.decode("utf-8", "replace")[-200:]
        raise SessionError(f"timed out waiting for {pattern.pattern!r}; last output: {tail!r}")


def connect_paramiko(request: Request) -> Session:
    """Open an SSH session with paramiko. Imported lazily so tests need no deps."""
    import io

    import paramiko

    client = paramiko.SSHClient()
    # Lab devices are reflashed often, so their host keys change. Accept and log
    # rather than pin; the target allowlist is what bounds where we connect.
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    pkey = None
    if request.private_key:
        pkey = _load_private_key(paramiko, io.StringIO(request.private_key))
    try:
        client.connect(
            request.host,
            port=request.port,
            username=request.username,
            password=request.password,
            pkey=pkey,
            timeout=request.timeout,
            banner_timeout=request.timeout,
            auth_timeout=request.timeout,
            look_for_keys=False,
            allow_agent=False,
        )
    except (paramiko.SSHException, OSError, socket.timeout) as exc:
        client.close()
        raise SessionError(f"ssh to {request.host}:{request.port} failed: {exc}") from exc
    return _ParamikoSession(client)


class _ParamikoSession:
    def __init__(self, client):
        self._client = client

    def exec(self, command: str, timeout: float) -> tuple[int, bytes, bytes]:
        import paramiko

        try:
            _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            out = stdout.read(MAX_OUTPUT_BYTES + 1)
            err = stderr.read(MAX_OUTPUT_BYTES + 1)
            return stdout.channel.recv_exit_status(), out, err
        except (paramiko.SSHException, OSError, socket.timeout) as exc:
            raise SessionError(f"running {command!r} failed: {exc}") from exc

    def open_shell(self) -> Channel:
        return self._client.invoke_shell(width=200, height=50)

    def close(self) -> None:
        self._client.close()


def _load_private_key(paramiko, stream):
    for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        stream.seek(0)
        try:
            return key_class.from_private_key(stream)
        except paramiko.SSHException:
            continue
    raise RequestError("private_key is not a supported unencrypted OpenSSH/PEM key")


def _strip_echo_and_prompt(output: str, command: str, prompt: re.Pattern[str]) -> str:
    lines = output.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].strip() == command.strip():
        lines = lines[1:]
    if lines and prompt.search(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _decode(raw: bytes) -> tuple[str, bool]:
    truncated = len(raw) > MAX_OUTPUT_BYTES
    return raw[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), truncated


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise RequestError(f"cannot resolve target {host}") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _split_csv(value: str, sep: str = ",") -> list[str]:
    return [item.strip() for item in (value or "").split(sep) if item.strip()]


def _required_str(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RequestError(f"{key} is required")
    return value.strip()


def _optional_str(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(f"{key} must be a string")
    return value or None


def _regex(body: dict, key: str, default: str) -> str:
    value = body.get(key, default)
    if not isinstance(value, str):
        raise RequestError(f"{key} must be a regex string")
    try:
        re.compile(value)
    except re.error as exc:
        raise RequestError(f"{key} is not a valid regex: {exc}") from exc
    return value


def _regex_list(body: dict, key: str, default: tuple[str, ...]) -> list[str]:
    values = body.get(key, list(default))
    if not isinstance(values, list):
        raise RequestError(f"{key} must be a list of regex strings")
    return [_regex({key: v}, key, "") for v in values]


def _login(value: object) -> Login | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RequestError("login must be an object")
    overrides = {
        k: _regex(value, k, getattr(Login, k))
        for k in ("username_prompt", "password_prompt")
        if k in value
    }
    return Login(
        username=_required_str(value, "username"),
        password=_required_str(value, "password"),
        **overrides,
    )
