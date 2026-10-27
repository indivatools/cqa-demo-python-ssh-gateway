import hashlib
import json
import os
import select
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

TOKEN = "test-token"
os.environ["GATEWAY_TOKEN_SHA256"] = hashlib.sha256(TOKEN.encode()).hexdigest()
os.environ["ALLOWED_TARGETS"] = "10.20.0.0/24"
os.environ["ALLOWED_COMMANDS"] = "^demo-cli \n^(get|set|reset) "

import gateway  # noqa: E402
import lambda_function  # noqa: E402
from auth import token_matches  # noqa: E402

DEMO_CLI = os.path.join(REPO_ROOT, "demo", "device", "demo_cli.py")
POLICY = gateway.Policy.from_strings("10.20.0.0/24")
DEVICE = "10.20.0.10"
DEVICE_PROMPT = r"demo-ap> $"
PASSWORD = "s3cret"


def exec_body(**overrides):
    body = {"host": DEVICE, "username": "labops", "password": PASSWORD,
            "commands": ["demo-cli get version"]}
    body.update(overrides)
    return body


class FakeExecSession:
    def __init__(self, replies):
        self.replies = replies
        self.ran = []
        self.closed = False

    def exec(self, command, timeout):
        self.ran.append(command)
        return self.replies[command]

    def open_shell(self):
        raise AssertionError("exec mode must not open a shell")

    def close(self):
        self.closed = True


class PipeChannel:
    """Drives the real demo_cli.py over pipes, standing in for an SSH shell.

    Pipes rather than a PTY so this runs in sandboxes and CI without TTYs;
    stderr is merged because getpass prompts there when stdin is not a TTY.
    """

    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", DEMO_CLI, "--interactive", "--login"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env,
        )
        self.out = self.proc.stdout.fileno()

    def send(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
        return len(data)

    def recv_ready(self):
        return bool(select.select([self.out], [], [], 0)[0])

    def recv(self, size):
        return os.read(self.out, size)

    def close(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


class PipeSession:
    def __init__(self, env):
        self.env = env

    def exec(self, command, timeout):
        raise AssertionError("shell mode must not use exec")

    def open_shell(self):
        return PipeChannel(self.env)

    def close(self):
        pass


class ValidateRequestTest(unittest.TestCase):
    def test_minimal_exec_request(self):
        request = gateway.validate_request(exec_body(), POLICY)
        self.assertEqual(request.mode, "exec")
        self.assertEqual(request.commands, ("demo-cli get version",))
        self.assertEqual(request.port, 22)

    def test_single_command_shorthand(self):
        body = exec_body(command="demo-cli get controller")
        del body["commands"]
        self.assertEqual(gateway.validate_request(body, POLICY).commands, ("demo-cli get controller",))

    def test_rejects_target_outside_allowlist(self):
        with self.assertRaises(gateway.RequestError) as ctx:
            gateway.validate_request(exec_body(host="169.254.169.254"), POLICY)
        self.assertEqual(ctx.exception.status, 403)

    def test_rejects_command_outside_allowlist(self):
        policy = gateway.Policy.from_strings("10.20.0.0/24", "^demo-cli ")
        with self.assertRaises(gateway.RequestError) as ctx:
            gateway.validate_request(exec_body(commands=["rm -rf /"]), policy)
        self.assertEqual(ctx.exception.status, 403)

    def test_rejects_multiline_command(self):
        with self.assertRaises(gateway.RequestError):
            gateway.validate_request(exec_body(commands=["demo-cli get version\nreboot"]), POLICY)

    def test_requires_a_credential(self):
        body = exec_body()
        del body["password"]
        with self.assertRaises(gateway.RequestError):
            gateway.validate_request(body, POLICY)

    def test_caps_timeout(self):
        request = gateway.validate_request(exec_body(timeout_seconds=600), POLICY)
        self.assertEqual(request.timeout, gateway.MAX_TIMEOUT_SECONDS)

    def test_rejects_bad_regex(self):
        with self.assertRaises(gateway.RequestError):
            gateway.validate_request(exec_body(mode="shell", prompt="("), POLICY)

    def test_policy_needs_a_target(self):
        with self.assertRaises(ValueError):
            gateway.Policy.from_strings("")


class ExecModeTest(unittest.TestCase):
    def test_reports_exit_codes_and_output(self):
        session = FakeExecSession({
            "demo-cli set controller qa.example.net": (0, b"OK\n", b""),
            "demo-cli get bogus": (1, b"", b"ERROR: unknown command\n"),
        })
        request = gateway.validate_request(
            exec_body(commands=list(session.replies)), POLICY)
        response = gateway.run(request, lambda _: session).to_dict()

        self.assertFalse(response["ok"])
        self.assertEqual([r["exit_code"] for r in response["results"]], [0, 1])
        self.assertEqual(response["results"][0]["stdout"], "OK\n")
        self.assertIn("unknown command", response["results"][1]["stderr"])
        self.assertEqual(response["exit_code"], 1)
        self.assertTrue(session.closed)

    def test_truncates_large_output(self):
        big = b"x" * (gateway.MAX_OUTPUT_BYTES + 10)
        session = FakeExecSession({"demo-cli get version": (0, big, b"")})
        request = gateway.validate_request(exec_body(), POLICY)
        result = gateway.run(request, lambda _: session).results[0]
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout), gateway.MAX_OUTPUT_BYTES)

    def test_connect_failure_is_an_error_not_a_crash(self):
        def refuse(_):
            raise gateway.SessionError("ssh to 10.20.0.10:22 failed: refused")

        request = gateway.validate_request(exec_body(), POLICY)
        response = gateway.run(request, refuse).to_dict()
        self.assertFalse(response["ok"])
        self.assertIn("refused", response["error"])


