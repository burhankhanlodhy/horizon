"""One-shot, user-operated local request preparation CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .preparation import PreparationError, PreparedPacket, ReferenceSource, prepare_packet
from .settings import ConfigurationError, load_settings
from .source_access import SourceAccessError, SourceReader

_MAX_INLINE_BYTES = 8 * 1024 * 1024
_MAX_REFERENCE_BYTES = 8 * 1024 * 1024
_PAGE_TOKEN_BUDGET = 8000
_SOURCE_ID_RE = re.compile(r"^cs1_[0-9a-f]{32}$")


class PreparationCliError(ValueError):
    """Sanitized one-shot CLI failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contextshrink-prepare",
        description=(
            "Prepare a local JSON packet for manual review and copying into an existing chat. "
            "This command does not contact Claude, use the clipboard, or send a message."
        ),
        epilog=(
            "Preview packet contents locally with --show-packet. Export only with an explicit "
            "--output PATH --confirm (with --confirm last); existing files require --overwrite."
        ),
    )
    parser.add_argument(
        "--request-file", required=True, type=Path, help="explicit UTF-8 request file"
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("passthrough", "compact", "focused", "reference", "reference_only"),
        help="passthrough/compact use explicit text; source modes use an existing broker handle",
    )
    parser.add_argument("--context-file", type=Path, help="explicit UTF-8 supporting-context file")
    parser.add_argument("--source-id", help="existing broker-issued cs1_ handle")
    parser.add_argument("--config", type=Path, help="local MCP config, used only with --source-id")
    parser.add_argument("--query", help="required lexical query for focused mode")
    parser.add_argument("--packet-byte-budget", type=int, default=65536)
    parser.add_argument("--packet-token-budget", type=int, default=8000)
    parser.add_argument(
        "--show-packet",
        action="store_true",
        help="include the escaped packet JSON in local preview output",
    )
    parser.add_argument(
        "--output", type=Path, help="packet file destination; requires --confirm to write"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output file (requires --confirm)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="confirm the final export step; place this flag last in the command",
    )
    return parser


def _read_explicit_text(path: Path, *, max_bytes: int) -> str:
    """Read one user-selected supported text file through the bounded source reader."""

    try:
        selected = path if path.is_absolute() else Path.cwd() / path
        if selected.name in {"", ".", ".."}:
            raise SourceAccessError("source_unapproved")
        reader = SourceReader((selected.parent,), max_source_bytes=max_bytes)
        return reader.read(str(selected)).text
    except SourceAccessError as exc:
        raise PreparationCliError(exc.code) from None
    except (OSError, RuntimeError, ValueError, TypeError):
        raise PreparationCliError("input_unreadable") from None


def _reference_from_broker(
    source_id: str, config: Path, *, reference_only: bool = False
) -> ReferenceSource:
    if (
        not isinstance(source_id, str)
        or len(source_id) != 36
        or not _SOURCE_ID_RE.fullmatch(source_id)
    ):
        raise PreparationCliError("invalid_source_id")
    try:
        settings = load_settings(config)
        if settings.profile != "local":
            raise PreparationCliError("local_profile_required")
        from .broker import BrokerClient

        client = BrokerClient(settings)
        payload = client.open(
            source_id,
            mode="overview" if reference_only else "exact",
            result_token_budget=min(_PAGE_TOKEN_BUDGET, settings.result_token_budget),
        )
    except PreparationCliError:
        raise
    except ConfigurationError as exc:
        raise PreparationCliError(exc.code) from None
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 64:
            raise PreparationCliError(code) from None
        raise PreparationCliError("source_unavailable") from None

    if not isinstance(payload, dict):
        raise PreparationCliError("source_unavailable")
    if payload.get("source_id") != source_id:
        raise PreparationCliError("source_version_mismatch")
    version = payload.get("version")
    kind = payload.get("kind")
    text = payload.get("text", "" if reference_only else None)
    selected_lines = payload.get("selected_lines")
    coverage = payload.get("coverage")
    if (
        not isinstance(version, str)
        or not isinstance(kind, str)
        or not isinstance(text, str)
        or not isinstance(coverage, str)
        or (selected_lines is not None and not isinstance(selected_lines, dict))
    ):
        raise PreparationCliError("source_unavailable")

    # This CLI deliberately prepares the first bounded exact page. The packet
    # retains the existing handle/version for exact recovery in the chat.
    if reference_only:
        coverage = "metadata only; no source excerpt loaded; exact recovery uses existing handle"
    elif payload.get("more_available") is True:
        coverage = f"{coverage}; first bounded exact page only; use the existing handle for remaining lines"
    if selected_lines is None:
        selected_lines = {"start": 1, "end": 1}
    start_line = selected_lines.get("start", 1)
    if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
        raise PreparationCliError("source_unavailable")
    if not reference_only and len(text.encode("utf-8")) > _MAX_REFERENCE_BYTES:
        raise PreparationCliError("source_too_large")
    name = f"Approved source ({kind})"
    return ReferenceSource(
        source_id=source_id,
        version=version,
        kind=kind,
        name=name,
        coverage=coverage,
        evidence_text=text,
        selected_start_line=start_line,
    )


