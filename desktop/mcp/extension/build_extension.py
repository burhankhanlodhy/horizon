"""Build, validate and pack the contextshrink-desktop Claude Desktop extension (MCPB).

Subcommands:
  build     freeze entry.py with PyInstaller (--onedir) into build/dist/
  validate  check manifest.template.json (and the build output when present)
  pack      render manifest.json and zip it with server/ into dist/*.mcpb
  all       build, validate, pack

Nothing here uploads or publishes. Build output is git-ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# One version source for the MCP server, the manifest and the runtime build id.
from horizon.desktop_mcp.version import VERSION  # noqa: E402

EXE_NAME = "contextshrink-desktop"
ENTRY_SCRIPT = HERE / "entry.py"
TEMPLATE_PATH = HERE / "manifest.template.json"
BUILD_DIR = HERE / "build"
ONEDIR = BUILD_DIR / "dist" / EXE_NAME
DIST_DIR = HERE / "dist"
COMPANION_UI = REPO_ROOT / "horizon" / "desktop_mcp" / "companion_ui"
COMPANION_TARGET = "horizon/desktop_mcp/companion_ui"

_REQUIRED_TOP_LEVEL = ("manifest_version", "name", "version", "description", "author", "server")
_REQUIRED_SERVER = ("type", "entry_point", "mcp_config")
_REQUIRED_MCP_CONFIG = ("command", "args", "env")
_USER_CONFIG_TYPES = frozenset({"string", "number", "boolean", "directory", "file"})
_MULTIPLE_TYPES = frozenset({"directory", "file"})
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
_VARIABLE = re.compile(r"\$\{([^}]*)\}")
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

_HIDDEN_IMPORTS = (
    "win32pipe",
    "win32file",
    "win32security",
    "win32api",
    "win32con",
    "pywintypes",
    "win32timezone",
    "pypdf",
    "mcp.server",
    "mcp.server.lowlevel",
    "mcp.server.models",
    "mcp.server.session",
    "mcp.server.stdio",
    "mcp.client.stdio",
    "mcp.types",
    "anyio._backends._asyncio",
)
_COLLECT_SUBMODULES = ("horizon.desktop_mcp",)
_COLLECT_DATA = ("pypdf",)
_EXCLUDE_MODULES = (
    "torch",
    "transformers",
    "onnxruntime",
    "numpy",
    "scipy",
    "pandas",
    "litellm",
    "fastapi",
    "horizon.proxy",
    "horizon.providers",
    "horizon.cli",
    "horizon.evals",
    "matplotlib",
    "tkinter",
    # Cloud SDKs, tokenizer and model-hub stacks that the desktop path never imports.
    "botocore",
    "boto3",
    "s3transfer",
    "hf_xet",
    "tokenizers",
    "huggingface_hub",
    "fsspec",
    "tree_sitter",
    "tree_sitter_c_sharp",
    "tree_sitter_yaml",
    "tree_sitter_embedded_template",
    "zstandard",
    "safetensors",
    "aiohttp",
    "websockets",
)


_COPY_METADATA = ("tiktoken", "pypdf", "mcp")


def _tool_names() -> list[str]:
    from horizon.desktop_mcp.contracts import tool_specs

    # The manifest lists the default (source) profile's tools; the compute
    # profile adds more at runtime, which tools_generated declares.
    return [spec["name"] for spec in tool_specs("source")]


def load_template(path: Path = TEMPLATE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def render_manifest(version: str, template: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = json.loads(json.dumps(template if template is not None else load_template()))
    manifest["version"] = version
    return manifest


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def validate_manifest(
    manifest: dict[str, Any],
    onedir: Path | None = None,
    tool_names: list[str] | None = None,
) -> list[str]:
    """Return a list of problems; an empty list means the manifest is acceptable."""
    errors: list[str] = []
    for key in _REQUIRED_TOP_LEVEL:
        if key not in manifest:
            errors.append(f"missing required field: {key}")
    author = manifest.get("author")
    if not isinstance(author, dict) or not author.get("name"):
        errors.append("author.name is required")
    version = manifest.get("version")
    if isinstance(version, str) and not _SEMVER.match(version):
        errors.append(f"version is not semver: {version!r}")

    server = manifest.get("server")
    server = server if isinstance(server, dict) else {}
    for key in _REQUIRED_SERVER:
        if key not in server:
            errors.append(f"missing required field: server.{key}")
    if server.get("type") != "binary":
        errors.append("server.type must be 'binary'")
    mcp_config = server.get("mcp_config")
    mcp_config = mcp_config if isinstance(mcp_config, dict) else {}
    for key in _REQUIRED_MCP_CONFIG:
        if key not in mcp_config:
            errors.append(f"missing required field: server.mcp_config.{key}")
    if not isinstance(mcp_config.get("args", []), list):
        errors.append("server.mcp_config.args must be a list")

    entry_point = server.get("entry_point")
    if isinstance(entry_point, str) and mcp_config.get("command") != "${__dirname}/" + entry_point:
        errors.append("server.mcp_config.command must be ${__dirname}/<entry_point>")

    user_config = manifest.get("user_config")
    user_config = user_config if isinstance(user_config, dict) else {}
    for key, entry in user_config.items():
        if not isinstance(entry, dict):
            errors.append(f"user_config.{key} must be an object")
            continue
        kind = entry.get("type")
        if kind not in _USER_CONFIG_TYPES:
            errors.append(f"user_config.{key}.type is not supported: {kind!r}")
        for field in ("title", "description"):
            if not entry.get(field):
                errors.append(f"user_config.{key}.{field} is required")
        if "multiple" in entry and kind not in _MULTIPLE_TYPES:
            errors.append(f"user_config.{key}.multiple applies only to directory or file")
        if "sensitive" in entry and not isinstance(entry["sensitive"], bool):
            errors.append(f"user_config.{key}.sensitive must be boolean")

    referenced: set[str] = set()
    for text in _strings(mcp_config):
        for variable in _VARIABLE.findall(text):
            if variable == "__dirname":
                continue
            if variable.startswith("user_config."):
                name = variable[len("user_config.") :]
                referenced.add(name)
                if name not in user_config:
                    errors.append(f"${{{variable}}} is not declared in user_config")
            else:
                errors.append(f"unknown variable ${{{variable}}}")
    for key in user_config:
        if key not in referenced:
            errors.append(f"user_config.{key} is declared but never referenced")

    compatibility = manifest.get("compatibility")
    platforms = compatibility.get("platforms") if isinstance(compatibility, dict) else None
    if platforms != ["win32"]:
        errors.append("compatibility.platforms must be ['win32']")

    tools = manifest.get("tools")
    expected = tool_names if tool_names is not None else _tool_names()
    if not isinstance(tools, list) or any(
        not isinstance(tool, dict) or not tool.get("description") for tool in tools
    ):
        errors.append("tools must be a list of objects with name and description")
    else:
        names = [tool.get("name") for tool in tools]
        if names != expected:
            errors.append(f"tools {names} do not match tool_specs('source') {expected}")
    if manifest.get("tools_generated") is not True:
        errors.append("tools_generated must be true: the compute profile adds tools")

    if onedir is not None and isinstance(entry_point, str):
        prefix = "server/"
        if not entry_point.startswith(prefix):
            errors.append("server.entry_point must start with server/")
        else:
            target = Path(onedir) / entry_point[len(prefix) :]
            if not target.is_file():
                errors.append(f"entry_point is missing from build output: {target}")
    return errors


def validate(onedir: Path | None = None) -> list[str]:
    """Validate the checked-in template; check the build output when it exists."""
    if onedir is not None and not Path(onedir).is_dir():
        onedir = None
    return validate_manifest(load_template(), onedir=onedir)


def build() -> Path:
    import PyInstaller.__main__ as pyinstaller

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    arguments = [
        "--onedir",
        "--console",
        "--noconfirm",
        "--clean",
        "--name",
        EXE_NAME,
        "--distpath",
        str(BUILD_DIR / "dist"),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--specpath",
        str(BUILD_DIR),
        "--paths",
        str(REPO_ROOT),
    ]
    for source in sorted(COMPANION_UI.glob("*")):
        if source.is_file():
            arguments += ["--add-data", f"{source}{os.pathsep}{COMPANION_TARGET}"]
    for package in _COLLECT_SUBMODULES:
        arguments += ["--collect-submodules", package]
    for package in _COLLECT_DATA:
        arguments += ["--collect-data", package]
    for module in _HIDDEN_IMPORTS:
        arguments += ["--hidden-import", module]
    # Package metadata lets the frozen build report real library versions
    # (for example the token counter version) instead of "unknown".
    for package in _COPY_METADATA:
        arguments += ["--copy-metadata", package]
    for module in _EXCLUDE_MODULES:
        arguments += ["--exclude-module", module]
    arguments.append(str(ENTRY_SCRIPT))
    pyinstaller.run(arguments)
    if not (ONEDIR / f"{EXE_NAME}.exe").is_file():
        raise SystemExit("build failed: executable was not produced")
    return ONEDIR


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def pack(
    version: str = VERSION,
    onedir: Path = ONEDIR,
    output_dir: Path = DIST_DIR,
) -> Path:
    """Write dist/contextshrink-desktop-<version>.mcpb with deterministic bytes."""
    if not _SEMVER.match(version):
        raise SystemExit(f"version is not semver: {version!r}")
    onedir = Path(onedir)
    manifest = render_manifest(version)
    problems = validate_manifest(manifest, onedir=onedir)
    if problems:
        raise SystemExit("pack refused:\n  " + "\n  ".join(problems))
    manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    entries: dict[str, bytes | Path] = {"manifest.json": manifest_bytes}
    for path in onedir.rglob("*"):
        if path.is_file():
            entries[f"server/{path.relative_to(onedir).as_posix()}"] = path

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{EXE_NAME}-{version}.mcpb"
    temporary = output_dir / f".{target.name}.tmp"
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            source = entries[name]
            data = source if isinstance(source, bytes) else Path(source).read_bytes()
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if name.endswith(".exe") else 0o644) << 16
            archive.writestr(info, data)
    os.replace(temporary, target)
    return target


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _print_problems(problems: list[str]) -> None:
    for problem in problems:
        print(f"invalid: {problem}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build_extension.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="freeze the executable with PyInstaller")
    commands.add_parser("validate", help="check the manifest template and build output")
    for name, help_text in (("pack", "write the .mcpb archive"), ("all", "build, validate, pack")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument(
            "--version", default=VERSION, help=f"extension version (default {VERSION})"
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        onedir = build()
        print(f"build: {onedir}")
        print(f"build size: {directory_size(onedir) / 1_048_576:.1f} MiB")
        return 0
    if args.command == "validate":
        problems = validate(ONEDIR)
        if problems:
            _print_problems(problems)
            return 1
        state = "with build output" if ONEDIR.is_dir() else "no build output present"
        print(f"validate: ok ({state})")
        return 0
    if args.command in ("pack", "all"):
        if args.command == "all":
            build()
        problems = validate(ONEDIR)
        if problems:
            _print_problems(problems)
            return 1
        target = pack(args.version)
        print(f"mcpb: {target}")
        print(f"mcpb size: {target.stat().st_size} bytes")
        print(f"sha256: {sha256_of(target)}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
