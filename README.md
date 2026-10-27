# SSH CLI gateway for ContextQA (demo and reference implementation)

Run CLI commands on lab machines and network devices over SSH from a ContextQA
test, and use their output in the rest of the test.

ContextQA tests drive browsers and call APIs. Many end-to-end flows also need a
step on a device: point an access point at the QA controller, check a firmware
version, change a setting and read it back. This repo is a small HTTP service
that turns "SSH in and run these commands" into an API call. A ContextQA API
step calls it, and later steps use the result.

```
 ContextQA test
 ├─ UI step   log into the web app
 ├─ API step  POST /  ───HTTPS + bearer───▶  ssh-gateway  ───SSH───▶  device / lab host
 │            ◀──── { ok, exit_code, output, results[] } ──────────┘
 └─ UI step   use ${ssh.body.output} in the next steps
```

> **Demo and knowledge-transfer code.** It is deliberately small (about 700
> lines, one dependency: paramiko) so you can read all of it, host it yourself
> and adapt it. Review it before pointing it at production equipment.

## Contents

- [What it does](#what-it-does)
- [Try it locally](#try-it-locally)
- [API contract](#api-contract)
- [Using it from a ContextQA test](#using-it-from-a-contextqa-test)
- [Deploying it](#deploying-it): AWS Lambda, GCP Cloud Run, VM or on-prem, containers
- [Security model](#security-model)
- [Repo layout](#repo-layout), [Tests](#tests), [FAQ](#faq)

## What it does

- Accepts a `POST` with a JSON body naming a host, credentials and commands.
- Connects over SSH and runs the commands in one of two modes:
  - **`exec`**: one SSH exec channel per command, with real exit codes. Use it
    for Linux hosts and anything with a normal shell.
  - **`shell`**: one interactive PTY session. It waits for the device prompt,
    optionally answers an in-band login (`Please login:` then `Password:`, as
    many access points and switches do), sends each command and captures the
    output up to the next prompt. Use it for device CLIs that don't support exec.
- Returns per-command results as JSON, plus `output` and `exit_code` of the
  last command so a single assertion is enough for simple steps.
- Enforces guardrails configured on the gateway, which callers cannot
  override: a bearer token, a target allowlist, an optional command allowlist,
  and limits on time and size.

The core (`src/gateway.py`) has two entry points:

| Entry point | Use it for |
|---|---|
| `src/lambda_function.py` | AWS Lambda behind a Function URL |
| `src/server.py` | Everything else: Cloud Run, a VM, a container, a lab box, your laptop |

## Try it locally

Requires Docker. This starts the gateway and a fake access point (an SSH server
with a small device CLI) on a private Docker network:

```sh
docker compose up --build
```

In another terminal:

```sh
# exec mode: Linux-style login, real exit codes
curl -s localhost:8080 -H 'Authorization: Bearer demo-token' -H 'content-type: application/json' -d '{
  "host": "device", "username": "labops", "password": "demo-password",
  "commands": ["demo-cli get version", "demo-cli get controller"]
}'

# shell mode: device CLI with an in-band login; re-point the AP from prod to QA
curl -s localhost:8080 -H 'Authorization: Bearer demo-token' -H 'content-type: application/json' -d '{
  "host": "device", "username": "ap", "password": "demo-password", "mode": "shell",
  "prompt": "demo-ap> $",
  "login": {"username": "ap", "password": "demo-password"},
  "commands": ["get controller", "set controller controller.qa.example.net", "get controller"]
}'

# guardrails: wrong token returns 401, a command outside the allowlist returns 403
curl -s localhost:8080 -H 'Authorization: Bearer nope' -d '{}'
curl -s localhost:8080 -H 'Authorization: Bearer demo-token' -d '{"host":"device","username":"labops","password":"demo-password","commands":["cat /etc/shadow"]}'
```

The fake device understands `get version`, `get controller`,
`set controller <host>`, `reset controller` and `help`. The `labops` login is
a normal shell, so exec mode runs `demo-cli <command>`. The `ap` login gets
only the device CLI and refuses exec, as real hardware often does; shell mode
exists for that case.

`demo-token` and `demo-password` are fixed demo values in `docker-compose.yml`.
Do not reuse them anywhere else.

## API contract

`POST /` with `Authorization: Bearer <token>` and a JSON body:

| Field | Required | Notes |
|---|---|---|
| `host` | yes | IP or hostname. Must be allowed by the gateway's `ALLOWED_TARGETS`. |
| `username` | yes | SSH username. |
| `password` or `private_key` | one of | `private_key` is the text of an unencrypted OpenSSH/PEM key (Ed25519, ECDSA or RSA). |
| `commands` (list) or `command` (string) | yes | Up to 20, single-line. Each must match `ALLOWED_COMMANDS` if that is set. |
| `mode` | no | `exec` (default) or `shell`. |
| `port` | no | Default `22`. |
| `timeout_seconds` | no | Applies to each wait. Default `20`, capped at `60`. |
| `prompt` | shell | Regex for the device prompt, such as `demo-ap> $` or `[#>]\s*$`. Default `[#>$]\s*$`. |
| `login` | shell | `{username, password, username_prompt?, password_prompt?}` for CLIs that ask for credentials again after SSH connects. The prompts are regexes. |
| `error_patterns` | shell | Regexes that mark a command's output as failed. The default matches lines starting with `error`, `invalid` or `unknown command`. |

Response:

```json
{
  "ok": true,
  "host": "device",
  "mode": "exec",
  "results": [
    {"command": "demo-cli get controller", "ok": true, "exit_code": 0,
     "stdout": "controller: controller.qa.example.net\n", "stderr": "",
     "duration_ms": 41, "truncated": false}
  ],
  "exit_code": 0,
  "output": "controller: controller.qa.example.net\n",
  "error": null
}
```

| Status | Meaning |
|---|---|
| `200` | The gateway ran the request. Check `ok`: a command can still have failed (non-zero exit code, or an `error_patterns` match in shell mode). |
| `400` | Malformed request. `error` names the field and the problem. |
| `401` | Missing or wrong bearer token. |
| `403` | The target or a command is not allowed by the gateway's policy. |
| `502` | The host could not be reached or authenticated against, or the shell timed out. `error` includes the last output received. |

In shell mode `exit_code` is `null`, because a device CLI does not report one;
`ok` is derived from `error_patterns`.

## Using it from a ContextQA test

1. Add environment variables to the ContextQA environment the test runs with,
   so secrets stay out of the steps:
   - `gateway_url` (text) and `gateway_token` (password)
   - `device_host` (text) and `device_password` (password)
2. Add a REST API step (`POST`) to the browser test case:
   - URL: the gateway URL
   - Header: `Authorization: Bearer *|gateway_token|`
   - JSON body:
     ```json
     {"host": "*|device_host|", "username": "labops", "password": "*|device_password|",
      "commands": ["demo-cli set controller controller.qa.example.net", "demo-cli get controller"]}
     ```
   - Store the response in a variable, for example `ssh`.
   - Assertions: status `200`, `ssh.body.ok` equals `true`, `ssh.body.exit_code` equals `0`.
3. Use the result in later UI steps as test data: `${ssh.body.output}`,
   `${ssh.body.exit_code}` or `${ssh.body.ok}`. For example, type it into a
   form or compare it with what the UI shows.

A complete example test logs into a web app, calls the gateway to re-point a
fake access point to the QA controller, and then writes the command, exit code
and output into a table in the same web app: UI, then API, then UI, in one test
case.

You do not need a public URL for the gateway. Run it on a machine inside your
lab network and add a route for it to your ContextQA tunnel; the API step then
calls the tunnelled URL. See [`deploy/vm`](deploy/vm/README.md).

## Deploying it

Choose where the gateway runs by where your devices are reachable from. The
gateway must be able to open TCP port 22 (or your SSH port) to them.

| Where | Guide | When |
|---|---|---|
| Lab machine, VM or on-prem, with the ContextQA tunnel | [`deploy/vm`](deploy/vm/README.md) | Recommended for private labs. No public endpoint and no firewall changes. |
| AWS Lambda with a Function URL | [`deploy/aws-lambda`](deploy/aws-lambda/README.md) | Devices reachable from an AWS VPC. No servers to manage. |
| GCP Cloud Run | [`deploy/gcp-cloud-run`](deploy/gcp-cloud-run/README.md) | Devices reachable from a GCP VPC through Direct VPC egress. |
| Any container platform | `Dockerfile` | ECS/Fargate, App Runner, Azure Container Apps, Kubernetes, `docker run`. Set `HOST=0.0.0.0` and `PORT`. |

Configuration, the same for every entry point:

| Variable | Required | Meaning |
|---|---|---|
| `GATEWAY_TOKEN_SHA256` | yes | Hex SHA-256 of the bearer token. To generate: `TOKEN=$(openssl rand -hex 24); printf %s "$TOKEN" \| sha256sum` (macOS: `shasum -a 256`). |
| `ALLOWED_TARGETS` | yes | Comma-separated CIDRs and/or hostnames the gateway may connect to, for example `10.20.0.0/24,lab-switch-01`. |
| `ALLOWED_COMMANDS` | no | Regexes, one per line; every command must match at least one. If neither this nor `ALLOWED_COMMANDS_FILE` is set, any command on an allowed target can run. |
| `ALLOWED_COMMANDS_FILE` | no | Path to a file of regexes, one per line. Blank lines and lines starting with `#` are ignored. Combined with `ALLOWED_COMMANDS` if both are set. |
| `HOST`, `PORT` | no | `server.py` only. Default `127.0.0.1:8080`. |

Env files (`docker run --env-file`, systemd `EnvironmentFile`) cannot hold
multi-line values, so on a VM or with plain Docker put the command regexes in a
file and set `ALLOWED_COMMANDS_FILE`. Lambda and Cloud Run accept multi-line
values through a JSON or YAML file; the deploy guides show how.

## Security model

The service runs commands on your infrastructure, so it keeps the attack
surface small and makes every control explicit:

- **Bearer token stored as a hash.** The gateway compares the SHA-256 of the
  presented token in constant time. Its configuration never contains the token
  itself.
- **Target allowlist (`ALLOWED_TARGETS`).** A host that exactly matches a
  hostname in the list is allowed by name. Any other host is resolved, and
  every address it resolves to must fall inside an allowed CIDR. Without this
  the gateway could be used to reach anything it can route to; for example, a
  request to `169.254.169.254` returns `403`. List hostnames only for names
  you control, because they are not re-checked against the CIDRs.
- **Command allowlist (`ALLOWED_COMMANDS`).** Strongly recommended, especially
  when many people or AI-driven test steps hold the token. It decides what can
  run, whatever a caller asks for.
- **Limits.** At most 20 commands per request, single-line commands only, 60
  seconds per wait, 64 KB of output per command (`truncated: true` beyond
  that), and a 256 KB request body.
- **Logs** record host, mode, commands, exit codes and errors. They never
  include passwords, keys or command output.
- **Host keys are not pinned.** Lab devices are often reflashed, so unknown
  host keys are accepted with a warning and the target allowlist limits where
  the gateway can connect. If your environment is stable, pin keys with
  paramiko's `RejectPolicy` and a known_hosts file.
- **Prefer no public endpoint.** Run the gateway next to the devices and reach
  it through the ContextQA tunnel. If you must expose it, put it behind TLS and
  an IP allowlist.
- **Device credentials** travel in the request body. Keep them in ContextQA
  password-type environment variables, and use a dedicated low-privilege
  device account for testing.

## Repo layout

```
src/
  gateway.py          core: validation, policy, exec and shell modes (no transport code)
  auth.py             bearer-token check
  lambda_function.py  AWS Lambda / Function URL entry point
  server.py           stdlib HTTP server entry point (Cloud Run, VM, Docker)
  requirements.txt    paramiko
demo/device/          fake access point: sshd + demo_cli.py (local demo only)
deploy/               aws-lambda/, gcp-cloud-run/, vm/
tests/                unit tests; shell mode runs against the real demo CLI
Dockerfile            gateway image
docker-compose.yml    gateway + fake device for the local demo
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

No dependencies are needed. SSH is faked for exec mode, and shell mode drives
the real `demo/device/demo_cli.py` over pipes, which covers the in-band login,
prompt detection and echo stripping.

## FAQ

**Why a separate service instead of an SSH step inside ContextQA?** Running SSH
from a shared execution environment needs isolation and guardrails. This
pattern works with standard API steps and keeps device credentials and network
access under your control.

**My device's prompt looks different.** Set `prompt` to a regex that matches
it. If the device asks for credentials again inside the session, also set
`login.username_prompt` and `login.password_prompt`. Run the commands once over
SSH by hand to see the exact prompt text.

**My device needs `enable`, or the pager disabled.** Send those as the first
commands in shell mode, for example `["terminal length 0", "show version"]`.

**Can it use SSH keys?** Yes. Pass `private_key` instead of `password`.

**How do I run it on another platform?** `gateway.run()` has no transport code.
Write a small adapter like `lambda_function.py` for your platform's request
format and reuse `validate_request()` and `run()`.
