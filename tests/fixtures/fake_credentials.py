"""Runtime-generated credential placeholders for tests — never committed secrets.

The credential scanner blocks any source literal that resembles an access
token or PEM key block, even an obviously fake one. These helpers mint
throwaway material at runtime instead: values are random per run, valid in
shape only, and useless outside the test process.
"""

from __future__ import annotations

import secrets
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def fake_access_token(prefix: str = "test-only") -> str:
    """Opaque bearer-shaped token, unique per call; carries no authority."""
    return f"{prefix}-{secrets.token_urlsafe(32)}"


@lru_cache(maxsize=1)
def fake_pem_private_key() -> str:
    """PKCS8 PEM of a fresh throwaway RSA key, generated once per session."""
    # Module-level cache keeps repeated constructor calls cheap; the key never
    # leaves this process and guards nothing.
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
