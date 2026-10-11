"""Cloudflare Turnstile check for the public forms (signup, contact).

Off until TURNSTILE_SECRET is set: with no secret every request passes, so the
API can ship before the widget exists. With a secret, a request must carry a
token the browser widget produced; Cloudflare's siteverify endpoint confirms
it once. Failures to reach Cloudflare fail closed (the form asks to retry),
because an open gate during an outage is exactly when bots would get through.
"""

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException, Request, status

logger = logging.getLogger("contextshrink.turnstile")

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT_S = 8


def secret() -> str:
    return os.environ.get("TURNSTILE_SECRET", "").strip()


def enabled() -> bool:
    return bool(secret())


def _siteverify(token: str, remote_ip: str | None) -> dict:
    fields = {"secret": secret(), "response": token}
    if remote_ip:
        fields["remoteip"] = remote_ip
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(SITEVERIFY_URL, data=data, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def verify(token: str | None, request: Request) -> None:
    """Raise 400 unless Turnstile is off or ``token`` is a fresh, valid pass."""
    if not enabled():
        return
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please complete the security check.")
    remote_ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else None
    )
    try:
        result = await asyncio.to_thread(_siteverify, token, remote_ip)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.error("turnstile siteverify unreachable: %s", type(exc).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "We couldn't run the security check. Please try again in a moment.",
        ) from None
    if not result.get("success"):
        logger.info("turnstile rejected: %s", ",".join(result.get("error-codes") or []))
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The security check failed or expired. Please try it again.",
        )
