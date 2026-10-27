#!/bin/sh
# Sets the demo password on both logins, then runs sshd in the foreground.
set -eu

: "${DEVICE_PASSWORD:?set DEVICE_PASSWORD}"

echo "labops:${DEVICE_PASSWORD}" | chpasswd
echo "ap:${DEVICE_PASSWORD}" | chpasswd

# The in-band "Please login:" check compares a hash, never the password.
digest=$(printf %s "$DEVICE_PASSWORD" | python3 -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
printf '{"login_user": "ap", "login_password_sha256": "%s"}\n' "$digest" > /etc/demo-ap/config.json

ssh-keygen -A >/dev/null
exec /usr/sbin/sshd -D -e
