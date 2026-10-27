#!/usr/bin/env bash
# Builds dist/ssh-gateway-lambda.zip: the gateway sources plus paramiko's
# manylinux wheels for the Lambda architecture you deploy.
#
#   ./deploy/aws-lambda/build.sh            # arm64 (Graviton), python3.13
#   ARCH=x86_64 ./deploy/aws-lambda/build.sh
set -euo pipefail

cd "$(dirname "$0")/../.."
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"   # must match the function's runtime
ARCH="${ARCH:-arm64}"
case "$ARCH" in
  arm64)  PLATFORM=manylinux2014_aarch64 ;;
  x86_64) PLATFORM=manylinux2014_x86_64 ;;
  *) echo "ARCH must be arm64 or x86_64" >&2; exit 2 ;;
esac

rm -rf build dist
mkdir -p build dist
python3 -m pip install --quiet --target build --platform "$PLATFORM" \
  --implementation cp --python-version "$PYTHON_VERSION" --only-binary=:all: \
  -r src/requirements.txt
cp src/gateway.py src/auth.py src/lambda_function.py build/
(cd build && zip -qr ../dist/ssh-gateway-lambda.zip . -x '*/__pycache__/*')
echo "built dist/ssh-gateway-lambda.zip ($(du -h dist/ssh-gateway-lambda.zip | cut -f1)) for $ARCH"
