"""The engine's cycle in a real process: WMI start, pythonw.exe -I -s, a fake API. Windows only.

The engine is created the way the shipped client creates it (runtime.py's WMI launcher,
pythonw.exe -I -s -X utf8). The test changes only the plan-note verify key, because the
fake API signs notes with a key the test generates, and the intervals, which are shortened
through the test-only CONTEXTSHRINK_TEST_* variables. Every path is inside tmp_path, and
the engine is ended by its own health check and PID file (runtime.stop), never by name.
"""

from __future__ import annotations

import base64
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="WMI start is Windows-only")

LAUNCHER = """\
import json
import os
import sys
from pathlib import Path

cfg = json.loads((Path(__file__).resolve().parent / "cfg.json").read_text(encoding="utf-8"))
os.environ["ANTHROPIC_TARGET_API_URL"] = cfg["upstream"]
import horizon.code_engine.license as _license  # noqa: E402

_license.CODE_ENTITLEMENT_PUBLIC_KEY_B64 = cfg["pubkey"]
from horizon.cli.main import main  # noqa: E402

main(args=sys.argv[1:], prog_name="horizon", standalone_mode=True)
"""

MACHINE = "live-cycle-test-machine"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:  # the engine closes sockets on stop
        pass


def _serve(handler) -> ThreadingHTTPServer:
    server = _QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_cycle_refreshes_and_uploads_in_the_real_launched_engine(tmp_path, monkeypatch) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from horizon.code_engine import runtime
    from horizon.code_engine.device import device_hash

    signing = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        signing.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    device_hash_value = device_hash(MACHINE)
    seen: list[str] = []
    lock = threading.Lock()

    class Api(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            with lock:
                seen.append(self.path)
            if self.path == "/code/entitlement":
                now = int(time.time())
                payload = json.dumps({
                    "v": 1, "iss": "contextshrink-code", "sub": "u", "dev": "d",
                    "dh": device_hash_value, "plan": "trial", "iat": now,
                    "exp": now + 72 * 3600, "until": now + 7 * 86400,
                }, separators=(",", ":")).encode()
                reply = {"token": _b64u(payload) + "." + _b64u(signing.sign(payload)), "plan": "trial"}
            else:
                reply = {"ok": True}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            data = json.dumps({
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 5},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    api = _serve(Api)
    upstream = _serve(Upstream)
    home = tmp_path / "home"
    home.mkdir()
    launcher_dir = tmp_path / "launcher"
    launcher_dir.mkdir()
    (launcher_dir / "launch_engine.py").write_text(LAUNCHER, encoding="utf-8")
    (launcher_dir / "cfg.json").write_text(json.dumps({
        "upstream": f"http://127.0.0.1:{upstream.server_address[1]}", "pubkey": public_b64,
    }), encoding="utf-8")
    (home / "device.json").write_text(json.dumps({
        "version": 1,
        "api_url": f"http://127.0.0.1:{api.server_address[1]}",
        "device_id": "11111111-2222-3333-4444-555555555555",
        "device_key": "cs_dev_live_test",
        "device_hash": device_hash_value,
        "tools": ["claude"],
        "created_at": "2026-10-23T00:00:00Z",
    }), encoding="utf-8")

    # The test's own environment is isolated by conftest; the launched engine inherits the
    # allowed profile variables through the saved launch environment.
    monkeypatch.setenv("CONTEXTSHRINK_MACHINE_ID", MACHINE)
    monkeypatch.setenv("CONTEXTSHRINK_TEST_PLAN_REFRESH_SECONDS", "1")
    monkeypatch.setenv("CONTEXTSHRINK_TEST_USAGE_DEBOUNCE_SECONDS", "0.5")
    monkeypatch.setenv("CONTEXTSHRINK_TEST_CYCLE_TICK_SECONDS", "0.2")
    monkeypatch.setenv("CONTEXTSHRINK_TEST_USAGE_FLUSH_SECONDS", "900")

    port = _free_port()
    pythonw = str(Path(sys.executable).with_name("pythonw.exe"))
    argv = [pythonw, "-I", "-s", "-X", "utf8", str(launcher_dir / "launch_engine.py"),
            "code-engine", "run", "--port", str(port), "--home", str(home)]
    try:
        runtime._start_with_wmi(argv, home, runtime._wmi_create)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and runtime.health(port) is None:
            time.sleep(0.2)
        assert runtime.health(port) is not None, "the launched engine never answered"

        import urllib.request

        # Two different prompts: an identical repeat is served from the proxy's response
        # cache without a model call, and a cache hit is not a usage request.
        for turn in range(2):
            body = {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": [
                {"role": "user", "content": f"Summarize build log number {turn}."},
            ]}
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/messages", data=json.dumps(body).encode(),
                headers={"content-type": "application/json", "x-api-key": "sk-ant-test",
                         "anthropic-version": "2023-06-01"}, method="POST")
            with urllib.request.urlopen(request, timeout=60) as response:
                assert response.status == 200
                response.read()
            time.sleep(3)
        time.sleep(4)

        with lock:
            refreshes = seen.count("/code/entitlement")
            uploads = seen.count("/code/usage")
        assert refreshes >= 3, f"only {refreshes} plan refreshes arrived"
        assert uploads >= 2, f"only {uploads} usage uploads arrived"
    finally:
        runtime.stop(home, port)
        api.shutdown()
        upstream.shutdown()
