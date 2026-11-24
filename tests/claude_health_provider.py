from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from remote_agent.runtimes.health import OBSERVATION_TOOLS


class ClaudeProvider(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), ClaudeProviderHandler)
        self.requests: list[dict[str, Any]] = []
        self.mode = "complete"
        self.request_started = threading.Event()
        self.release = threading.Event()

    def __enter__(self) -> ClaudeProvider:
        self.worker = threading.Thread(target=self.serve_forever, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.shutdown()
        self.worker.join(timeout=5)
        self.server_close()


class ClaudeProviderHandler(BaseHTTPRequestHandler):
    server: ClaudeProvider

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/v1/messages":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append({"body": request, "api_key": self.headers.get("x-api-key")})
        self.server.request_started.set()
        mode = self.server.mode
        if mode == "wait":
            self.server.release.wait(timeout=30)
        actions = list(reversed(OBSERVATION_TOOLS))
        if mode == "unknown_action":
            actions[-1] = "outside_health_catalog"
        message = {
            "id": "msg_fixture", "type": "message", "role": "assistant", "model": request["model"],
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        events = [
            ("message_start", {"message": message}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {
                "index": 0, "delta": {"type": "text_delta", "text": json.dumps({"actions": actions})},
            }),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 8}}),
            ("message_stop", {}),
        ]
        payload = "".join(
            f"event: {name}\ndata: {json.dumps({'type': name, **body})}\n\n" for name, body in events
        ).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass
