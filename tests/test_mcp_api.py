from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings


class McpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        settings = Settings(
            host="127.0.0.1",
            port=8000,
            public_url="http://testserver",
            data_dir=root,
            database_path=root / "test.db",
            session_days=1,
            enrollment_minutes=30,
            max_command_timeout=3600,
            secure_cookies=False,
        )
        self.client_context = TestClient(create_app(settings))
        self.client = self.client_context.__enter__()
        self.client.post(
            "/api/auth/register",
            json={"email": "mcp@example.com", "password": "correct horse battery staple"},
        )
        created = self.client.post("/api/mcp-keys", json={"name": "MCP Test"})
        self.secret = created.json()["secret"]
        self.base_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tempdir.cleanup()

    def test_mcp_rejects_missing_bearer_key(self) -> None:
        response = self.client.post(
            "/mcp/",
            headers=self.base_headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        self.assertEqual(response.status_code, 401)

    def test_authenticated_mcp_lists_fixed_tools(self) -> None:
        headers = {**self.base_headers, "Authorization": f"Bearer {self.secret}"}
        initialized = self.client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        self.assertEqual(initialized.status_code, 200, initialized.text)

        listed = self.client.post(
            "/mcp/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        names = [tool["name"] for tool in listed.json()["result"]["tools"]]
        self.assertEqual(names, ["list_agents", "get_agent_capabilities", "remote_exec"])


if __name__ == "__main__":
    unittest.main()
