"""``horizon code-engine``: run, start, stop and inspect the ContextShrink Code engine.

The license and device modules need ``cryptography``, so they are imported
inside the commands that use them. The CLI must start without it.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

import click

from horizon.cli.main import main
from horizon.code_engine import runtime
from horizon.code_engine.store import home_dir
from horizon.proxy import engine_hooks

# The engine runs the max savings profile, but never changes the model the user
# picked: HORIZON_MODEL_MODERNIZE would serve a superseded model id on its successor.
# These are forced, so a value the user set in the environment does not win.
ENGINE_SAVINGS_ENV = {"HORIZON_SAVINGS": "max", "HORIZON_MODEL_MODERNIZE": "0"}


def force_engine_environment(environ) -> None:
    environ.update(ENGINE_SAVINGS_ENV)


_PORT = click.option(
    "--port",
    default=runtime.DEFAULT_PORT,
    show_default=True,
    type=click.IntRange(1, 65535),
    help="Loopback port the engine listens on.",
)

_STOP_MESSAGES = {
    "stopped": "Engine stopped.",
    "not_running": "Engine is not running.",
    "killed": "Engine did not stop in time and was ended.",
    "still_running": "Engine is still running; end it from Task Manager by its PID.",
}


@main.group(name="code-engine")
def code_engine() -> None:
    """ContextShrink Code engine: a local proxy that compresses only while licensed.

    Without a valid plan note, every request is forwarded unchanged.
    """


@code_engine.command("run")
@_PORT
def run_engine(port: int) -> None:
    """Serve the engine in the foreground. A second instance exits at once."""
    from horizon.code_engine.engine import Engine

    home = home_dir()
    # The starter saved its environment for us (WMI does not pass one on). Apply it first.
    runtime.take_launch_environment(home, os.environ)
    lock = runtime.InstanceLock(home / runtime.LOCK_FILE)
    if not lock.acquire():
        click.echo("ContextShrink engine is already running.")
        return
    engine = None
    code = 0
    try:
        # Set before the proxy reads any flag.
        force_engine_environment(os.environ)
        # A stop left over from an earlier run must not stop this one.
        runtime.consume_stop_request(home)
        engine = Engine(home)
        engine.load_cached_note()
        engine_hooks.install(
            license_allows=engine.allows_compression,
            usage_sink=engine.record_usage,
            health=engine.health,
        )
        engine.start()
        runtime.write_pid(home, os.getpid())
        from horizon.cli.proxy import proxy as proxy_command

        proxy_command.main(
            args=["--host", runtime.HOST, "--port", str(port)],
            prog_name="horizon code-engine run",
            standalone_mode=False,
        )
    except KeyboardInterrupt:
        code = 0  # a stop request: the proxy shut down
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        if engine is not None:
            engine.stop()  # the final usage upload
        engine_hooks.reset()
        runtime.remove_pid(home)
        lock.release()
    # Last: interpreter shutdown can hang on the proxy's background threads.
    runtime.exit_now(code)


@code_engine.command("ensure")
@_PORT
def ensure_engine(port: int) -> None:
    """Start the engine in the background if it is not answering. Always exits 0."""
    try:
        runtime.ensure(home_dir(), port)
    except Exception:  # noqa: BLE001 - a hook must never fail or block Claude Code
        pass


@code_engine.command("stop")
@_PORT
def stop_engine(port: int) -> None:
    """Stop the running engine. Pending usage is uploaded before it exits."""
    outcome = runtime.stop(home_dir(), port)
    click.echo(_STOP_MESSAGES.get(outcome, outcome))
    if outcome == "still_running":
        sys.exit(1)


@code_engine.command("status")
@_PORT
@click.option("--json", "as_json", is_flag=True, help="Emit the status as JSON.")
def status_engine(port: int, as_json: bool) -> None:
    """Show whether the engine runs, the plan state and the device connection."""
    home = home_dir()
    running = runtime.health(port)
    plan, expires_at, connected = _license_status(home)
    report = {
        "running": running is not None,
        "port": port,
        "engine_version": running.get("engine") if running else None,
        "compression_licensed": bool(running and running.get("ok")),
        "plan": plan,
        "license_expires_at": expires_at,
        "device_connected": connected,
        "pid": runtime.read_pid(home),
    }
    if as_json:
        click.echo(json.dumps(report, indent=2))
        return
    click.echo(f"Engine:      {'running' if report['running'] else 'not running'} (port {port})")
    click.echo(f"Device:      {'connected' if connected else 'not connected'}")
    click.echo(f"Plan:        {plan or 'none'}")
    click.echo(f"Compression: {'on' if report['compression_licensed'] else 'off (passthrough)'}")


def _license_status(home) -> tuple[str | None, int | None, bool]:
    """(plan, expiry, device connected) read from the local files. Never raises."""
    from horizon.code_engine.device import MachineIdError, load_device, local_device_hash

    device = load_device(home)
    if device is None:
        return None, None, False
    try:
        from horizon.code_engine.license import read_license

        entitlement = read_license(home, device_hash=local_device_hash(), now=time.time())
    except (ImportError, MachineIdError):
        return None, None, True
    if entitlement is None:
        return None, None, True
    return entitlement.plan, entitlement.expires_at, True