def _prepare(args: argparse.Namespace) -> PreparedPacket:
    request = _read_explicit_text(args.request_file, max_bytes=_MAX_INLINE_BYTES)
    if args.mode in {"passthrough", "compact"}:
        if args.source_id is not None or args.config is not None or args.query is not None:
            raise PreparationCliError("unexpected_source_options")
        context = (
            _read_explicit_text(args.context_file, max_bytes=_MAX_INLINE_BYTES)
            if args.context_file is not None
            else ""
        )
        return prepare_packet(
            request,
            context,
            mode=args.mode,
            packet_byte_budget=args.packet_byte_budget,
            packet_token_budget=args.packet_token_budget,
            max_input_bytes=_MAX_INLINE_BYTES,
        )

    if args.context_file is not None:
        raise PreparationCliError("unexpected_context_file")
    if not args.source_id or args.config is None:
        raise PreparationCliError("source_id_and_config_required")
    if args.mode == "focused" and (args.query is None or not args.query.strip()):
        raise PreparationCliError("query_required")
    if args.mode in {"reference", "reference_only"} and args.query is not None:
        raise PreparationCliError("unexpected_query")
    source = _reference_from_broker(
        args.source_id, args.config, reference_only=args.mode == "reference_only"
    )
    return prepare_packet(
        request,
        mode=args.mode,
        source=source,
        query=args.query,
        packet_byte_budget=args.packet_byte_budget,
        packet_token_budget=args.packet_token_budget,
        max_input_bytes=_MAX_INLINE_BYTES,
    )


def _preview(packet: PreparedPacket, *, show_packet: bool) -> dict[str, Any]:
    raw = packet.packet_json.encode("utf-8")
    result = {
        "status": "preview_ready",
        "mode": packet.mode,
        "fidelity": packet.fidelity,
        "coverage": packet.coverage,
        "measurements": packet.measurements,
        "provenance": packet.provenance,
        "fallback_reason": packet.fallback_reason,
        "packet_utf8_bytes": len(raw),
        "packet_sha256": hashlib.sha256(raw).hexdigest(),
        "packet_written": False,
        "copy_instructions": "Review the packet, then copy the entire JSON packet into your existing chat manually.",
    }
    if show_packet:
        result["packet_json"] = packet.packet_json
    return result


def _write_packet(path: Path, packet_json: str, *, overwrite: bool) -> None:
    destination = path if path.is_absolute() else Path.cwd() / path
    data = packet_json.encode("utf-8")
    descriptor: int | None = None
    temporary: Path | None = None
    created_identity: tuple[int, int] | None = None
    written = False
    try:
        if overwrite:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            written = True
        else:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            info = os.fstat(descriptor)
            created_identity = (info.st_dev, info.st_ino)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            written = True
    except FileExistsError:
        raise PreparationCliError("output_exists") from None
    except (OSError, ValueError, TypeError):
        raise PreparationCliError("output_write_failed") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if not written and created_identity is not None:
            try:
                info = os.stat(destination, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == created_identity:
                    destination.unlink()
            except OSError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if "--confirm" in values and values[-1] != "--confirm":
        parser.error("--confirm must be the last command-line flag")
    args = parser.parse_args(values)
    if args.overwrite and args.output is None:
        parser.error("--overwrite requires --output")
    if args.confirm and args.output is None:
        parser.error("--confirm requires --output")
    if args.output is not None and not args.confirm:
        parser.error("--output requires --confirm before a packet is written")
    try:
        prepared = _prepare(args)
        result = _preview(prepared, show_packet=args.show_packet)
        if args.confirm:
            destination = args.output if args.output.is_absolute() else Path.cwd() / args.output
            input_paths = [args.request_file]
            if args.context_file is not None:
                input_paths.append(args.context_file)
            target_key = os.path.normcase(str(destination.resolve(strict=False)))
            if any(
                os.path.normcase(str(path.resolve(strict=False))) == target_key
                for path in input_paths
            ):
                raise PreparationCliError("output_conflicts_with_input")
            _write_packet(args.output, prepared.packet_json, overwrite=args.overwrite)
            result["status"] = "packet_exported"
            result["packet_written"] = True
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (PreparationError, PreparationCliError) as exc:
        print(json.dumps({"error": exc.code}, separators=(",", ":")), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError):
        print('{"error":"preparation_failed"}', file=sys.stderr)
        return 2


__all__ = ["main"]
