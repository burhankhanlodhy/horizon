"""Fast-mode governor: drop the 2x premium where nobody waits, decided once per launch."""

from __future__ import annotations

import pytest

from horizon.proxy.fast_mode_policy import entrypoint_of, govern, is_headless

INTERACTIVE_UA = {"user-agent": "claude-cli/2.1.291 (external, cli)"}
PRINT_UA = {"user-agent": "claude-cli/2.1.291 (external, sdk-cli)"}


def test_default_policy_forwards_fast_mode(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_FAST_MODE_POLICY", raising=False)
    body = {"speed": "fast"}
    assert govern(body, PRINT_UA) is None
    assert body == {"speed": "fast"}


def test_headless_policy_drops_fast_mode_for_print_runs(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_FAST_MODE_POLICY", "headless")
    body = {"model": "claude-opus-5-5", "speed": "fast"}
    assert govern(body, PRINT_UA) == "fast_mode:dropped:headless"
    assert "speed" not in body


def test_headless_policy_keeps_fast_mode_for_people(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_FAST_MODE_POLICY", "headless")
    body = {"speed": "fast"}
    assert govern(body, INTERACTIVE_UA) is None
    assert body == {"speed": "fast"}


def test_never_policy_drops_everywhere(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_FAST_MODE_POLICY", "never")
    body = {"speed": "fast"}
    assert govern(body, INTERACTIVE_UA) == "fast_mode:dropped:never"
    assert body == {}


def test_standard_speed_requests_are_untouched(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_FAST_MODE_POLICY", "never")
    body = {"speed": "standard"}
    assert govern(body, PRINT_UA) is None
    assert body == {"speed": "standard"}


def test_unknown_policy_is_treated_as_allow(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_FAST_MODE_POLICY", "sometimes")
    body = {"speed": "fast"}
    assert govern(body, PRINT_UA) is None
    assert body == {"speed": "fast"}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-horizon-interactive": "0"}, True),
        ({"X-Horizon-Interactive": "false"}, True),
        # wrap's explicit "interactive" wins over a headless-looking User-Agent.
        ({"x-horizon-interactive": "1", **PRINT_UA}, False),
        (PRINT_UA, True),
        ({"user-agent": "claude-cli/2.1.291 (external, github-action)"}, True),
        (INTERACTIVE_UA, False),
        ({"user-agent": "claude-cli/2.1.291 (external, claude-vscode)"}, False),
        ({}, False),
    ],
)
def test_headless_detection(headers, expected) -> None:
    assert is_headless(headers) is expected


def test_headless_entrypoints_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_FAST_MODE_HEADLESS_ENTRYPOINTS", "claude-vscode")
    assert is_headless({"user-agent": "claude-cli/2 (external, claude-vscode)"})
    assert not is_headless(PRINT_UA)


def test_entrypoint_parsing() -> None:
    assert entrypoint_of("claude-cli/2.1.291 (external, sdk-cli)") == "sdk-cli"
    assert entrypoint_of("claude-cli/2.1.291") == ""
    assert entrypoint_of("") == ""


def test_the_decision_is_the_same_for_every_request_of_a_launch(monkeypatch) -> None:
    """Switching speed mid-session invalidates the cache; the governor never does."""
    monkeypatch.setenv("HORIZON_FAST_MODE_POLICY", "headless")
    outcomes = set()
    for _ in range(10):
        body = {"speed": "fast"}
        govern(body, PRINT_UA)
        outcomes.add(body.get("speed"))
    assert outcomes == {None}


def test_wrap_marks_print_runs_headless_and_respects_a_user_header() -> None:
    from horizon.cli.wrap import _apply_interactive_header_env

    env: dict[str, str] = {}
    _apply_interactive_header_env(env, ("-p", "fix the test"))
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Horizon-Interactive: 0"
    assert is_headless({"x-horizon-interactive": "0"})

    env = {"ANTHROPIC_CUSTOM_HEADERS": "X-Horizon-Project: demo"}
    _apply_interactive_header_env(env, ())
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Horizon-Project: demo\nX-Horizon-Interactive: 1"

    mine = {"ANTHROPIC_CUSTOM_HEADERS": "x-horizon-interactive: 0"}
    _apply_interactive_header_env(mine, ())
    assert mine["ANTHROPIC_CUSTOM_HEADERS"] == "x-horizon-interactive: 0"
