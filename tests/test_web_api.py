from __future__ import annotations

import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings


class WebApiTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tempdir.cleanup()

    def register(self, email: str = "owner@example.com") -> None:
        response = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "correct horse battery staple"},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_register_create_and_revoke_mcp_key(self) -> None:
        self.register()
        created = self.client.post("/api/mcp-keys", json={"name": "Claude Desktop"})
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        self.assertTrue(payload["secret"].startswith("mcp_"))

        listed = self.client.get("/api/mcp-keys").json()["keys"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("secret", listed[0])
        self.assertEqual(self.client.delete(f"/api/mcp-keys/{listed[0]['id']}").status_code, 204)
        self.assertIsNotNone(self.client.get("/api/mcp-keys").json()["keys"][0]["revoked_at"])

    def test_web_responses_include_security_headers(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_enrollment_registers_agent_and_cannot_be_reused(self) -> None:
        self.register()
        created = self.client.post("/api/enrollment-tokens", json={"name": "GPU Notebook"})
        self.assertEqual(created.status_code, 201, created.text)
        secret = created.json()["secret"]

        hello = {
            "v": 1,
            "type": "hello",
            "enrollment_token": secret,
            "name": "GPU Notebook",
            "description": "模型训练环境",
            "profiles": ["shell.v1"],
        }
        with self.client.websocket_connect("/ws/agent") as websocket:
            websocket.send_json(hello)
            registered = websocket.receive_json()
            self.assertEqual(registered["type"], "registered")
            agents = self.client.get("/api/agents").json()["agents"]
            self.assertEqual(len(agents), 1)
            self.assertTrue(agents[0]["online"])
            self.assertEqual(agents[0]["capabilities"], ["远程 Shell"])

        with self.client.websocket_connect("/ws/agent") as websocket:
            websocket.send_json(hello)
            error = websocket.receive_json()
            self.assertEqual(error["type"], "error")
            self.assertIn("已过期或已被使用", error["error"])

    def test_manual_enrollment_only_issues_token_and_source_command(self) -> None:
        self.register()
        created = self.client.post("/api/enrollment-tokens", json={"name": "CI Manual"})
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        self.assertTrue(payload["secret"].startswith("enroll_"))
        self.assertIn("python agent.py", payload["command"])
        self.assertNotIn("--name", payload["command"])
        self.assertEqual(self.client.get("/api/agent-bundles").json()["bundles"], [])

    def test_agent_is_hidden_from_another_user(self) -> None:
        self.register()
        created = self.client.post("/api/enrollment-tokens", json={"name": "Private Agent"})
        hello = {
            "v": 1,
            "type": "hello",
            "enrollment_token": created.json()["secret"],
            "name": "Private Agent",
            "description": "",
            "profiles": ["shell.v1"],
        }
        with self.client.websocket_connect("/ws/agent") as websocket:
            websocket.send_json(hello)
            websocket.receive_json()
        self.client.post("/api/auth/logout")
        self.register("other@example.com")
        self.assertEqual(self.client.get("/api/agents").json()["agents"], [])

    def test_user_can_build_and_download_python_agent(self) -> None:
        self.register()
        created = self.client.post(
            "/api/agent-bundles",
            json={
                "name": "Built Agent",
                "description": "从工作台组装",
                "runtime": "python",
                "profiles": ["shell.v1"],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        self.assertTrue(payload["secret"].startswith("enroll_"))
        self.assertIn("python run.py", payload["command"])
        token_prefix = payload["secret"][:14]
        token_record = next(
            item
            for item in self.client.get("/api/enrollment-tokens").json()["tokens"]
            if item["prefix"] == token_prefix
        )
        self.assertEqual(token_record["expires_at"], payload["bundle"]["expires_at"])

        download = self.client.get(payload["bundle"]["download_url"])
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.headers["content-type"], "application/zip")
        with zipfile.ZipFile(BytesIO(download.content)) as archive:
            self.assertTrue(any(name.endswith("/remote_agent/profiles/shell_v1.py") for name in archive.namelist()))
            combined = b"\n".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(payload["secret"].encode(), combined)

    def test_bundle_download_is_owner_scoped(self) -> None:
        self.register()
        created = self.client.post(
            "/api/agent-bundles",
            json={"name": "Private Bundle", "runtime": "python", "profiles": ["shell.v1"]},
        ).json()
        self.client.post("/api/auth/logout")
        self.register("bundle-other@example.com")
        response = self.client.get(created["bundle"]["download_url"])
        self.assertEqual(response.status_code, 404)

    def test_builder_rejects_unknown_profile(self) -> None:
        self.register()
        response = self.client.post(
            "/api/agent-bundles",
            json={"name": "Invalid Bundle", "runtime": "python", "profiles": ["unknown.v1"]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("不支持的 Profile", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
