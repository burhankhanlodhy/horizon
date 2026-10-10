"""``horizon login`` / ``logout`` / ``account``: use the hosted ContextShrink proxy.

Same account flow as the desktop app: sign in with email and password, get a
per-device key (``CLI: <hostname>``) kept in the OS credential store, and
every ``horizon wrap <tool>`` then runs through the hosted proxy, so usage and
savings show on the account dashboard. The password is never stored.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import sys
from datetime import datetime, timezone
from typing import Any

import click

from horizon import hosted
from horizon.cli.main import main
from horizon.vault import VaultError, clear_secret, get_secret, set_credential, set_secret


def _fail(message: str) -> None:
    click.echo(f"error: {message}", err=True)
    sys.exit(2)


def _missing_relay_deps() -> list[str]:
    return [m for m in ("fastapi", "uvicorn", "keyring") if importlib.util.find_spec(m) is None]


def _signed_out_cleanup() -> None:
    try:
        clear_secret(hosted.KEY_ENTRY)
        clear_secret(hosted.SESSION_ENTRY)
    finally:
        hosted.clear_state()


def _logout(state: dict[str, Any], *, quiet: bool = False) -> bool:
    """Revoke this device's key and end the session; True when the key was revoked."""
    token = get_secret(hosted.SESSION_ENTRY)
    revoked = False
    if token:
        try:
            hosted.revoke_key(token, str(state["key_id"]))
            revoked = True
        except hosted.ApiError as exc:
            if not quiet:
                click.echo(f"  Could not revoke this device's key: {exc}", err=True)
        hosted.end_session(token)
    _signed_out_cleanup()
    if not revoked and not quiet:
        click.echo(
            f"  Revoke '{state.get('device', 'this device')}' on the dashboard's API Keys page: "
            f"{hosted.app_url()}"
        )
    return revoked


@main.command()
@click.option("--email", default=None, help="Account email (prompted when omitted).")
@click.option("--force", is_flag=True, help="Sign in again, replacing this device's key.")
def login(email: str | None, force: bool) -> None:
    """Sign in to ContextShrink so `horizon wrap` uses the hosted proxy."""
    state = hosted.load_state()
    if state and not force:
        click.echo(f"Already signed in as {state.get('email')} ({state.get('device')}).")
        click.echo("Use `horizon login --force` to sign in again, or `horizon logout`.")
        return
    if state:
        _logout(state, quiet=True)

    email = (email or click.prompt("Email")).strip()
    password = getpass.getpass("Password: ")
    if not email or not password:
        _fail("email and password are required")
    try:
        token, user = hosted.login(email, password)
    except hosted.ApiError as exc:
        _fail(str(exc))
    device = hosted.device_name()
    try:
        key = hosted.create_device_key(token, device)
    except hosted.ApiError as exc:
        hosted.end_session(token)
        _fail(f"could not create a device key: {exc}")

    try:
        storage = set_credential(key["key"], name=hosted.KEY_ENTRY, allow_file=True)
        set_secret(hosted.SESSION_ENTRY, token, allow_file=True)
    except VaultError as exc:
        try:
            hosted.revoke_key(token, str(key["id"]))
        finally:
            hosted.end_session(token)
        _fail(str(exc))

    hosted.save_state(
        {
            "email": user.get("email") or email,
            "name": user.get("name"),
            "user_id": user.get("id"),
            "key_id": key["id"],
            "device": device,
            "storage": storage,
            "api_url": hosted.api_url(),
            "proxy_url": hosted.proxy_url(),
            "signed_in_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    click.echo(f"Signed in as {user.get('email') or email}. Device key '{device}' created.")
    if storage == "file":
        from horizon.vault import fallback_path

        click.echo(
            f"  No OS credential store is available, so the key is kept in {fallback_path()} "
            "(readable only by you)."
        )
    missing = _missing_relay_deps()
    if missing:
        click.echo(
            f'  The hosted relay needs {", ".join(missing)}: pip install "contextshrink[hosted]"'
        )
    click.echo("  `horizon wrap <tool>` now runs through ContextShrink; `horizon account` shows")
    click.echo(f"  your plan and savings, and the dashboard is at {hosted.app_url()}")


@main.command()
def logout() -> None:
    """Sign out: revoke this device's key and forget the session."""
    state = hosted.load_state()
    if not state:
        click.echo("Not signed in.")
        return
    revoked = _logout(state)
    click.echo("Signed out." + (f" Device key '{state.get('device')}' revoked." if revoked else ""))


def _usd(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _day(value: Any) -> str:
    return str(value or "")[:10]


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Print the raw account data as JSON.")
def account(as_json: bool) -> None:
    """Show your plan, this cycle's savings and fee, and the last 30 days."""
    state = hosted.load_state()
    if not state:
        _fail("not signed in. Run: horizon login")
    token = get_secret(hosted.SESSION_ENTRY)
    if not token:
        _fail("this device's session is missing. Run: horizon login --force")
    try:
        user = hosted.me(token)
        estimate = hosted.billing_estimate(token)
        usage = hosted.usage_summary(token, 30)
        key_active = hosted.key_is_active(token, str(state["key_id"]))
    except hosted.ApiError as exc:
        _fail(str(exc))

    if as_json:
        click.echo(
            json.dumps(
                {
                    "user": user,
                    "estimate": estimate,
                    "usage": usage,
                    "device": state.get("device"),
                    "key_active": key_active,
                },
                indent=2,
            )
        )
        return

    plan = str(estimate.get("plan") or user.get("plan") or "free")
    status = user.get("subscription_status")
    click.echo(f"{user.get('name') or ''} <{user.get('email') or state.get('email')}>".strip())
    click.echo(f"  Plan:        {plan}" + (f" ({status})" if status else ""))
    click.echo(
        f"  This device: {state.get('device')} - key {'active' if key_active else 'REVOKED'}"
    )
    if not key_active:
        click.echo("               Run `horizon login --force` for a new key.")
    click.echo(
        f"  Cycle:       {_day(estimate.get('period_start'))} to {_day(estimate.get('period_end'))}"
    )
    click.echo(f"  Est. savings this cycle: {_usd(estimate.get('estimated_savings_usd'))}")
    entitlement = estimate.get("compression") or {}
    if plan == "free":
        cap = entitlement.get("cap_usd", 20)
        used = entitlement.get("cycle_savings_usd")
        click.echo(
            f"  Free savings cap: {_usd(used)} of {_usd(cap)}"
            + (
                " - reached; compression paused until the next cycle"
                if entitlement.get("capped")
                else ""
            )
        )
    else:
        rate = float(estimate.get("savings_fee_rate") or 0.05)
        threshold = estimate.get("savings_fee_threshold_usd", 20)
        click.echo(
            f"  Est. fee this cycle: {_usd(estimate.get('estimated_total_usd'))} "
            f"({rate:.0%} of savings above {_usd(threshold)})"
        )
    if estimate.get("payment_issue"):
        click.echo("  Payment issue: open the dashboard's Billing page to fix it.")
    totals = usage.get("totals") or {}
    click.echo(
        "  Last 30 days: "
        f"{int(totals.get('requests') or 0):,} requests, "
        f"{int(totals.get('tokens_saved') or 0):,} tokens saved, "
        f"{_usd(totals.get('savings_usd'))} saved, {_usd(totals.get('cost_usd'))} spent"
    )
    click.echo(f"  Dashboard:   {hosted.app_url()}")
