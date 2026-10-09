"""Password hashing and opaque session-token handling.

Tokens are returned to the client once; only SHA-256 hashes are stored.
"""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()
