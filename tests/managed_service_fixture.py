from __future__ import annotations

import argparse
import hmac
import http.client
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ManagedServiceFixture:
    def __init__(self, app_config: Path):
        self.app_config = app_config
        self.credential = secrets.token_urlsafe(36)
        self.lock = threading.Lock()
        self.generation = ""
        self.events: list[dict[str, Any]] = []
        self.restart_count = 0
        self.worker: subprocess.Popen[bytes] | None = None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.worker_port = listener.getsockname()[1]

        fixture = self

        class Supervisor(QuietHandler):
            def _authorized(self) -> bool:
                supplied = self.headers.get("Authorization", "").encode("latin1")
                return hmac.compare_digest(supplied, f"Bearer {fixture.credential}".encode("ascii"))

            def do_GET(self) -> None:
                if not self._authorized() or self.path != "/rag/v1/status":
                    self.send_json(404, {})
                    return
                with fixture.lock:
                    state = fixture._state()
                    self.send_json(200, {
                        "generation": fixture.generation, "state": state,
                        "recent_events": fixture.events[-8:],
                        "ignored_backend_field": fixture.credential,
                    })

            def do_POST(self) -> None:
                if not self._authorized() or self.path != "/rag/v1/restart":
                    self.send_json(404, {})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 1 <= length <= 1024:
                        raise ValueError
                    request = json.loads(self.rfile.read(length))
                    if not isinstance(request, dict) or set(request) != {"expected_generation"}:
                        raise ValueError
                except ValueError:
                    self.send_json(400, {})
                    return
                with fixture.lock:
                    if (
                        request["expected_generation"] != fixture.generation
                        or fixture._state() not in {"degraded", "stopped"}
                    ):
                        self.send_json(409, {})
                        return
                    fixture.restart_count += 1
                    fixture._event("restart_requested")
                    fixture._stop_worker()
                    fixture._start_worker()
                    self.send_json(202, {})

        self.supervisor = ThreadingHTTPServer(("127.0.0.1", 0), Supervisor)
        self.supervisor_thread = threading.Thread(target=self.supervisor.serve_forever, daemon=True)

    @property
    def worker_pid(self) -> int | None:
        return self.worker.pid if self.worker is not None else None

    def __enter__(self) -> ManagedServiceFixture:
        try:
            self._start_worker()
            self.supervisor_thread.start()
        except BaseException:
            self._stop_worker()
            self.supervisor.server_close()
            raise
        return self

    def __exit__(self, *args: object) -> None:
        self.supervisor.shutdown()
        self.supervisor.server_close()
        with self.lock:
            self._stop_worker()
        self.supervisor_thread.join(3)

    def write_agent_config(self, path: Path) -> None:
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"services": [{
                "id": "demo", "port": self.supervisor.server_port, "credential": self.credential,
            }]}, stream)

    def _state(self) -> str:
        if self.worker is None or self.worker.poll() is not None:
            return "stopped"
        connection = http.client.HTTPConnection("127.0.0.1", self.worker_port, timeout=0.3)
        try:
            connection.request("GET", "/ready")
            with connection.getresponse() as response:
                return "ready" if response.status == 200 else "degraded"
        except (OSError, http.client.HTTPException):
            return "starting"
        finally:
            connection.close()

    def _event(self, code: str) -> None:
        self.events.append({"code": code, "observed_at": int(time.time())})

    def _start_worker(self) -> None:
        self.generation = uuid.uuid4().hex
        self.worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker", str(self.app_config), str(self.worker_port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            state = self._state()
            if state in {"ready", "degraded"}:
                self._event("ready" if state == "ready" else "config_invalid")
                return
            if state == "stopped":
                break
            time.sleep(0.05)
        self._event("process_exited")

    def _stop_worker(self) -> None:
        if self.worker is not None and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(3)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait(3)
        self.worker = None


def run_worker(config: Path, port: int) -> None:
    healthy = config.read_text(encoding="utf-8").strip() == "mode=ready"

    class Application(QuietHandler):
        def do_GET(self) -> None:
            if self.path == "/ready":
                self.send_json(200 if healthy else 503, {"ready": healthy})
            else:
                self.send_json(404, {})

    with ThreadingHTTPServer(("127.0.0.1", port), Application) as app:
        app.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an isolated RAG managed-service acceptance fixture")
    parser.add_argument("mode", choices=("supervisor", "worker"))
    parser.add_argument("app_config", type=Path)
    parser.add_argument("supervisor_config_or_port")
    arguments = parser.parse_args()
    if arguments.mode == "worker":
        run_worker(arguments.app_config, int(arguments.supervisor_config_or_port))
        return
    with ManagedServiceFixture(arguments.app_config.resolve()) as fixture:
        fixture.write_agent_config(Path(arguments.supervisor_config_or_port))
        print(f"Registered fixture demo; application readiness at http://127.0.0.1:{fixture.worker_port}/ready", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
