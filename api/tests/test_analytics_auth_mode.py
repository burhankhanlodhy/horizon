"""The auth_mode tag the hosted proxy adds to each event (model only, no Postgres)."""

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analytics  # noqa: E402


def _event(**extra):
    return {
        "event_id": str(uuid4()),
        "user_id": str(uuid4()),
        "key_id": str(uuid4()),
        "runtime_id": str(uuid4()),
        "occurred_at": "2026-10-08T12:00:00+00:00",
        "request_id": "req_abc",
        "status": 200,
        **extra,
    }


@pytest.mark.parametrize("mode", ["payg", "oauth", "subscription"])
def test_each_auth_mode_is_accepted_and_stored_in_data(mode):
    e = analytics.EventIn.model_validate(_event(auth_mode=mode))
    assert e.auth_mode == mode
    assert f'"auth_mode":"{mode}"' in e.model_dump_json()


def test_events_without_the_tag_count_as_payg():
    e = analytics.EventIn.model_validate(_event())
    assert e.auth_mode == "payg"


@pytest.mark.parametrize("bad", ["api_key", "PAYG", "", None])
def test_unknown_auth_modes_are_rejected(bad):
    with pytest.raises(ValidationError):
        analytics.EventIn.model_validate(_event(auth_mode=bad))
