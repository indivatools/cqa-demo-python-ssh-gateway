#!/usr/bin/env python3
"""Stand-in for an access point's CLI, so the gateway demo has something to drive.

It does nothing real. It keeps a firmware version and a controller address in a
state file, enough to show the flow from the call: read the version, re-point
the device from the production controller to the QA one, and read it back.

  demo-cli get version                one-shot, for the gateway's exec mode
  demo-cli --interactive [--login]    prompt loop, for shell mode; --login asks
                                      "Please login:" / "Password:" first, like
                                      a device that logs in again inside SSH

Runs on the box's system python (3.9 on AL2023): stdlib only, no 3.10+ syntax.
"""

import getpass
import hashlib
import hmac
import json
import os
import re
import sys

# Overridable so the gateway's tests can drive this CLI over a local PTY.
CONFIG_PATH = os.environ.get("DEMO_AP_CONFIG", "/etc/demo-ap/config.json")
STATE_PATH = os.environ.get("DEMO_AP_STATE", "/var/lib/demo-ap/state.json")
PROMPT = "demo-ap> "
FIRMWARE_VERSION = "1.4.2.118"
MODEL = "DEMO-AP-650"
PRODUCTION_CONTROLLER = "controller.prod.example.net"
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
MAX_LOGIN_ATTEMPTS = 3

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

HELP = """commands:
  get version             firmware and model
  get controller          controller this AP reports to
  set controller <host>   re-point the AP to another controller
  reset controller        back to the production controller
  help                    this text
  exit                    leave the CLI (interactive only)"""


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"controller": PRODUCTION_CONTROLLER}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def execute(line):
    """Run one command line. Returns (exit_code, output)."""
    words = line.split()
    if not words:
        return EXIT_OK, ""
    if words == ["help"]:
        return EXIT_OK, HELP
    if words == ["get", "version"]:
        return EXIT_OK, "model: %s\nfirmware: %s" % (MODEL, FIRMWARE_VERSION)
    if words == ["get", "controller"]:
        return EXIT_OK, "controller: %s" % load_state()["controller"]
    if words[:2] == ["set", "controller"]:
        if len(words) != 3 or not HOSTNAME_RE.match(words[2]):
            return EXIT_ERROR, "ERROR: usage: set controller <hostname>"
        state = load_state()
        state["controller"] = words[2]
        save_state(state)
        return EXIT_OK, "OK"
    if words == ["reset", "controller"]:
        save_state({"controller": PRODUCTION_CONTROLLER})
        return EXIT_OK, "OK"
    return EXIT_ERROR, "ERROR: unknown command '%s' (try help)" % line.strip()


def login():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    for _ in range(MAX_LOGIN_ATTEMPTS):
        user = input("Please login: ").strip()
        password = getpass.getpass("Password: ")
        digest = hashlib.sha256(password.encode()).hexdigest()
        if user == config["login_user"] and hmac.compare_digest(digest, config["login_password_sha256"]):
            print("Welcome to %s" % MODEL)
            return True
        print("Login incorrect")
    return False


def interactive(require_login):
    if require_login and not login():
        return EXIT_ERROR
    while True:
        try:
            line = input(PROMPT)
        except EOFError:
            return EXIT_OK
        if line.strip() in ("exit", "quit"):
            return EXIT_OK
        _, output = execute(line)
        if output:
            print(output)


def main(argv):
    if argv[:1] == ["--interactive"]:
        return interactive(require_login="--login" in argv)
    # sshd runs a login shell as `<shell> -c "<command>"` for exec requests.
    if argv[:1] == ["-c"]:
        argv = argv[1].split() if len(argv) > 1 else []
    if not argv:
        print(HELP, file=sys.stderr)
        return EXIT_USAGE
    code, output = execute(" ".join(argv))
    print(output, file=sys.stdout if code == EXIT_OK else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
