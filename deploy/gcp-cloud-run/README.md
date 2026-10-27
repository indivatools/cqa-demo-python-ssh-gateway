# Deploy on GCP Cloud Run

Good fit when the devices are reachable from a GCP VPC (directly, or via Cloud
VPN / Interconnect to the lab). Cloud Run gives you HTTPS and scales to zero;
**Direct VPC egress** lets the container reach private device IPs.

```
ContextQA ──HTTPS──▶ Cloud Run service ──Direct VPC egress──▶ VPC ──SSH:22──▶ devices
```

## 1. Create a token and store its hash

```sh
TOKEN=$(openssl rand -hex 24)                                    # give this to ContextQA
printf %s "$TOKEN" | sha256sum | cut -d' ' -f1 | \
  gcloud secrets create ssh-gateway-token-sha256 --data-file=-
```

Only the hash lives in Secret Manager; the token itself never touches GCP.

## 2. Deploy from source

Put the non-secret settings in a YAML file. YAML block scalars carry the
multi-line `ALLOWED_COMMANDS` value cleanly:

```yaml
# env.yaml
ALLOWED_TARGETS: "10.20.0.0/24"
ALLOWED_COMMANDS: |-
  ^show \S+
  ^get \S+
```

Then deploy from the repo root (Cloud Build builds the `Dockerfile`):

```sh
PROJECT=my-project
REGION=us-central1
NETWORK=lab-vpc
SUBNET=lab-subnet          # a subnet that routes to the devices

gcloud run deploy ssh-gateway \
  --project $PROJECT --region $REGION \
  --source . \
  --network $NETWORK --subnet $SUBNET --vpc-egress private-ranges-only \
  --env-vars-file env.yaml \
  --set-secrets "GATEWAY_TOKEN_SHA256=ssh-gateway-token-sha256:latest" \
  --allow-unauthenticated \
  --timeout 90 --cpu 1 --memory 256Mi --max-instances 5
```

`ALLOWED_COMMANDS` is optional; leave it out to allow any command on an allowed
target.

- `--allow-unauthenticated` turns off Google IAM on the URL, because a
  ContextQA API step cannot mint Google ID tokens. The bearer token check in the
  gateway still applies to every request.
- The service account Cloud Run runs as needs `roles/secretmanager.secretAccessor`
  on the secret.
- To restrict *who* can reach the URL, put it behind an external HTTPS load
  balancer with Cloud Armor and allowlist ContextQA's egress IPs, or skip the
  public URL entirely and use the ContextQA tunnel (see the main README).

## 3. Test

```sh
URL=$(gcloud run services describe ssh-gateway --region $REGION --format 'value(status.url)')
curl -s "$URL/healthz"
curl -s "$URL" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"host":"10.20.0.15","username":"admin","password":"...","commands":["show version"]}'
```

## Alternatives on GCP

- **Compute Engine VM.** Run it as a systemd service (see `deploy/vm/`). This is
  the simplest option when the lab is already reachable from one VM.
- **GKE.** Use the same container image, give the pod egress to the device
  range, and keep the Service internal (ClusterIP behind an internal
  Ingress/Gateway, or reached through the tunnel).
