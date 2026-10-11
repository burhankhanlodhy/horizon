"""Outgoing email over SMTP, so any provider works by configuration alone.

Cloudflare Email Service and AWS SES both offer SMTP; switching between them
is a change of these settings, not of code:

    SMTP_HOST, SMTP_PORT (587), SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_SECURITY ("starttls" default, "ssl" for port 465),
    MAIL_FROM ("ContextShrink <noreply@contextshrink.com>")

With SMTP_HOST unset, email is off: nothing is sent and nothing that depends
on a verified address is enforced (see account_email.verification_required).
"""

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

logger = logging.getLogger("contextshrink.mail")

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
MAIL_FROM = os.environ.get("MAIL_FROM", "ContextShrink <noreply@contextshrink.com>")
SMTP_TIMEOUT_S = 20


def configured() -> bool:
    return bool(SMTP_HOST)


def build_message(to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    domain = MAIL_FROM.rsplit("@", 1)[-1].strip("> ") or "contextshrink.com"
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _send_blocking(msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if SMTP_SECURITY == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_S, context=context
        )
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_S)
    with server:
        if SMTP_SECURITY == "starttls":
            server.starttls(context=context)
        if SMTP_USERNAME:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)


async def send(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Send one email. Returns False (and logs) on failure; never raises."""
    if not configured():
        logger.warning("email not sent (SMTP_HOST unset): %s", subject)
        return False
    msg = build_message(to, subject, text, html)
    try:
        await asyncio.to_thread(_send_blocking, msg)
    except Exception as exc:  # network, auth, rejected recipient
        logger.error("email send failed (%s): %s", subject, type(exc).__name__)
        return False
    return True
