"""Pilot method setting: manifest entry, argument mapping, raw presentation, rebuild."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

# mcp.client.stdio binds its default errlog to sys.stderr at import time. Import it
# during collection so a later capture-based test cannot leave it on a closed file.
import mcp.client.stdio  # noqa: F401
import pytest

from horizon.desktop_mcp import __main__ as cli
from horizon.desktop_mcp import backend_worker, extension
from horizon.desktop_mcp.local_backend import LocalBackend
from horizon.desktop_mcp.settings import ConfigurationError
from horizon.desktop_mcp.source_access import SourceReader
from horizon.desktop_mcp.version import VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD_PATH = REPO_ROOT / "desktop" / "mcp" / "extension" / "build_extension.py"
_MANIFEST_PATH = REPO_ROOT / "desktop" / "mcp" / "extension" / "manifest.template.json"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("build_extension", _BUILD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_extension = _load_build_module()


def _manifest() -> dict[str, Any]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _settings_for(method: str | None, root: Path):
    argv = ["serve", "--extension", "--approved-roots", str(root)]
    if method is not None:
        argv += ["--pilot-method", method]
    return extension._extension_settings(cli._parser().parse_args(argv))


# --- manifest ----------------------------------------------------------------------


def test_manifest_declares_the_pilot_method_setting() -> None:
    entry = _manifest()["user_config"]["pilot_method"]
    assert entry["type"] == "string"
    assert entry["title"] == "Pilot method"
    assert entry["default"] == "normal"
    assert "normal" in entry["description"]
    assert "exact_only" in entry["description"]
    assert "self-test method A" in entry["description"]


def test_manifest_passes_pilot_method_before_approved_roots() -> None:
    args = _manifest()["server"]["mcp_config"]["args"]
    pair = args.index("--pilot-method")
    assert args[pair + 1] == "${user_config.pilot_method}"
    assert pair < args.index("--approved-roots")
    # --approved-roots stays last: its values are expanded after the flag.
    assert args[-2:] == ["--approved-roots", "${user_config.approved_roots}"]


def test_template_and_build_validation_pass() -> None:
    assert build_extension.validate_manifest(build_extension.load_template()) == []
    assert build_extension.main(["validate"]) == 0


def test_version_is_the_current_release() -> None:
    assert VERSION == "0.1.2"


# --- argument mapping ----------------------------------------------------------------


def test_pilot_method_defaults_to_normal_and_optimized_arm(tmp_path) -> None:
    settings = _settings_for(None, tmp_path)
    assert settings.eval_arm == "optimized"


@pytest.mark.parametrize(("method", "arm"), [("normal", "optimized"), ("exact_only", "raw")])
def test_pilot_method_maps_to_eval_arm(method: str, arm: str, tmp_path) -> None:
    assert _settings_for(method, tmp_path).eval_arm == arm


def test_parser_collects_pilot_method_as_given(tmp_path) -> None:
    args = cli._parser().parse_args(["serve", "--extension", "--pilot-method", "exact_only"])
    assert args.pilot_method == "exact_only"
    assert cli._parser().parse_args(["serve", "--extension"]).pilot_method is None


@pytest.mark.parametrize("value", ["", "raw", "optimized", "Exact_Only", "exact-only", " normal"])
def test_invalid_pilot_method_is_rejected_without_echo(value: str, tmp_path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _settings_for(value, tmp_path)
    assert caught.value.code == "extension_pilot_method_invalid"
    assert caught.value.fields == ()
    assert value not in caught.value.message or value == ""  # empty value: nothing to echo


def test_invalid_pilot_method_exits_2_with_sanitized_stderr(tmp_path, capfd) -> None:
    # capfd (not capsys): the stdio tests later in the session need a real stderr fd.
    code = cli.main(
        ["serve", "--extension", "--approved-roots", str(tmp_path), "--pilot-method", "bogus"]
    )
    streams = capfd.readouterr()
    assert code == 2
    assert json.loads(streams.err) == {"error": "extension_pilot_method_invalid", "fields": []}
    assert "bogus" not in streams.err
    assert streams.out == ""


def test_pilot_method_without_extension_is_rejected(capsys) -> None:
    assert cli.main(["serve", "--pilot-method", "exact_only"]) == 2
    streams = capsys.readouterr()
    assert json.loads(streams.err) == {"error": "extension_flags_require_extension"}


# --- settings file -------------------------------------------------------------------


def test_settings_file_carries_eval_arm_raw_for_exact_only(tmp_path) -> None:
    path = tmp_path / "desktop-ext.json"
    extension.write_extension_settings(_settings_for("exact_only", tmp_path), path)
    assert json.loads(path.read_text(encoding="utf-8"))["eval_arm"] == "raw"


def test_settings_file_carries_eval_arm_optimized_for_normal(tmp_path) -> None:
    path = tmp_path / "desktop-ext.json"
    extension.write_extension_settings(_settings_for("normal", tmp_path), path)
    assert json.loads(path.read_text(encoding="utf-8"))["eval_arm"] == "optimized"


# --- raw presentation ----------------------------------------------------------------

_TEXT = "".join(f"line {number}: alpha beta gamma delta\n" for number in range(1, 41))


@pytest.mark.parametrize(
    ("mode", "kwargs"),
    [
        ("exact", {}),
        ("compact", {}),
        ("focused", {"query": "gamma"}),
        ("overview", {}),
    ],
)
def test_raw_open_returns_exact_text_for_every_mode(mode: str, kwargs, tmp_path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    note = root / "notes.txt"
    # Bytes, not write_text: Windows text mode would store CRLF and the exact
    # read returns the stored bytes unchanged.
    note.write_bytes(_TEXT.encode("utf-8"))
    backend = LocalBackend(SourceReader((root,)), eval_arm="raw")

    payload = backend.open(str(note), mode=mode, **kwargs)

    assert payload is not None
    assert payload["mode"] == "exact"
    assert payload["text"] == _TEXT
    assert not payload.get("compression_applied")  # absent on exact pages
    assert payload["eval_arm"] == "raw"


def test_optimized_arm_presents_focused_and_overview_differently(tmp_path) -> None:
    # Control: the same file is not returned as exact text under normal settings,
    # so the raw assertions above are meaningful.
    root = tmp_path / "approved"
    root.mkdir()
    note = root / "notes.txt"
    note.write_bytes(_TEXT.encode("utf-8"))
    backend = LocalBackend(SourceReader((root,)), eval_arm="optimized")
    focused = backend.open(str(note), mode="focused", query="gamma")
    overview = backend.open(str(note), mode="overview")
    assert focused is not None and focused["mode"] == "focused"
    assert overview is not None and overview["mode"] == "overview"
    assert focused.get("text") != _TEXT
    assert overview.get("text") != _TEXT


def test_raw_backend_reports_its_evaluation_arm(tmp_path) -> None:
    raw = LocalBackend(SourceReader((tmp_path,)), eval_arm="raw")
    optimized = LocalBackend(SourceReader((tmp_path,)), eval_arm="optimized")
    assert raw.stats()["evaluation_arm"] == "raw"
    assert optimized.stats()["evaluation_arm"] == "optimized"


def test_raw_backend_rejects_unknown_arm() -> None:
    with pytest.raises(ValueError):
        LocalBackend(SourceReader(()), eval_arm="compact")


# --- backend rebuild on change --------------------------------------------------------


def test_changing_the_pilot_method_rebuilds_the_worker_backend(tmp_path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    path = tmp_path / "desktop-ext.json"
    normal = _settings_for("normal", root)
    extension.write_extension_settings(normal, path)
    runtime = backend_worker.Runtime(
        path,
        expected_broker_id=normal.broker_id,
        expected_max_clients=normal.max_broker_clients,
        expected_timeout=normal.broker_timeout_seconds,
    )
    try:
        assert runtime.backend is not None
        assert runtime.backend.eval_arm == "optimized"
        extension.write_extension_settings(_settings_for("exact_only", root), path)
        runtime._reload()
        assert runtime.backend is not None
        assert runtime.backend.eval_arm == "raw"
    finally:
        runtime.close()
