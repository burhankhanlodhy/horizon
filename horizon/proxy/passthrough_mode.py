"""Single source of truth for pass-through mode (``horizon proxy --passthrough``).

Pass-through mode makes the proxy a byte-faithful relay: every request body,
header and usage figure the client sent or received is forwarded unchanged,
except for the documented exceptions below. It is the mode the Claude Desktop
3P gateway runs in (see ``wiki/plans/2026-10-16-desktop-gateway-passthrough.md``).

Where the switch lives:

* ``ProxyConfig.passthrough`` is the only stored value. The CLI sets it from
  ``--passthrough`` or ``HORIZON_PASSTHROUGH=1``; nothing else reads the env var.
* Every transform checks :func:`is_passthrough` explicitly at its call site, so
  turning pass-through on does not depend on each transform's own env default.
* :func:`passthrough_violations` drives the safety net in
  :func:`horizon.proxy.body_forwarding.select_outbound_body`: a body that a
  transform still mutated is replaced by the client's original bytes.

Documented exceptions (the only body changes allowed in pass-through):

* ``model_id_sanitized``: ``sanitize_anthropic_model_id`` strips ANSI styling
  from the model id. Upstream rejects that styling, so the sanitized body is
  forwarded and the reason is logged.

Header changes are limited to hop-by-hop headers and the proxy's own
``x-horizon-*`` headers.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

PASSTHROUGH_ENV = "HORIZON_PASSTHROUGH"

#: Startup line printed when pass-through is on.
PASSTHROUGH_STARTUP_LINE = "passthrough: on (all transforms off)"

#: Body mutation reasons that pass-through still forwards.
ALLOWED_PASSTHROUGH_MUTATIONS: frozenset[str] = frozenset({"model_id_sanitized"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def passthrough_env_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``HORIZON_PASSTHROUGH`` is set to a truthy value."""
    source = os.environ if environ is None else environ
    return str(source.get(PASSTHROUGH_ENV, "")).strip().lower() in _TRUTHY


def is_passthrough(config: object | None) -> bool:
    """Whether ``config`` runs in pass-through mode.

    Only an explicit ``True`` counts. ``None``, config-shaped objects without the
    attribute, and loose test doubles (a bare ``MagicMock`` attribute is truthy)
    all read as off, so an unrelated object can never switch the proxy into
    relay mode.
    """
    return getattr(config, "passthrough", False) is True


def passthrough_violations(reasons: Iterable[str]) -> list[str]:
    """Mutation reasons that pass-through does not allow, in order, deduplicated."""
    out: list[str] = []
    for reason in reasons:
        if reason not in ALLOWED_PASSTHROUGH_MUTATIONS and reason not in out:
            out.append(reason)
    return out
