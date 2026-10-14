"""Fast checks for the MCPB packaging tool (no PyInstaller run)."""

from __future__ import annotations

import copy
import importlib.util
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.contracts import tool_specs

REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD_PATH = REPO_ROOT / "desktop" / "mcp" / "extension" / "build_extension.py"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("build_extension", _BUILD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_extension = _load_build_module()


def _template() -> dict[str, Any]:
    return build_extension.load_template()


def _fake_onedir(root: Path) -> Path:
    onedir = root / "onedir"
    (onedir / "_internal").mkdir(parents=True)
    (onedir / "contextshrink-desktop.exe").write_bytes(b"MZ-fake-executable")
    (onedir / "_internal" / "lib.dll").write_bytes(b"dll-bytes" * 100)
    (onedir / "_internal" / "horizon" / "desktop_mcp" / "companion_ui").mkdir(parents=True)
    (onedir / "_internal" / "horizon" / "desktop_mcp" / "companion_ui" / "index.html").write_text(
        "<!doctype html>", encoding="utf-8"
    )
    return onedir


def test_template_validates() -> None:
    assert build_extension.validate_manifest(_template()) == []


def test_every_user_config_key_is_referenced_in_args() -> None:
    manifest = _template()
    declared = set(manifest["user_config"])
    referenced = set()
    for text in build_extension._strings(manifest["server"]["mcp_config"]):
        for variable in build_extension._VARIABLE.findall(text):
            if variable.startswith("user_config."):
                referenced.add(variable[len("user_config.") :])
    assert referenced == declared
    assert manifest["user_config"]["approved_roots"]["multiple"] is True
    assert manifest["user_config"]["approved_roots"]["type"] == "directory"


def test_tools_match_the_default_profile_and_declare_generated_tools() -> None:
    manifest = _template()
    expected = [spec["name"] for spec in tool_specs("source")]
    assert [tool["name"] for tool in manifest["tools"]] == expected
    assert manifest["user_config"]["tool_profile"]["default"] == "source"
    assert manifest["tools_generated"] is True


def test_platform_and_server_fields() -> None:
    manifest = _template()
    assert manifest["compatibility"]["platforms"] == ["win32"]
    assert manifest["server"]["type"] == "binary"
    assert manifest["server"]["entry_point"] == "server/contextshrink-desktop.exe"
    assert manifest["server"]["mcp_config"]["command"] == (
        "${__dirname}/server/contextshrink-desktop.exe"
    )


def test_pack_writes_sorted_deterministic_archive(tmp_path: Path) -> None:
    onedir = _fake_onedir(tmp_path)
    first = build_extension.pack("0.9.0", onedir=onedir, output_dir=tmp_path / "first")
    second = build_extension.pack("0.9.0", onedir=onedir, output_dir=tmp_path / "second")
    assert first.name == "contextshrink-desktop-0.9.0.mcpb"
    assert build_extension.sha256_of(first) == build_extension.sha256_of(second)

    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert "manifest.json" in names
        assert names == sorted(names)
        assert names[0] == "manifest.json"
        others = [name for name in names if name != "manifest.json"]
        assert others and all(name.startswith("server/") for name in others)
        assert "server/contextshrink-desktop.exe" in names
        assert "server/_internal/lib.dll" in names
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_DEFLATED
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == "0.9.0"
        assert manifest["server"]["entry_point"] == "server/contextshrink-desktop.exe"
        assert archive.read("server/contextshrink-desktop.exe") == b"MZ-fake-executable"


def test_pack_refuses_missing_executable(tmp_path: Path) -> None:
    onedir = tmp_path / "empty"
    onedir.mkdir()
    with pytest.raises(SystemExit):
        build_extension.pack("0.9.0", onedir=onedir, output_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.mcpb"))


def test_pack_refuses_invalid_version(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_extension.pack("latest", onedir=_fake_onedir(tmp_path), output_dir=tmp_path / "out")


def test_validate_rejects_undeclared_user_config_variable() -> None:
    manifest = copy.deepcopy(_template())
    manifest["server"]["mcp_config"]["args"].append("${user_config.nope}")
    problems = build_extension.validate_manifest(manifest)
    assert any("user_config.nope" in problem for problem in problems)


def test_validate_rejects_missing_required_field() -> None:
    manifest = copy.deepcopy(_template())
    del manifest["description"]
    problems = build_extension.validate_manifest(manifest)
    assert "missing required field: description" in problems


def test_validate_rejects_missing_mcp_config_field() -> None:
    manifest = copy.deepcopy(_template())
    del manifest["server"]["mcp_config"]["args"]
    problems = build_extension.validate_manifest(manifest)
    assert "missing required field: server.mcp_config.args" in problems


def test_validate_rejects_tool_list_drift() -> None:
    manifest = copy.deepcopy(_template())
    manifest["tools"] = manifest["tools"][:3]
    problems = build_extension.validate_manifest(manifest)
    assert any("do not match tool_specs" in problem for problem in problems)
    manifest = copy.deepcopy(_template())
    manifest.pop("tools_generated")
    assert any(
        "tools_generated" in problem for problem in build_extension.validate_manifest(manifest)
    )


def test_validate_checks_entry_point_in_build_output(tmp_path: Path) -> None:
    assert build_extension.validate_manifest(_template(), onedir=_fake_onedir(tmp_path)) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    problems = build_extension.validate_manifest(_template(), onedir=empty)
    assert any("entry_point is missing" in problem for problem in problems)
