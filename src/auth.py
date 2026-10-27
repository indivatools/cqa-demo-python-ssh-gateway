"""Bearer-token check shared by the Lambda and the standalone server.

The gateway holds only the SHA-256 of the token, so its config (Lambda env,
systemd unit) never carries the secret itself.
"""

import hashlib
import hmac

BEARER_PREFIX = "bearer "


def token_matches(authorization_header: str | None, expected_sha256_hex: str) -> bool:
    if not authorization_header or not expected_sha256_hex:
        return False
    if not authorization_header.lower().startswith(BEARER_PREFIX):
        return False
    token = authorization_header[len(BEARER_PREFIX):].strip()
    presented = hashlib.sha256(token.encode()).hexdigest()
    return hmac.compare_digest(presented, expected_sha256_hex.strip().lower())
