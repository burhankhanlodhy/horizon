"""Health probes answer HEAD as well as GET.

Uptime monitors such as UptimeRobot probe with HEAD by default. A GET-only
route answered 405 (API) or, through the Pi 5 gateway, 404; on the proxy a
HEAD that matched no route could also fall through to the passthrough relay.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from horizon.proxy.server import ProxyConfig, create_app


@pytest.fixture(scope="module")
def client():
    config = ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
    )
    with TestClient(create_app(config)) as c:
        yield c


@pytest.mark.parametrize("path", ["/livez", "/readyz", "/health"])
def test_health_probes_answer_head_like_get(client, path):
    get = client.get(path)
    head = client.head(path)
    assert head.status_code == get.status_code
    assert head.status_code < 400 or path != "/livez"
    assert head.content == b""
