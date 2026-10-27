# Run on a VM, a lab box, or on-prem

The **recommended** setup when the devices sit in a private lab: run the
gateway on a machine that is already inside that network, keep it bound to
localhost, and let the ContextQA tunnel carry ContextQA's calls to it. Nothing
is exposed to the internet and your firewall rules do not change.

```
ContextQA ──tunnel──▶ tunnel agent (lab box) ──▶ 127.0.0.1:8080 gateway ──SSH──▶ devices
```

Works on any Linux host with Python 3.10+ (Ubuntu, RHEL, Amazon Linux, a GCE or
EC2 VM, a Raspberry Pi on the lab bench).

## Install

```sh
sudo useradd --system --create-home --home-dir /opt/ssh-gateway ssh-gateway
sudo -u ssh-gateway git clone https://github.com/indivatools/cqa-demo-python-ssh-gateway /opt/ssh-gateway/app
cd /opt/ssh-gateway/app
sudo -u ssh-gateway python3 -m venv /opt/ssh-gateway/venv
sudo -u ssh-gateway /opt/ssh-gateway/venv/bin/pip install -r src/requirements.txt
```

## Configure

```sh
TOKEN=$(openssl rand -hex 24)              # give this to ContextQA
sudo tee /etc/ssh-gateway.env >/dev/null <<EOF
GATEWAY_TOKEN_SHA256=$(printf %s "$TOKEN" | sha256sum | cut -d' ' -f1)
ALLOWED_TARGETS=10.20.0.0/24
HOST=127.0.0.1
PORT=8080
EOF
sudo chmod 0640 /etc/ssh-gateway.env && sudo chgrp ssh-gateway /etc/ssh-gateway.env
```

## Run as a service

```sh
sudo cp deploy/vm/ssh-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ssh-gateway
curl -s localhost:8080/healthz          # {"ok": true}
journalctl -u ssh-gateway -f
```

## Or with Docker

```sh
docker build -t ssh-gateway .
docker run -d --name ssh-gateway --restart unless-stopped \
  -p 127.0.0.1:8080:8080 --env-file /etc/ssh-gateway.env -e HOST=0.0.0.0 ssh-gateway
```

(`HOST=0.0.0.0` inside the container; the `-p 127.0.0.1:` mapping keeps it
local on the host.)

## Connect ContextQA

Add a route for `http://127.0.0.1:8080` to your ContextQA tunnel, then point the
ContextQA API step at the tunnelled URL. See the main README for the step setup.
