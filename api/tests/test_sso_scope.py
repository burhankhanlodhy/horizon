"""SSO sessions must never inherit personal workspace or operator access."""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contact  # noqa: E402
import main  # noqa: E402


@pytest.fixture
def scoped_session(monkeypatch):
    team_id, member_id = uuid4(), uuid4()
    calls = []

    async def session(_token):
        return {"user_id": uuid4(), "plan": "pro", "subscription_status": "active", "sso_team_id": team_id}

    async def context(_user, team):
        calls.append(team)
        return {"team_id": team, "membership_id": member_id, "role": "member"}

    async def entitlement(_team):
        return True

    monkeypatch.setenv("CONTEXTSHRINK_TEAM_SSO_ENABLED", "true")
    monkeypatch.setattr(main.db, "resolve_session", session)
    monkeypatch.setattr(main.teams, "context", context)
    monkeypatch.setattr(main.team_billing, "entitlement", entitlement)
    return team_id, calls


def test_sso_forces_team_even_without_workspace_header(scoped_session):
    team_id, calls = scoped_session
    result = asyncio.run(main.current_user("Bearer test", None))
    assert result["team_id"] == team_id and result["plan"] == "team"
    assert calls == [team_id]
    result = asyncio.run(main.current_user("Bearer test", str(team_id)))
    assert result["membership_id"] and result["team_role"] == "member"


def test_sso_cannot_select_personal_or_another_team(scoped_session):
    _team_id, calls = scoped_session
    with pytest.raises(HTTPException) as failure:
        asyncio.run(main.current_user("Bearer test", str(uuid4())))
    assert failure.value.status_code == 403 and not calls


def test_sso_rollout_off_disables_existing_sso_sessions(scoped_session, monkeypatch):
    monkeypatch.setenv("CONTEXTSHRINK_TEAM_SSO_ENABLED", "false")
    with pytest.raises(HTTPException) as failure:
        asyncio.run(main.current_user("Bearer test", None))
    assert failure.value.status_code == 401


def test_sso_session_cannot_inherit_operator_admin_from_email(monkeypatch):
    monkeypatch.setattr(contact, "ADMIN_EMAILS", frozenset({"operator@example.com"}))

    async def user():
        return {"email": "operator@example.com", "sso_team_id": uuid4()}

    app = FastAPI()
    contact.install(app, user)
    with TestClient(app) as client:
        response = client.get("/admin/contact-messages")
    assert response.status_code == 403
