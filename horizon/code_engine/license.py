"""Plan note (entitlement token) verification and the local license cache.

A note is ``base64url(payload_json) + "." + base64url(ed25519_signature)`` with
no padding. The engine compiles in the public key and nothing else, so a note
can only come from the API that holds the private key.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from horizon.code_engine.store import read_json, remove, write_private_json

# Verify key for plan notes. Compiled in; there is deliberately no env override.
CODE_ENTITLEMENT_PUBLIC_KEY_B64 = "jys7sL/fRV7Z+9xWRUZygtzpFGV9os/ns4QwrFzqJD0="
NOTE_ISSUER = "contextshrink-code"
NOTE_VERSION = 1
PLANS = ("trial", "active")
# A note issued a little in the future (clock skew) is still accepted.
CLOCK_SKEW_SECONDS = 300
LICENSE_FILE = "license.json"
_B64URL = re.compile(r"[A-Za-z0-9_-]*")


class NoteError(ValueError):
    """A plan note that must not unlock compression. The message says why, never the token."""


@dataclass(frozen=True)
class Entitlement:
    """The verified claims of one plan note."""

    plan: str
    subject: str
    device_id: str
    device_hash: str
    issued_at: int
    expires_at: int
    until: int

    def valid_at(self, now: float) -> bool:
        return self.issued_at - CLOCK_SKEW_SECONDS <= now < self.expires_at


def _b64url_decode(text: str) -> bytes:
    if not _B64URL.fullmatch(text):
        raise NoteError("malformed")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise NoteError("malformed") from exc


def _int_claim(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise NoteError("malformed")
    return value


def verify_note(token: str, *, device_hash: str, now: float) -> Entitlement:
    """Check a plan note against the compiled-in key and this device, or raise NoteError."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise NoteError("malformed")
    payload_part, signature_part = token.split(".")
    payload_bytes = _b64url_decode(payload_part)
    signature = _b64url_decode(signature_part)
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(CODE_ENTITLEMENT_PUBLIC_KEY_B64))
        key.verify(signature, payload_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise NoteError("bad signature") from exc
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except ValueError as exc:
        raise NoteError("malformed") from exc
    if not isinstance(payload, dict):
        raise NoteError("malformed")
    version = payload.get("v")
    if isinstance(version, bool) or version != NOTE_VERSION or payload.get("iss") != NOTE_ISSUER:
        raise NoteError("wrong note version or issuer")
    if payload.get("dh") != device_hash:
        raise NoteError("issued for another device")
    plan = payload.get("plan")
    if plan not in PLANS:
        raise NoteError("no active plan")
    entitlement = Entitlement(
        plan=plan,
        subject=str(payload.get("sub") or ""),
        device_id=str(payload.get("dev") or ""),
        device_hash=device_hash,
        issued_at=_int_claim(payload, "iat"),
        expires_at=_int_claim(payload, "exp"),
        until=_int_claim(payload, "until"),
    )
    if now < entitlement.issued_at - CLOCK_SKEW_SECONDS:
        raise NoteError("not yet valid")
    if now >= entitlement.expires_at:
        raise NoteError("expired")
    return entitlement


def read_license(home: Path, *, device_hash: str, now: float) -> Entitlement | None:
    """The cached note, if it still verifies for this device and time. Never raises."""
    data = read_json(home / LICENSE_FILE)
    token = data.get("token") if data else None
    if not isinstance(token, str):
        return None
    try:
        return verify_note(token, device_hash=device_hash, now=now)
    except NoteError:
        return None


def write_license(home: Path, token: str, *, fetched_at: float) -> None:
    fetched = datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat()
    write_private_json(
        home / LICENSE_FILE,
        {"version": 1, "token": token, "fetched_at": fetched},
    )


def clear_license(home: Path) -> None:
    remove(home / LICENSE_FILE)
