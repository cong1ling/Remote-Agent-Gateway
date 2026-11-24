from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.agent_protocol import CURRENT_AGENT_VERSION, CURRENT_CORE_VERSION
from gateway.app import create_app
from gateway.config import Settings
from gateway.profiles import WORKSPACE_MAX_FILE_BYTES, profile_mcp_tools, public_profile_catalog
from remote_agent.core.limits import MAX_CONCURRENT_REQUESTS_PER_AGENT


SHELL_DECLARATIONS = [
    {
        "id": "shell.v1",
        "runtime": {
            "os": "windows",
            "dialect": "powershell",
            "executable": "powershell.exe",
        },
    }
]


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
                    "protocolVersion": "2025-11-25",
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
        listed_tools = listed.json()["result"]["tools"]
        names = [tool["name"] for tool in listed_tools]
        self.assertEqual(
            names,
            [
                "list_agents",
                "get_agent_capabilities",
                "remote_exec",
                "remote_read_file",
                "remote_write_file",
                "remote_edit_file",
                "remote_get_host_info",
                "remote_get_resources",
                "remote_get_processes",
                "remote_get_network",
            ],
        )
        descriptions = {tool["name"]: tool["description"] for tool in listed_tools}
        schemas = {tool["name"]: tool["inputSchema"] for tool in listed_tools}
        self.assertIn("get_agent_capabilities", descriptions["remote_exec"])
        self.assertIn("shell.v1", descriptions["remote_exec"])
        self.assertIn("mcp_tool", descriptions["remote_exec"])
        self.assertIn("必须先调用", descriptions["get_agent_capabilities"])
        self.assertIn("workspace.v1", descriptions["remote_read_file"])
        self.assertIn("expected_sha256", descriptions["remote_edit_file"])
        directory_schema = schemas["list_agents"]
        self.assertNotIn("filters", directory_schema.get("required", []))
        self.assertEqual(
            set(directory_schema["$defs"]["DeviceFilters"]["properties"]),
            {"platform", "environment", "tags", "capability"},
        )
        write_schema = schemas["remote_write_file"]
        self.assertIn("expected_sha256", write_schema["required"])
        expected_schema = write_schema["properties"]["expected_sha256"]
        self.assertEqual(
            {item.get("type") for item in expected_schema["anyOf"]},
            {"string", "null"},
        )
        mappings = [
            action.get("mcp_tool")
            for profile in public_profile_catalog()
            if not profile["plan_only"]
            for action in profile["actions"]
        ]
        self.assertTrue(mappings)
        self.assertTrue(all(isinstance(mapping, str) and mapping for mapping in mappings))
        self.assertEqual(set(mappings), profile_mcp_tools())
        self.assertTrue(set(mappings).issubset(names))
        self.assertEqual(profile_mcp_tools(plan_only=True), {"create_operation_plan"})
        self.assertNotIn("create_operation_plan", names)

    def test_capability_contract_names_its_mcp_tool(self) -> None:
        enrollment = self.client.post(
            "/api/enrollment-tokens", json={"name": "Capability Agent"}
        ).json()["secret"]
        hello = {
            "v": 2,
            "type": "hello",
            "enrollment_token": enrollment,
            "name": "Capability Agent",
            "description": "",
            "profiles": SHELL_DECLARATIONS,
            "version": {
                "agent_version": CURRENT_AGENT_VERSION,
                "core_version": CURRENT_CORE_VERSION,
                "protocol_version": 2,
                "build_id": "mcp-capability-test",
            },
        }
        headers = {**self.base_headers, "Authorization": f"Bearer {self.secret}"}
        with self.client.websocket_connect("/ws/agent") as websocket:
            websocket.send_json(hello)
            agent_id = websocket.receive_json()["agent_id"]
            response = self.client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "get_agent_capabilities",
                        "arguments": {"agent_id": agent_id},
                    },
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        profile = response.json()["result"]["structuredContent"]["profiles"][0]
        self.assertEqual(profile["id"], "shell.v1")
        self.assertEqual(profile["actions"][0]["mcp_tool"], "remote_exec")
        self.assertEqual(
            response.json()["result"]["structuredContent"]["gateway_limits"][
                "max_concurrent_requests_per_agent"
            ],
            MAX_CONCURRENT_REQUESTS_PER_AGENT,
        )
        self.assertEqual(
            response.json()["result"]["structuredContent"]["agent"]["shell_policy"]["mode"],
            "off",
        )

    def test_device_directory_matches_owned_platform_tags_environment_and_capability(self) -> None:
        windows = self._enroll_directory_agent("Legacy Windows", SHELL_DECLARATIONS)
        workspace = [{
            "id": "workspace.v1",
            "runtime": {"scope": "workdir", "path_format": "relative-posix", "encoding": "utf-8",
                        "max_file_bytes": WORKSPACE_MAX_FILE_BYTES},
        }]
        macos = self._enroll_directory_agent(
            "macOS", workspace, {"device_type": "computer", "platform": "macos", "platform_version": "26.1"},
        )
        unknown = self._enroll_directory_agent("Workspace Legacy", workspace)
        patch_data = {"name": "renamed", "environment": "staging", "tags": ["west", "canary", "west"]}
        for agent_id in (windows, macos):
            response = self.client.patch(f"/api/agents/{agent_id}", json=patch_data)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["agent"]["tags"], ["canary", "west"])
        self.assertEqual(self.client.patch(f"/api/agents/{macos}", json={"name": "macOS"}).status_code, 200)
        filters = {"platform": "macos", "environment": "staging", "tags": ["canary", "west"],
                   "capability": "workspace.v1/read"}
        listed = self.client.get("/api/agents", params=filters)
        self.assertEqual(listed.status_code, 200, listed.text)
        found, = listed.json()["agents"]
        self.assertEqual(found["id"], macos)
        self.assertEqual(found["device"]["platform_version"], "26.1")
        self.assertEqual(found["trust_level"], "owner_enrolled")
        self.assertEqual(self.client.get("/api/agents?platform=windows").json()["agents"][0]["id"], windows)
        self.assertEqual(self.client.get("/api/agents?platform=unknown").json()["agents"][0]["id"], unknown)
        self.assertEqual(self.client.get("/api/agents?environment=prod.*").status_code, 422)
        self.assertEqual(self.client.get("/api/agents?platfrom=windows").status_code, 422)
        self.assertEqual(self.client.get("/api/agents?capability=workspace.v1/invalid").json()["agents"], [])
        self.assertEqual(self.client.get("/api/agents?tags=missing").json()["agents"], [])

        self.client.post("/api/auth/register", json={
            "email": "directory-other@example.com", "password": "other account password",
        }).raise_for_status()
        stranger = self._enroll_directory_agent(
            "Another macOS", workspace, {"device_type": "computer", "platform": "macos"},
        )
        self.client.patch(f"/api/agents/{stranger}", json=patch_data).raise_for_status()
        self.assertEqual(self.client.patch(f"/api/agents/{macos}", json=patch_data).status_code, 404)
        self.assertEqual(
            [agent["id"] for agent in self.client.get("/api/agents", params=filters).json()["agents"]],
            [stranger],
        )
        response = self.client.post(
            "/mcp/",
            headers={**self.base_headers, "Authorization": f"Bearer {self.secret}"},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "list_agents", "arguments": {"filters": filters}}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        mcp_agent, = response.json()["result"]["structuredContent"]["agents"]
        self.assertEqual(mcp_agent["id"], macos)
        self.assertEqual(mcp_agent["device"], found["device"])
        self.client.patch(f"/api/agents/{stranger}", json={
            "name": "cleared", "environment": None, "tags": [],
        }).raise_for_status()
        self.assertEqual(self.client.get("/api/agents", params=filters).json()["agents"], [])

    def _enroll_directory_agent(self, name: str, profiles: list[dict], device: dict | None = None) -> str:
        enrollment = self.client.post("/api/enrollment-tokens", json={"name": name}).raise_for_status().json()["secret"]
        with self.client.websocket_connect("/ws/agent") as websocket:
            websocket.send_json({
                "v": 2, "type": "hello", "enrollment_token": enrollment, "name": name,
                "profiles": profiles, "device": device, "metadata": {"platform": "misleading-string"},
                "version": {"agent_version": CURRENT_AGENT_VERSION, "core_version": CURRENT_CORE_VERSION,
                            "protocol_version": 2, "build_id": "directory-test"},
            })
            return websocket.receive_json()["agent_id"]

    def test_disconnect_and_reconnect_update_device_alerts_without_a_task(self) -> None:
        enrollment = self.client.post("/api/enrollment-tokens", json={"name": "Monitored"}).raise_for_status().json()
        device = {"device_type": "computer", "platform": "windows", "platform_version": "11"}
        hello = {
            "v": 2, "type": "hello", "profiles": SHELL_DECLARATIONS, "device": device,
            "version": {"agent_version": CURRENT_AGENT_VERSION, "core_version": CURRENT_CORE_VERSION,
                        "protocol_version": 2, "build_id": "alerts"},
        }
        with self.client.websocket_connect("/ws/agent") as websocket:
            websocket.send_json({**hello, "enrollment_token": enrollment["secret"], "name": "Monitored"})
            identity = websocket.receive_json()
            self.assertEqual(identity["type"], "registered")
        offline, = self.client.get("/api/alerts?status=open").raise_for_status().json()["alerts"]
        self.assertEqual(offline["category"], "device_offline")
        self.assertIsNone(offline["task_id"])
        self.assertEqual(offline["agent_id"], identity["agent_id"])
        with self.client.websocket_connect("/ws/agent") as websocket:
            websocket.send_json({
                **hello, "agent_id": identity["agent_id"], "credential": identity["credential"],
                "device": {**device, "platform_version": "11.1"},
            })
            self.assertEqual(websocket.receive_json()["type"], "ready")
            changed = self.client.get("/api/alerts").raise_for_status().json()["alerts"]
            self.assertTrue(any(alert["category"] == "device_version_changed" and
                                alert["basis"] == {"components": ["platform"]} for alert in changed))
        resolved = self.client.get(f"/api/alerts/{offline['id']}").raise_for_status().json()["alert"]
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["history"][-1]["source"], "agent.reconnected")
        self.assertIsNone(resolved["history"][-1]["actor_user_id"])
        self.assertEqual(
            1, sum(alert["category"] == "device_offline"
                   for alert in self.client.get("/api/alerts?status=open").json()["alerts"]),
        )

    def test_standard_shell_policy_blocks_before_agent_dispatch(self) -> None:
        enrollment = self.client.post(
            "/api/enrollment-tokens", json={"name": "Protected Agent"}
        ).json()["secret"]
        hello = {
            "v": 2,
            "type": "hello",
            "enrollment_token": enrollment,
            "name": "Protected Agent",
            "description": "",
            "profiles": SHELL_DECLARATIONS,
            "version": {
                "agent_version": CURRENT_AGENT_VERSION,
                "core_version": CURRENT_CORE_VERSION,
                "protocol_version": 2,
                "build_id": "mcp-policy-test",
            },
        }
        headers = {**self.base_headers, "Authorization": f"Bearer {self.secret}"}
        with self.client.websocket_connect("/ws/agent") as websocket:
            websocket.send_json(hello)
            agent_id = websocket.receive_json()["agent_id"]
            configured = self.client.patch(
                f"/api/agents/{agent_id}/shell-policy", json={"mode": "standard"}
            )
            self.assertEqual(configured.status_code, 200, configured.text)
            response = self.client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "remote_exec",
                        "arguments": {
                            "agent_id": agent_id,
                            "command": "Format-Volume -DriveLetter C",
                        },
                    },
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("command_blocked [disk_management]", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
