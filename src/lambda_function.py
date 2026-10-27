"""AWS Lambda (Function URL) entry point for the CLI gateway.

Environment:
  GATEWAY_TOKEN_SHA256   hex SHA-256 of the bearer token callers must present
  ALLOWED_TARGETS        comma-separated CIDRs / hostnames the gateway may SSH to
  ALLOWED_COMMANDS       optional newline-separated regexes; a command must match one
  ALLOWED_COMMANDS_FILE  optional path to a file of regexes, one per line
"""

import base64
import json
import logging
import os

from auth import token_matches
from gateway import Policy, RequestError, connect_paramiko, run, validate_request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

POLICY = Policy.from_env(os.environ)
TOKEN_SHA256 = os.environ["GATEWAY_TOKEN_SHA256"]

HTTP_OK = 200
HTTP_BAD_GATEWAY = 502
HTTP_UNAUTHORIZED = 401
HTTP_METHOD_NOT_ALLOWED = 405


def handler(event, _context):
    http = event.get("requestContext", {}).get("http", {})
    if http.get("method") != "POST":
        return _reply(HTTP_METHOD_NOT_ALLOWED, {"ok": False, "error": "use POST"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not token_matches(headers.get("authorization"), TOKEN_SHA256):
        return _reply(HTTP_UNAUTHORIZED, {"ok": False, "error": "unauthorized"})

    try:
        request = validate_request(_json_body(event), POLICY)
    except RequestError as exc:
        logger.info("rejected request: %s", exc)
        return _reply(exc.status, {"ok": False, "error": str(exc)})

    response = run(request, connect_paramiko)
    # Log what ran and how it ended; never credentials or command output.
    logger.info(
        json.dumps(
            {
                "host": request.host,
                "mode": request.mode,
                "commands": list(request.commands),
                "exit_codes": [r.exit_code for r in response.results],
                "ok": response.ok,
                "error": response.error,
            }
        )
    )
    status = HTTP_BAD_GATEWAY if response.error else HTTP_OK
    return _reply(status, response.to_dict())


def _json_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestError(f"body is not valid JSON: {exc.msg}") from exc


def _reply(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }
