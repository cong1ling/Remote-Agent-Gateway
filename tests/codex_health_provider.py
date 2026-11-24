from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HEALTH_TOOLS = ("get_host_info", "get_resources", "get_processes", "get_network")


class HealthProvider(ThreadingHTTPServer):
    def __init__(self, home: Path):
        super().__init__(("127.0.0.1", 0), HealthProviderHandler)
        self.requests: list[dict[str, Any]] = []
        self.mode = "complete"
        self.request_started = threading.Event()
        self.release = threading.Event()
        self.complete_from = 0
        self.step_paused = threading.Event()
        self.step_release = threading.Event()
        home.mkdir()
        (home / "config.toml").write_text(
            'model_provider = "health_fixture"\n'
            'model = "gpt-5.4"\n'
            'cli_auth_credentials_store = "file"\n'
            '[model_providers.health_fixture]\n'
            'name = "Health fixture"\n'
            f'base_url = "http://127.0.0.1:{self.server_port}/v1"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )

    def __enter__(self) -> HealthProvider:
        self.worker = threading.Thread(target=self.serve_forever, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.step_release.set()
        self.shutdown()
        self.worker.join(timeout=5)
        self.server_close()


class HealthProviderHandler(BaseHTTPRequestHandler):
    server: HealthProvider

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        index = len(self.server.requests)
        self.server.requests.append(request)
        self.server.request_started.set()
        if self.server.mode == "wait":
            self.server.release.wait(timeout=30)
        tool_index = index - self.server.complete_from
        if self.server.mode == "complete_with_pause" and tool_index == 1:
            self.server.step_paused.set()
            self.server.step_release.wait(timeout=30)
        if self.server.mode in {"complete", "complete_with_pause"} and 0 <= tool_index < len(HEALTH_TOOLS):
            item = {
                "type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}",
                "name": HEALTH_TOOLS[tool_index], "arguments": "{}", "status": "completed",
            }
        else:
            item = {
                "type": "message", "id": f"msg_{index}", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "fixture result", "annotations": []}],
            }
        response = {"id": f"resp_{index}", "model": request["model"], "output": [item]}
        events = [
            {"type": "response.created", "response": {**response, "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
