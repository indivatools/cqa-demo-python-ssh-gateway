# Deploy on AWS Lambda

Good fit when the target devices are reachable from an AWS VPC (directly, via
VPN/Direct Connect, or peering). The function runs **inside your VPC** so it can
reach private device IPs, and a **Function URL** gives ContextQA an HTTPS
endpoint to call.

```
ContextQA ──HTTPS──▶ Function URL ──▶ Lambda (in VPC) ──SSH:22──▶ devices
```

## 1. Build the package

```sh
./deploy/aws-lambda/build.sh          # -> dist/ssh-gateway-lambda.zip (arm64, python3.13)
```

The script installs paramiko's Linux wheels for the Lambda architecture, so it
works from macOS or Windows too. Use `ARCH=x86_64` for an x86 function.

## 2. Create a token

```sh
TOKEN=$(openssl rand -hex 24)                         # give this to ContextQA
TOKEN_SHA256=$(printf %s "$TOKEN" | sha256sum | cut -d' ' -f1)   # macOS: shasum -a 256
```

The function only ever stores the hash.

## 3. Create the function

Write the environment to a JSON file. JSON can carry the multi-line
`ALLOWED_COMMANDS` value (`\n` between regexes), which the inline
`Variables={...}` syntax cannot:

```sh
cat > env.json <<EOF
{"Variables": {
  "GATEWAY_TOKEN_SHA256": "$TOKEN_SHA256",
  "ALLOWED_TARGETS": "10.20.0.0/24",
  "ALLOWED_COMMANDS": "^show \\\\S+\n^get \\\\S+"
}}
EOF
```

Then create the role and the function:

```sh
REGION=us-east-1
SUBNETS=subnet-aaa,subnet-bbb          # private subnets that can route to the devices
SG=sg-xxxx                             # egress tcp/22 to the device range only

aws iam create-role --role-name ssh-gateway-lambda \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name ssh-gateway-lambda \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole

aws lambda create-function --region $REGION \
  --function-name ssh-gateway \
  --runtime python3.13 --architectures arm64 \
  --handler lambda_function.handler \
  --zip-file fileb://dist/ssh-gateway-lambda.zip \
  --role "arn:aws:iam::<ACCOUNT_ID>:role/ssh-gateway-lambda" \
  --timeout 90 --memory-size 256 \
  --vpc-config SubnetIds=$SUBNETS,SecurityGroupIds=$SG \
  --environment file://env.json
```

`ALLOWED_COMMANDS` is optional; remove it from `env.json` to allow any command
on an allowed target.

## 4. Expose it with a Function URL

```sh
aws lambda create-function-url-config --region $REGION \
  --function-name ssh-gateway --auth-type NONE

# Since Oct 2025 a public Function URL needs BOTH grants or every call is 403.
aws lambda add-permission --region $REGION --function-name ssh-gateway \
  --statement-id url-public --action lambda:InvokeFunctionUrl \
  --principal '*' --function-url-auth-type NONE
aws lambda add-permission --region $REGION --function-name ssh-gateway \
  --statement-id url-invoke --action lambda:InvokeFunction \
  --principal '*' --invoked-via-function-url
```

`--auth-type NONE` means no IAM signing; the gateway still rejects any call
without the bearer token. ContextQA API steps send a header, not a SigV4
signature, so IAM auth would not work here.

> **Terraform note:** `aws_lambda_function_url` already creates the
> `InvokeFunctionUrl` statement; declaring it again fails with a 409. You only
> add the `InvokeFunction` one yourself.

## 5. Test

```sh
URL=$(aws lambda get-function-url-config --function-name ssh-gateway --query FunctionUrl --output text)
curl -s "$URL" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"host":"10.20.0.15","username":"admin","password":"...","commands":["show version"]}'
```

## Gotchas

- **No internet egress needed.** The function only talks to the devices; it
  never calls AWS APIs at runtime, so no NAT gateway or VPC endpoints.
- A VPC Lambda's cold start is a few seconds; set the ContextQA step timeout
  accordingly.
- Logs go to CloudWatch (`/aws/lambda/ssh-gateway`). They contain host,
  commands and exit codes, never passwords or command output.
