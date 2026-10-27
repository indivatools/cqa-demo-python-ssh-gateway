"""Standalone HTTP server for the SSH CLI gateway.

Same request/response contract as the Lambda handler. Use it anywhere a
container or a Python process can run: GCP Cloud Run, a VM, a lab box inside
your firewall, a laptop.

  pip install -r requirements.txt
  export GATEWAY_TOKEN_SHA256=$(printf %s "$TOKEN" | sha256sum | cut -d' ' -f1)
  export ALLOWED_TARGETS=10.20.0.0/24            # lab device subnet
  python server.py                               # 127.0.0.1:8080

Bind address and port come from --bind/--port, else HOST/PORT (Cloud Run sets
PORT), else 127.0.0.1:8080. Plain HTTP: put TLS in front of it (Cloud Run,
a load balancer, the ContextQA tunnel, or a reverse proxy).
"""

import argparse
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from auth import token_matches
from gateway import Policy, RequestError, connect_paramiko, run, validate_request

MAX_BODY_BYTES = 256 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_BAD_GATEWAY = 502

logger = logging.getLogger("ssh-gateway")


def make_handler(policy: Policy, token_sha256: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self._reply(HTTP_OK, {"ok": True})
                return
            self._reply(HTTP_NOT_FOUND, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not token_matches(self.headers.get("Authorization"), token_sha256):
                self._reply(HTTP_UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._reply(HTTP_PAYLOAD_TOO_LARGE, {"ok": False, "error": "body too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"null")
                request = validate_request(body, policy)
            except json.JSONDecodeError as exc:
                self._reply(HTTP_BAD_REQUEST, {"ok": False, "error": f"invalid JSON: {exc.msg}"})
                return
            except RequestError as exc:
                self._reply(exc.status, {"ok": False, "error": str(exc)})
                return

            response = run(request, connect_paramiko)
            # What ran and how it ended; never credentials or command output.
            logger.info(
                "host=%s mode=%s commands=%s ok=%s error=%s",
                request.host, request.mode, list(request.commands), response.ok, response.error,
            )
            self._reply(HTTP_BAD_GATEWAY if response.error else HTTP_OK, response.to_dict())

        def log_message(self, fmt, *args):
            logger.debug(fmt, *args)

        def _reply(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="SSH CLI gateway HTTP server")
    parser.add_argument("--bind", default=os.environ.get("HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    policy = Policy.from_strings(os.environ["ALLOWED_TARGETS"], os.environ.get("ALLOWED_COMMANDS", ""))
    handler = make_handler(policy, os.environ["GATEWAY_TOKEN_SHA256"])
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    logger.info("ssh-gateway listening on %s:%d", args.bind, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
