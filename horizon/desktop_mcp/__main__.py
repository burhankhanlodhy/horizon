"""Command-line entry point for the diagnostic desktop MCP spike."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from .settings import ConfigurationError, load_settings


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m horizon.desktop_mcp")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="serve diagnostic fixtures over MCP stdio")
    serve.add_argument("--config", type=Path)
    # Extension mode: the installed Claude Desktop extension passes these; the
    # defaults are applied in extension.run_extension, so None means "not given".
    serve.add_argument(
        "--extension",
        action="store_true",
        help="run as the installed extension: write settings from flags and ensure a broker",
    )
    serve.add_argument(
        "--approved-roots",
        nargs="*",
        default=None,
        metavar="DIR",
        help="extension: approved folders",
    )
    serve.add_argument(
        "--tool-profile", default=None, help="extension: source (default) or compute"
    )
    serve.add_argument(
        "--artifact-root",
        default=None,
        metavar="DIR",
        help="extension: artifact folder (empty for none)",
    )
    serve.add_argument("--broker-id", default=None, help="extension: broker identity")

    broker = commands.add_parser(
        "broker", help="serve approved local sources over the Windows named pipe"
    )
    broker.add_argument("--config", type=Path, required=True)

    companion = commands.add_parser(
        "companion",
        help="open the local graphical request-preparation companion",
        description=(
            "Prepare a request locally in a browser. It does not contact a model, "
            "send a message, or change Claude Desktop configuration."
        ),
    )
    companion.add_argument(
        "--config", type=Path, help="explicit local MCP config for existing broker reads"
    )
    companion.add_argument(
        "--staging-root",
        type=Path,
        help="one exact approved root from --config; enables session-scoped pasted-context import",
    )
    companion.add_argument(
        "--no-browser", action="store_true", help="print the local URL without opening a browser"
    )

    doctor = commands.add_parser("doctor", help="write a local runtime capability report")
    doctor.add_argument("--output", type=Path)

    probe = commands.add_parser(
        "probe",
        help="run the local MCP SDK stdio probe (does not verify Claude Desktop)",
        description="Exercise the local MCP SDK stdio path; this does not verify Claude Desktop compatibility.",
    )
    probe.add_argument("--output", type=Path)

    commands.add_parser(
        "prepare",
        help="prepare a local request packet for manual review and copying",
        description="Prepare locally; this command never sends to Claude or uses the clipboard.",
    )
    return parser


_EXTENSION_FLAGS = ("approved_roots", "tool_profile", "artifact_root", "broker_id")


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "prepare":
        from .preparation_cli import main as prepare_main

        return prepare_main(values[1:])
    if values and values[0] == "worker":
        # Passed verbatim: the frozen broker re-enters the worker through this
        # subcommand, and its arguments belong to backend_worker.main.
        from .backend_worker import main as worker_main

        return worker_main(values[1:])
    args = _parser().parse_args(values)
    if args.command == "broker":
        from .broker import BrokerService
        from .broker_transport import BrokerTransportError

        stop_event = threading.Event()
        try:
            settings = load_settings(args.config)
            if settings.profile != "local":
                raise ConfigurationError("local_profile_required")
            service = BrokerService(args.config)
            try:
                signal.signal(signal.SIGINT, lambda *_: stop_event.set())
                signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
                if hasattr(signal, "SIGBREAK"):
                    # Ctrl+Break would otherwise end the broker without cleanup.
                    signal.signal(signal.SIGBREAK, lambda *_: stop_event.set())
            except (ValueError, OSError):
                pass
            service.serve(stop_event=stop_event)
        except ConfigurationError as exc:
            print(
                json.dumps({"error": exc.code, "fields": list(exc.fields)}, separators=(",", ":")),
                file=sys.stderr,
            )
            return 2
        except BrokerTransportError as exc:
            print(json.dumps({"error": exc.code}, separators=(",", ":")), file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            code = getattr(exc, "code", "broker_start_failed")
            if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 64:
                code = "broker_start_failed"
            print(json.dumps({"error": code}, separators=(",", ":")), file=sys.stderr)
            return 2
        return 0

    if args.command == "companion":
        from .companion import CompanionError, serve_companion

        try:
            return serve_companion(
                config=args.config,
                staging_root=args.staging_root,
                open_browser=not args.no_browser,
            )
        except (CompanionError, ConfigurationError) as exc:
            code = getattr(exc, "code", "companion_start_failed")
            print(json.dumps({"error": code}, separators=(",", ":")), file=sys.stderr)
            return 2

    if args.command == "serve":
        extension_given = [name for name in _EXTENSION_FLAGS if getattr(args, name) is not None]
        if args.extension and args.config is not None:
            print('{"error":"config_and_extension_conflict"}', file=sys.stderr)
            return 2
        if not args.extension and extension_given:
            print('{"error":"extension_flags_require_extension"}', file=sys.stderr)
            return 2
        try:
            from .server import serve_stdio
        except ImportError:
            print('{"error":"mcp_sdk_unavailable"}', file=sys.stderr)
            return 2

        try:
            if args.extension:
                from .extension import run_extension

                run_extension(args)
            else:
                settings = load_settings(args.config)
                asyncio.run(serve_stdio(settings))
        except ConfigurationError as exc:
            print(
                json.dumps({"error": exc.code, "fields": list(exc.fields)}, separators=(",", ":")),
                file=sys.stderr,
            )
            return 2
        except (BrokenPipeError, EOFError):
            return 0
        except Exception as exc:
            code = getattr(exc, "code", "server_start_failed")
            if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 64:
                code = "server_start_failed"
            print(json.dumps({"error": code}, separators=(",", ":")), file=sys.stderr)
            return 2
        return 0

    from .capabilities import run_doctor, run_probe

    try:
        if args.command == "doctor":
            result = run_doctor(args.output)
        elif args.command == "probe":
            result = asyncio.run(run_probe(args.output))
        else:  # pragma: no cover - argparse constrains the command names
            return 2
    except OSError:
        print('{"error":"report_write_failed"}', file=sys.stderr)
        return 2
    # Doctor/probe are intentionally one-shot JSON commands; serve writes only
    # MCP protocol frames to stdout.
    _print_json(result)
    if result.get("output_error"):
        return 2
    if args.command == "doctor":
        return 0 if result.get("status") == "ready" else 2
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