class ShellModeAgainstDemoCliTest(unittest.TestCase):
    """End-to-end shell mode: in-band login, prompts, echo stripping, errors."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = os.path.join(self.tmp.name, "config.json")
        with open(config, "w") as f:
            json.dump({"login_user": "ap",
                       "login_password_sha256": hashlib.sha256(PASSWORD.encode()).hexdigest()}, f)
        self.env = dict(os.environ, DEMO_AP_CONFIG=config,
                        DEMO_AP_STATE=os.path.join(self.tmp.name, "state.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def shell_request(self, commands, password=PASSWORD):
        return gateway.validate_request({
            "host": DEVICE, "username": "ap", "password": PASSWORD, "mode": "shell",
            "prompt": DEVICE_PROMPT, "timeout_seconds": 5, "commands": commands,
            "login": {"username": "ap", "password": password},
        }, POLICY)

    def test_prod_to_qa_flow(self):
        request = self.shell_request([
            "get version",
            "get controller",
            "set controller controller.qa.example.net",
            "get controller",
        ])
        response = gateway.run(request, lambda _: PipeSession(self.env)).to_dict()

        self.assertTrue(response["ok"], response)
        outputs = [r["stdout"] for r in response["results"]]
        self.assertIn("firmware: 1.4.2.118", outputs[0])
        self.assertEqual(outputs[1], "controller: controller.prod.example.net")
        self.assertEqual(outputs[2], "OK")
        self.assertEqual(response["output"], "controller: controller.qa.example.net")

    def test_device_error_marks_command_failed(self):
        request = self.shell_request(["get bogus"])
        response = gateway.run(request, lambda _: PipeSession(self.env)).to_dict()
        self.assertFalse(response["ok"])
        self.assertIn("ERROR", response["output"])
        self.assertIsNone(response["exit_code"])

    def test_wrong_login_times_out_with_context(self):
        request = self.shell_request(["get version"], password="wrong")
        request = gateway.Request(**{**request.__dict__, "timeout": 2})
        response = gateway.run(request, lambda _: PipeSession(self.env)).to_dict()
        self.assertFalse(response["ok"])
        self.assertIn("Login incorrect", response["error"])


class LambdaHandlerTest(unittest.TestCase):
    def event(self, body, token=TOKEN, method="POST"):
        return {
            "requestContext": {"http": {"method": method}},
            "headers": {"Authorization": f"Bearer {token}"},
            "body": json.dumps(body),
        }

    def test_rejects_bad_token(self):
        reply = lambda_function.handler(self.event(exec_body(), token="nope"), None)
        self.assertEqual(reply["statusCode"], 401)

    def test_rejects_get(self):
        reply = lambda_function.handler(self.event(exec_body(), method="GET"), None)
        self.assertEqual(reply["statusCode"], 405)

    def test_rejects_direct_invoke_without_url_context(self):
        # lambda.tf's unconditioned InvokeFunction grant relies on this.
        reply = lambda_function.handler(exec_body(), None)
        self.assertEqual(reply["statusCode"], 405)

    def test_rejects_disallowed_command(self):
        body = exec_body(commands=["cat /etc/shadow"])
        reply = lambda_function.handler(self.event(body), None)
        self.assertEqual(reply["statusCode"], 403)

    def test_rejects_invalid_json(self):
        event = self.event({})
        event["body"] = "{nope"
        self.assertEqual(lambda_function.handler(event, None)["statusCode"], 400)

    def test_runs_and_returns_json(self):
        session = FakeExecSession({"demo-cli get version": (0, b"firmware: 1\n", b"")})
        original = lambda_function.connect_paramiko
        lambda_function.connect_paramiko = lambda _: session
        try:
            reply = lambda_function.handler(self.event(exec_body()), None)
        finally:
            lambda_function.connect_paramiko = original
        self.assertEqual(reply["statusCode"], 200)
        self.assertEqual(json.loads(reply["body"])["output"], "firmware: 1\n")


class AuthTest(unittest.TestCase):
    def test_token_matches(self):
        digest = hashlib.sha256(b"abc").hexdigest()
        self.assertTrue(token_matches("Bearer abc", digest))
        self.assertTrue(token_matches("bearer abc", digest))
        self.assertFalse(token_matches("Bearer abd", digest))
        self.assertFalse(token_matches("abc", digest))
        self.assertFalse(token_matches(None, digest))
        self.assertFalse(token_matches("Bearer abc", ""))


if __name__ == "__main__":
    unittest.main()
