from pathlib import Path

import pytest
from pydantic import ValidationError

from horizon.desktop_mcp.settings import ConfigurationError, DesktopSettings, load_settings


def test_settings_defaults_and_frozen_model() -> None:
    settings = load_settings()
    assert settings.profile == "diagnostic"
    assert settings.approved_roots == ()
    assert settings.result_token_budget == 2000
    assert settings.eval_arm == "optimized"
    assert settings.min_compaction_gain_tokens == 64
    assert settings.min_compaction_gain_percent == 5.0
    with pytest.raises(ValidationError):
        settings.result_token_budget = 300  # type: ignore[misc]


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile", "production"),
        ("result_token_budget", 127),
        ("result_token_budget", 8001),
        ("result_token_budget", True),
        ("eval_arm", "other"),
        ("min_compaction_gain_tokens", -1),
        ("min_compaction_gain_tokens", 8001),
        ("min_compaction_gain_percent", -0.1),
        ("min_compaction_gain_percent", 100.1),
        ("max_result_bytes", 1023),
        ("max_input_bytes", 1048577),
        ("snapshot_ttl_seconds", 59),
        ("max_source_bytes", 25 * 1024 * 1024 + 1),
        ("broker_id", "not a pipe name"),
        ("unknown", "secret"),
        ("approved_roots", ["relative/path"]),
    ],
)
def test_settings_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        DesktopSettings.model_validate({field: value})


def test_approved_roots_accept_absolute_paths_without_authorizing_reads(tmp_path: Path) -> None:
    settings = DesktopSettings.model_validate({"approved_roots": [str(tmp_path)]})
    assert settings.approved_roots == (tmp_path,)


def test_local_profile_allows_empty_roots_for_revocation() -> None:
    settings = DesktopSettings.model_validate({"profile": "local", "approved_roots": []})
    assert settings.profile == "local"
    assert settings.approved_roots == ()


def test_snapshot_and_store_bounds_must_cover_source_limit() -> None:
    with pytest.raises(ValidationError):
        DesktopSettings.model_validate({"max_snapshot_bytes": 1024})
    with pytest.raises(ValidationError):
        DesktopSettings.model_validate({"max_store_bytes": 8 * 1024 * 1024})


def test_load_explicit_json_config(tmp_path: Path) -> None:
    config = tmp_path / "settings.json"
    config.write_text('{"result_token_budget": 512}', encoding="utf-8")
    assert load_settings(config).result_token_budget == 512


def test_load_phase_c_evaluation_settings(tmp_path: Path) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        '{"eval_arm":"raw","min_compaction_gain_tokens":96,"min_compaction_gain_percent":7.5}',
        encoding="utf-8",
    )
    loaded = load_settings(config)
    assert loaded.eval_arm == "raw"
    assert loaded.min_compaction_gain_tokens == 96
    assert loaded.min_compaction_gain_percent == 7.5


@pytest.mark.parametrize(
    "contents,code,leaked",
    [
        ("{" + "x" * 32, "config_invalid_json", "x"),
        ('{"counting_model":"TOP_SECRET", "unknown": "PRIVATE"}', "config_invalid", "TOP_SECRET"),
        ("[]", "config_invalid_shape", "[]"),
    ],
)
def test_config_errors_are_sanitized(tmp_path: Path, contents: str, code: str, leaked: str) -> None:
    config = tmp_path / "sensitive-config.json"
    config.write_text(contents, encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config)
    message = str(caught.value)
    assert caught.value.code == code
    assert str(config) not in message
    assert leaked not in message


def test_oversized_config_rejected_before_json_decode(tmp_path: Path) -> None:
    config = tmp_path / "huge.json"
    config.write_bytes(b"x" * 65537)
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config)
    assert caught.value.code == "config_too_large"
    assert str(config) not in str(caught.value)


@pytest.mark.parametrize(
    "contents",
    ["[" * 30000 + "]" * 30000, "{" + '"n":' + "9" * 5000 + "}"],
    ids=["deep-nesting", "large-integer"],
)
def test_parser_resource_errors_are_sanitized(tmp_path: Path, contents: str) -> None:
    config = tmp_path / "parser-edge.json"
    config.write_text(contents, encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config)
    assert caught.value.code == "config_invalid_json"
    assert str(config) not in str(caught.value)


def test_unknown_config_key_is_not_reflected_in_error_fields(tmp_path: Path) -> None:
    config = tmp_path / "private.json"
    config.write_text('{"SECRET_CONFIG_KEY":"value"}', encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config)
    assert caught.value.fields == ("unknown_field",)
    assert "SECRET_CONFIG_KEY" not in caught.value.fields
    assert "SECRET_CONFIG_KEY" not in str(caught.value)


def test_missing_explicit_config_has_sanitized_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(tmp_path / "does-not-exist.json")
    assert caught.value.code == "config_unreadable"
    assert str(tmp_path) not in str(caught.value)
