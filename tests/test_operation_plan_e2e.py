from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from gateway.database import Database
from remote_agent.profiles.common.planned_action import digest_json
from tests import test_e2e_gateway_agent as gateway_e2e, test_health_check_e2e as health_e2e


class OperationPlanE2ETests(unittest.TestCase):
    def test_two_real_agents_execute_only_the_approved_plan(self) -> None:
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
            port = health_e2e.HealthCheckE2ETests._free_port()
            base = f"http://127.0.0.1:{port}"
            database = root / "data" / "gateway.db"
            env = {
                **os.environ,
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_PORT": str(port),
                "GATEWAY_PUBLIC_URL": base,
                "GATEWAY_DATA_DIR": str(database.parent),
                "GATEWAY_DATABASE": str(database),
                "GATEWAY_SECURE_COOKIES": "false",
                "NO_PROXY": "127.0.0.1,localhost",
            }
            client = cleanup.enter_context(httpx.Client(trust_env=False, timeout=10, base_url=base))
            stranger = cleanup.enter_context(httpx.Client(trust_env=False, timeout=10, base_url=base))

            def launch(label: str, args: list[str], cwd: Path) -> tuple[subprocess.Popen, Path]:
                log = root / f"{label}.log"
                with log.open("wb") as output:
                    process = subprocess.Popen(args, env=env, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
                cleanup.callback(health_e2e.HealthCheckE2ETests._stop, process)
                return process, log

            gateway, gateway_log = launch("gateway", [sys.executable, "-m", "gateway"], Path(__file__).resolve().parents[1])
            for _ in range(100):
                if gateway.poll() is not None:
                    self.fail(gateway_log.read_text(errors="replace"))
                try:
                    if client.get("/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                self.fail("Gateway did not become healthy")
            for account, email in ((client, "owner@example.test"), (stranger, "stranger@example.test")):
                account.post(
                    "/api/auth/register", json={"email": email, "password": "test-only passphrase 42"},
                ).raise_for_status()

            def enroll(label: str, profiles: list[str]) -> tuple[str, Path]:
                bundle = client.post(
                    "/api/agent-bundles", json={"name": label, "runtime": "python", "profiles": profiles},
                ).raise_for_status().json()
                archive = client.get(bundle["bundle"]["download_url"]).raise_for_status().content
                directory = root / label
                workdir = directory / "work"
                workdir.mkdir(parents=True)
                with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                    token = bundle["secret"].encode()
                    self.assertFalse(any(token in zipped.read(name) for name in zipped.namelist()))
                    zipped.extractall(directory / "agent")
                run = next(directory.rglob("run.py"))
                process, log = launch(label, [
                    sys.executable, str(run), "--gateway", f"ws://127.0.0.1:{port}/ws/agent",
                    "--token", bundle["secret"], "--workdir", str(workdir),
                ], run.parent)
                for _ in range(100):
                    if process.poll() is not None:
                        self.fail(log.read_text(errors="replace"))
                    agents = client.get("/api/agents").raise_for_status().json()["agents"]
                    for agent in agents:
                        if agent["name"] == label and agent["online"]:
                            self.assertTrue(agent["features"]["planned_actions"])
                            return agent["id"], workdir
                    time.sleep(0.1)
                self.fail("Agent did not come online")

            writer, workdir = enroll("writer", ["workspace.v1", "system_observe.v1"])
            reviewer, _ = enroll("reviewer", ["shell.v1"])
            settings_file = workdir / "service.ini"
            original = b"level=notice\n"
            settings_file.write_bytes(original)
            before_hash = hashlib.sha256(original).hexdigest()

            def draft(level: str) -> dict[str, Any]:
                return {
                    "objective": "Apply and verify a bounded configuration change",
                    "steps": [
                        {"key": "inspect", "agent_id": writer, "operation": "workspace.hash",
                         "params": {"path": "service.ini"}},
                        {"key": "repair", "agent_id": writer, "operation": "workspace.edit", "depends_on": ["inspect"],
                         "params": {"path": "service.ini", "old_text": "level=notice", "new_text": f"level={level}",
                                    "expected_sha256": before_hash}},
                        {"key": "confirm", "agent_id": reviewer, "operation": "shell.version", "depends_on": ["repair"],
                         "params": {"tool": "git"}},
                    ],
                }

            def submit_plan(level: str) -> dict[str, Any]:
                return client.post("/api/task-plans", json=draft(level)).raise_for_status().json()["task"]

            task = submit_plan("warning")
            endpoint = f"/api/tasks/{task['id']}"
            first = {"plan_hash": task["plan"]["hash"]}
            self.assertEqual(task["status"], "waiting_approval")
            self.assertEqual(task["plan"]["max_level"], "L2")
            self.assertEqual(client.post(f"{endpoint}/execute", json=first).status_code, 409)
            self.assertEqual(stranger.post(f"{endpoint}/approval", json={**first, "decision": "approve"}).status_code, 404)
            self.assertEqual(stranger.get(f"{endpoint}/events").status_code, 404)
            self.assertEqual(stranger.get(f"{endpoint}/events/live").status_code, 404)
            self.assertEqual(settings_file.read_bytes(), original)
            approved = client.post(f"{endpoint}/approval", json={**first, "decision": "approve"}).raise_for_status().json()
            self.assertTrue(approved["task"]["approval"]["approved_scope"]["single_use"])
            self.assertEqual(settings_file.read_bytes(), original)

            revised = client.put(f"{endpoint}/plan", json={**first, "plan": draft("info")}).raise_for_status().json()["task"]
            second = {"plan_hash": revised["plan"]["hash"]}
            self.assertNotEqual(first, second)
            self.assertIsNone(revised["approval"])
            self.assertEqual(client.post(f"{endpoint}/execute", json=first).status_code, 409)
            self.assertEqual(client.post(f"{endpoint}/execute", json=second).status_code, 409)
            client.post(f"{endpoint}/approval", json={**second, "decision": "approve"}).raise_for_status()
            client.patch(f"/api/agents/{reviewer}", json={
                "name": "reviewer", "environment": "acceptance", "tags": ["review"],
            }).raise_for_status()
            self.assertEqual(client.post(f"{endpoint}/execute", json=second).status_code, 202)
            self.assertEqual(client.post(f"{endpoint}/execute", json=second).status_code, 409)
            for _ in range(200):
                finished = client.get(endpoint).raise_for_status().json()["task"]
                if finished["status"] not in {"running", "queued"}:
                    break
                time.sleep(0.1)
            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(settings_file.read_bytes(), b"level=info\n")
            self.assertEqual([step["status"] for step in finished["steps"]], ["completed"] * 3)
            events = client.get(f"{endpoint}/events").raise_for_status().json()["events"]
            self.assertIn("approval.invalidated", {entry["event_type"] for entry in events})
            live = client.get(f"{endpoint}/events/live", headers={"Last-Event-ID": str(events[-2]["sequence"])})
            self.assertEqual(live.headers["content-type"].split(";")[0], "text/event-stream")
            self.assertEqual(live.text.count("event: task_event"), 1)
            self.assertIn("event: task_finished", live.text)
            allows = [entry for entry in events if entry["event_type"] == "action.authorized"]
            self.assertEqual([entry["agent_id"] for entry in allows], [writer, writer, reviewer])
            evidence = client.get(f"{endpoint}/evidence").raise_for_status().json()["evidence"]
            self.assertEqual(len(evidence), 3)
            for entry in evidence:
                self.assertEqual(entry["content_hash"], digest_json(entry["summary"]))
            change = next(entry["summary"] for entry in evidence if entry["summary"]["operation"] == "workspace.edit")
            self.assertTrue(change["verified"])
            self.assertEqual(change["sha256"], hashlib.sha256(settings_file.read_bytes()).hexdigest())
            self.assertIn("+level=info", change["diff"])

            blocked = draft("blocked")
            blocked["steps"][0]["params"]["path"] = ".env"
            self.assertEqual(client.post("/api/task-plans", json=blocked).status_code, 422)
            self.assertEqual(client.post("/api/task-plans", json={
                **draft("blocked"), "target_selector": {"tags": ["review"]},
            }).status_code, 422)
            expired = submit_plan("expired")
            expired_endpoint = f"/api/tasks/{expired['id']}"
            version = {"plan_hash": expired["plan"]["hash"]}
            client.post(f"{expired_endpoint}/approval", json={**version, "decision": "approve"}).raise_for_status()
            with Database(database).connect() as connection:
                connection.execute("UPDATE task_approvals SET expires_at = 1 WHERE task_id = ?", (expired["id"],))
            self.assertEqual(client.post(f"{expired_endpoint}/execute", json=version).status_code, 409)
            self.assertEqual(settings_file.read_bytes(), b"level=info\n")

            self._exercise_plan_key(client, stranger, base, writer, reviewer, workdir, draft("info"))
            self._exercise_execution_stops(client, stranger, env, writer, reviewer, workdir)
            if browser_module := os.getenv("RAG_BROWSER_PLAYWRIGHT"):
                browser = subprocess.run(
                    ["node", str(Path(__file__).with_name("browser_plan_smoke.mjs")), base, writer, browser_module],
                    check=False, capture_output=True, text=True, encoding="utf-8", timeout=90,
                )
                self.assertEqual(browser.returncode, 0, browser.stderr)
                self.assertEqual((workdir / "browser.ini").read_bytes(), b"level=info\n")
                print(browser.stdout.strip())

    def _exercise_execution_stops(
        self, client: httpx.Client, stranger: httpx.Client, env: dict[str, str], writer: str, reviewer: str, workdir: Path,
    ) -> None:
        controls = "/api/execution-controls"
        device_stop = {"scope": "device", "agent_id": writer, "stopped": True}
        self.assertEqual(stranger.patch(controls, json=device_stop).status_code, 404)
        self.assertEqual(client.patch(controls, json={"scope": "global", "stopped": True}).status_code, 422)
        path = workdir / "stop-control.ini"
        task = client.post("/api/task-plans", json={
            "objective": "Approved write must not run after a scoped stop",
            "steps": [{"key": "write", "agent_id": writer, "operation": "workspace.create",
                       "params": {"path": path.name, "content": "level=info\n"}}],
        }).raise_for_status().json()["task"]
        execute_path = f"/api/tasks/{task['id']}/execute"
        payload = {"plan_hash": task["plan"]["hash"]}
        client.post(f"/api/tasks/{task['id']}/approval", json={**payload, "decision": "approve"}).raise_for_status()
        operation_stop = {"scope": "operation", "subject": "workspace_changes", "stopped": True}
        client.patch(controls, json=operation_stop).raise_for_status()
        self.assertEqual(client.post(execute_path, json=payload).status_code, 409)
        self.assertEqual(stranger.get(controls).json()["stops"], [])
        alert, = client.get(f"/api/tasks/{task['id']}/alerts").raise_for_status().json()["alerts"]
        self.assertEqual(alert["status"], "blocked")
        self.assertEqual(alert["action"], "workspace.create")
        self.assertEqual(alert["agent_id"], writer)
        self.assertTrue(alert["step_id"])
        self.assertEqual(alert["basis"], {"reason": "execution_stopped"})
        alert_path = f"/api/alerts/{alert['id']}"
        transition = {"previous_status": "blocked", "status": "acknowledged"}
        self.assertEqual(stranger.patch(alert_path, json=transition).status_code, 404)
        self.assertEqual(client.patch(alert_path, json={**transition, "status": "approved"}).status_code, 422)
        seen = client.patch(alert_path, json=transition).raise_for_status().json()["alert"]
        self.assertEqual(seen["history"][-1]["source"], "owner")
        self.assertEqual(seen["status"], "acknowledged")
        self.assertEqual(client.patch(alert_path, json=transition).status_code, 409)
        client.patch(alert_path, json={"previous_status": "acknowledged", "status": "resolved"}).raise_for_status()
        self.assertEqual(client.post(execute_path, json=payload).status_code, 409)
        client.patch(controls, json={**operation_stop, "stopped": False}).raise_for_status()
        self.assertEqual(client.post(execute_path, json=payload).status_code, 409)
        self.assertFalse(path.exists())

        client.patch(controls, json=device_stop).raise_for_status()
        self.assertEqual(client.post("/api/tasks", json={"agent_ids": [writer]}).status_code, 422)
        self.assertTrue(client.get(f"/api/agents/{writer}").json()["agent"]["online"])
        client.patch(controls, json={**device_stop, "stopped": False}).raise_for_status()
        new = client.post("/api/task-plans", json={
            "objective": "Unprivileged user cannot resume a global stop",
            "steps": [{"key": "verify", "agent_id": reviewer, "operation": "shell.version", "params": {"tool": "git"}}],
        }).raise_for_status().json()["task"]
        new_request = {"plan_hash": new["plan"]["hash"]}
        new_execute = f"/api/tasks/{new['id']}/execute"

        def instance_control(action: str, expected: str) -> None:
            result = subprocess.run(
                [sys.executable, "-m", "gateway", "execution-stop", action], env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=12,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"instance: {expected}", result.stdout)

        instance_control("pause", "stopped")
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(stranger.get(controls).json()["stops"][0]["scope"], "global")
        self.assertEqual(client.post(new_execute, json=new_request).status_code, 409)
        instance_control("resume", "running")
        self.assertEqual(client.post(new_execute, json=new_request).status_code, 409)
        self.assertFalse(path.exists())

    def _exercise_plan_key(
        self, client: httpx.Client, stranger: httpx.Client, base: str, writer: str, reviewer: str,
        workdir: Path, approval_draft: dict[str, Any],
    ) -> None:
        def issue_key(name: str) -> dict[str, Any]:
            return client.post("/api/mcp-keys", json={"name": name, "scope": "plan"}).raise_for_status().json()

        issued = issue_key("Controller")
        other = issue_key("Other controller")
        secret = issued["secret"]
        mcp = gateway_e2e.GatewayAgentEndToEndTests._mcp_call

        def tool(name: str, arguments: dict[str, Any], bearer: str = secret) -> dict[str, Any]:
            return mcp(client, base, bearer, 1, "tools/call", {"name": name, "arguments": arguments})

        listed = mcp(client, base, secret, 1, "tools/list", {})
        self.assertEqual(
            {item["name"] for item in listed["tools"]},
            {"list_agents", "get_agent_capabilities", "create_operation_plan",
             "get_operation_plan_status", "start_operation_plan"},
        )
        capabilities = tool("get_agent_capabilities", {"agent_id": writer})["structuredContent"]
        self.assertNotIn("mcp_tool", capabilities["profiles"][0]["actions"][0])
        target = workdir / "plan-note.txt"
        for name, arguments in {
            "remote_exec": {"agent_id": reviewer, "command": "git --version"},
            "remote_read_file": {"agent_id": writer, "path": "service.ini"},
            "remote_write_file": {
                "agent_id": writer, "path": target.name, "content": "test only", "expected_sha256": None,
            },
        }.items():
            denied = tool(name, arguments)
            self.assertTrue(denied["isError"], denied)
            self.assertIn("权限范围", denied["content"][0]["text"])
        self.assertFalse(target.exists())

        plan = {"objective": "Record a bounded verification note", "steps": [
            {"key": "write", "agent_id": writer, "operation": "workspace.create",
             "params": {"path": target.name, "content": "fixture\n"}},
            {"key": "verify", "agent_id": reviewer, "depends_on": ["write"], "operation": "shell.version",
             "params": {"tool": "git"}},
        ]}
        pending = tool("create_operation_plan", {"plan": plan})["structuredContent"]
        self.assertEqual(pending["status"], "waiting_approval")
        self.assertTrue(tool("start_operation_plan", {
            "task_id": pending["task_id"], "plan_hash": pending["plan_hash"],
        })["isError"])
        self.assertFalse(target.exists())

        policy_path = f"/api/agents/{writer}/plan-policy"
        permitted = {"l1_paths": [target.name]}
        self.assertEqual(stranger.patch(policy_path, json=permitted).status_code, 404)
        self.assertEqual(client.patch(policy_path, json={"l1_paths": ["service.ini"]}).status_code, 422)
        for path in ("../secret.txt", "AGENTS.md", "docs/*.txt"):
            self.assertEqual(client.patch(policy_path, json={"l1_paths": [path]}).status_code, 422)
        policy = client.patch(policy_path, json=permitted).raise_for_status().json()["agent"]["plan_policy"]
        self.assertEqual(policy, {"l1_paths": [target.name], "revision": 1})
        roleless = {"Authorization": f"Bearer {secret}", "Cookie": ""}
        self.assertEqual(client.patch(policy_path, headers=roleless, json=permitted).status_code, 401)

        manual = tool("create_operation_plan", {"plan": approval_draft})["structuredContent"]
        self.assertTrue(manual["requires_approval"])
        manual_api = f"/api/tasks/{manual['task_id']}"
        approval = {"plan_hash": manual["plan_hash"], "decision": "approve"}
        self.assertEqual(client.post(f"{manual_api}/approval", headers=roleless, json=approval).status_code, 401)
        client.post(f"{manual_api}/approval", json=approval).raise_for_status()
        self.assertTrue(tool("start_operation_plan", {
            "task_id": manual["task_id"], "plan_hash": manual["plan_hash"],
        })["isError"])

        auto = tool("create_operation_plan", {"plan": plan})["structuredContent"]
        self.assertEqual(auto["status"], "approved")
        self.assertFalse(auto["requires_approval"])
        binding = {"task_id": auto["task_id"], "plan_hash": auto["plan_hash"]}
        self.assertTrue(tool("get_operation_plan_status", {"task_id": auto["task_id"]}, other["secret"])["isError"])
        self.assertTrue(tool("start_operation_plan", binding, other["secret"])["isError"])
        self.assertEqual(tool("start_operation_plan", binding)["structuredContent"]["status"], "queued")
        self.assertTrue(tool("start_operation_plan", binding)["isError"])
        for _ in range(100):
            finished = tool("get_operation_plan_status", {"task_id": auto["task_id"]})["structuredContent"]
            if finished["status"] not in {"queued", "running"}:
                break
            time.sleep(0.1)
        self.assertEqual(finished["status"], "completed", finished)
        self.assertEqual(target.read_bytes(), b"fixture\n")
        self.assertNotIn("diff", finished["steps"][0]["result"])
        events = client.get(f"/api/tasks/{auto['task_id']}/events").raise_for_status().json()["events"]
        grant = next(event["payload"]["policy"] for event in events if event["event_type"] == "action.authorized")
        self.assertEqual(grant["matched_rules"], ["operation_plan.device_l1_path"])
        self.assertIsNone(grant["approval_id"])
        self.assertEqual(grant["device_policy_revision"], 1)

        stale = tool("create_operation_plan", {"plan": plan})["structuredContent"]
        client.patch(policy_path, json={"l1_paths": []}).raise_for_status()
        self.assertTrue(tool("start_operation_plan", {
            "task_id": stale["task_id"], "plan_hash": stale["plan_hash"],
        })["isError"])
        client.delete(f"/api/mcp-keys/{issued['key']['id']}").raise_for_status()
        rejected = client.post("/mcp/", headers={
            "Authorization": f"Bearer {secret}", "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(rejected.status_code, 401)


if __name__ == "__main__":
    unittest.main()
