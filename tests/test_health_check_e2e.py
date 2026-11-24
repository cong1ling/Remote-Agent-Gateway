from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path

import httpx

from gateway.database import Database
from remote_agent.runtimes.claude import CLAUDE_FIXTURE_KEY, CLAUDE_VERSION
from tests.claude_health_provider import ClaudeProvider
from tests.codex_health_provider import HEALTH_TOOLS, HealthProvider


class HealthCheckE2ETests(unittest.TestCase):
    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    @staticmethod
    def _stop(process: subprocess.Popen[str] | None) -> None:
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def test_real_agent_completes_read_only_health_check(self) -> None:
        self._run_health_check()

    @unittest.skipUnless(os.getenv("RAG_TEST_CODEX"), "set RAG_TEST_CODEX to the native Codex CLI 0.169.0")
    def test_real_codex_executes_health_check_with_loopback_provider(self) -> None:
        self._run_health_check(codex_bin=os.environ["RAG_TEST_CODEX"])

    @unittest.skipUnless(os.getenv("RAG_TEST_CLAUDE"), f"set RAG_TEST_CLAUDE to the native Claude Code {CLAUDE_VERSION}")
    def test_real_claude_executes_health_check_with_loopback_provider(self) -> None:
        self._run_health_check(claude_bin=os.environ["RAG_TEST_CLAUDE"])

    def _run_health_check(self, *, codex_bin: str | None = None, claude_bin: str | None = None) -> None:
        gateway: subprocess.Popen[str] | None = None
        agent: subprocess.Popen[str] | None = None
        requested_runtime = "codex" if codex_bin else "claude" if claude_bin else None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            port = self._free_port()
            base_url = f"http://127.0.0.1:{port}"
            environment = os.environ.copy()
            environment.pop("OPENAI_API_KEY", None)
            environment.pop("CODEX_API_KEY", None)
            environment["RAG_CLAUDE_API_KEY"] = "not-to-be-sent-to-loopback"
            environment["NO_PROXY"] = "127.0.0.1,localhost"
            environment.update(
                {
                    "GATEWAY_HOST": "127.0.0.1",
                    "GATEWAY_PORT": str(port),
                    "GATEWAY_PUBLIC_URL": base_url,
                    "GATEWAY_DATA_DIR": str(root / "data"),
                    "GATEWAY_DATABASE": str(root / "data" / "gateway.db"),
                    "GATEWAY_SECURE_COOKIES": "false",
                    "PYTHONUNBUFFERED": "1",
                }
            )
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            gateway_log = root / "gateway.log"
            agent_log = root / "agent.log"
            with gateway_log.open("wb") as log:
                gateway = subprocess.Popen(
                    [sys.executable, "-m", "gateway"],
                    cwd=Path(__file__).resolve().parents[1],
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            try:
                with (
                    httpx.Client(trust_env=False, timeout=10) as client,
                    HealthProvider(codex_home) if codex_bin else nullcontext() as provider,
                    ClaudeProvider() if claude_bin else nullcontext() as claude_provider,
                ):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if gateway.poll() is not None:
                            self.fail(f"Gateway exited early\n{gateway_log.read_text(encoding='utf-8', errors='replace')}")
                        try:
                            if client.get(f"{base_url}/api/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.1)
                    else:
                        self.fail("Gateway did not become healthy")

                    registered = client.post(
                        f"{base_url}/api/auth/register",
                        json={
                            "email": "health-e2e@example.com",
                            "password": "correct horse battery staple",
                        },
                    )
                    self.assertEqual(registered.status_code, 201, registered.text)
                    bundle_response = client.post(
                        f"{base_url}/api/agent-bundles",
                        json={
                            "name": "Health Agent",
                            "runtime": "python",
                            "profiles": ["system_observe.v1"],
                        },
                    )
                    self.assertEqual(bundle_response.status_code, 201, bundle_response.text)
                    bundle = bundle_response.json()
                    archive = root / "agent.zip"
                    archive.write_bytes(
                        client.get(f"{base_url}{bundle['bundle']['download_url']}").content
                    )
                    extracted = root / "agent"
                    with zipfile.ZipFile(archive) as handle:
                        handle.extractall(extracted)
                    launcher = next(extracted.rglob("run.py"))
                    with agent_log.open("wb") as log:
                        agent = subprocess.Popen(
                            [
                                sys.executable,
                                str(launcher),
                                "--gateway",
                                f"ws://127.0.0.1:{port}/ws/agent",
                                "--token",
                                bundle["secret"],
                                "--workdir",
                                str(root),
                                *(
                                    ["--codex-bin", codex_bin, "--codex-home", str(codex_home)]
                                    if codex_bin else []
                                ),
                                *(
                                    ["--claude-bin", claude_bin, "--claude-model", "claude-sonnet-4-6",
                                     "--claude-loopback-fixture-port", str(claude_provider.server_port)]
                                    if claude_bin else []
                                ),
                            ],
                            cwd=launcher.parent,
                            env=environment,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            creationflags=creationflags,
                        )
                    online_deadline = time.monotonic() + 20
                    agent_id = None
                    while time.monotonic() < online_deadline:
                        if agent.poll() is not None:
                            self.fail(f"Agent exited early\n{agent_log.read_text(encoding='utf-8', errors='replace')}")
                        agents = client.get(f"{base_url}/api/agents").json()["agents"]
                        if agents and agents[0]["online"]:
                            agent_id = agents[0]["id"]
                            self.assertGreaterEqual(
                                agents[0]["features"]["task_protocol"],
                                1,
                            )
                            self.assertTrue(agents[0]["features"]["health_step_cancel"])
                            self.assertEqual(bool(agents[0]["runtimes"]), bool(requested_runtime))
                            self.assertEqual(agents[0]["health_runtimes"], [requested_runtime] if requested_runtime else [])
                            device_platform = {"win32": "windows", "darwin": "macos", "linux": "linux"}[sys.platform]
                            self.assertEqual(agents[0]["device"]["platform"], device_platform)
                            self.assertEqual(agents[0]["device"]["device_type"], "computer")
                            self.assertTrue(agents[0]["device"]["platform_version"])
                            break
                        time.sleep(0.1)
                    self.assertIsNotNone(agent_id, "Agent did not connect")

                    request = {
                        "kind": "health_check",
                        "requested_runtime": requested_runtime,
                        "objective": "private-label-must-not-reach-model",
                    }
                    if requested_runtime:
                        request["agent_ids"] = [agent_id]
                    else:
                        client.patch(f"{base_url}/api/agents/{agent_id}", json={
                            "name": "Native health", "environment": "test", "tags": ["canary"],
                        }).raise_for_status()
                        selector = {"platform": device_platform, "environment": "test", "tags": ["canary"],
                                    "capability": "system_observe.v1/get_host_info"}
                        for invalid in (None, {}, {"platfrom": device_platform}):
                            self.assertEqual(client.post(f"{base_url}/api/tasks", json={
                                **request, "target_selector": invalid,
                            }).status_code, 422)
                        request["target_selector"] = selector
                    created = client.post(
                        f"{base_url}/api/tasks",
                        json=request,
                    )
                    self.assertEqual(created.status_code, 202, created.text)
                    self.assertEqual(created.json()["task"]["target_selector"], {"agent_ids": [agent_id]})
                    task_id = created.json()["task"]["id"]
                    task_deadline = time.monotonic() + 30
                    while time.monotonic() < task_deadline:
                        task = client.get(f"{base_url}/api/tasks/{task_id}").json()["task"]
                        if task["status"] in {"completed", "failed", "partially_completed"}:
                            break
                        time.sleep(0.1)
                    self.assertEqual(task["status"], "completed", task)
                    self.assertEqual(
                        task["result"]["summary"]["healthy"]
                        + task["result"]["summary"]["warning"],
                        1,
                        task,
                    )
                    self.assertEqual(task["result"]["summary"]["failed"], 0, task)
                    events = client.get(f"{base_url}/api/tasks/{task_id}/events").json()["events"]
                    evidence = client.get(
                        f"{base_url}/api/tasks/{task_id}/evidence"
                    ).json()["evidence"]
                    self.assertTrue(any(event["event_type"] == "task.completed" for event in events))
                    self.assertEqual(evidence[0]["kind"], "health_check")
                    self.assertEqual(set(evidence[0]["summary"]["observations"]), set(HEALTH_TOOLS))
                    decisions = [event for event in events if event["event_type"] == "action.requested"]
                    self.assertEqual(len(decisions), len(HEALTH_TOOLS))
                    self.assertTrue(all(event["payload"]["policy"]["decision"] == "allow" for event in decisions))
                    if provider is not None:
                        runtime = evidence[0]["summary"]["runtime"]
                        self.assertEqual(runtime["provider_id"], "codex")
                        self.assertEqual(runtime["mode"], "managed_limited")
                        db = Database(root / "data" / "gateway.db")
                        session = db.get_runtime_session(runtime["session_id"])
                        self.assertEqual(session["status"], "completed")
                        self.assertEqual(session["metadata"]["task_id"], task_id)
                        self.assertEqual(len(provider.requests), len(HEALTH_TOOLS) + 1)
                        for model_request in provider.requests:
                            self.assertEqual(
                                {
                                    tool.get("name", tool["type"]) for tool in model_request["tools"]
                                    if tool.get("name") != "request_user_input"
                                },
                                set(HEALTH_TOOLS),
                            )
                        inputs = json.dumps(provider.requests)
                        self.assertNotIn(request["objective"], inputs)
                        self.assertNotIn(bundle["secret"], inputs)
                        self.assertNotIn("observed_at", inputs)
                        self.assertIn("interface_count", inputs)
                        self._verify_codex_step_cancel(client, base_url, request, provider)
                        self._verify_codex_failure_and_cancel(client, base_url, request, provider, db)
                    elif claude_provider is not None:
                        runtime = evidence[0]["summary"]["runtime"]
                        self.assertEqual(runtime["provider_id"], "claude")
                        self.assertEqual(runtime["runtime_version"], CLAUDE_VERSION)
                        self.assertEqual(runtime["mode"], "managed_limited")
                        self.assertEqual([entry["payload"]["action"] for entry in decisions], list(reversed(HEALTH_TOOLS)))
                        model_request, = claude_provider.requests
                        self.assertEqual(model_request["api_key"], CLAUDE_FIXTURE_KEY)
                        self.assertEqual(model_request["body"].get("tools", []), [])
                        self.assertLessEqual(model_request["body"]["max_tokens"], 1024)
                        serialized = json.dumps(model_request)
                        for private in (request["objective"], bundle["secret"], agent_id, "observed_at", "not-to-be-sent"):
                            self.assertNotIn(private, serialized)
                        self._verify_claude_failure_and_cancel(client, base_url, request, claude_provider)
                    else:
                        unsupported = client.post(
                            f"{base_url}/api/tasks", json={**request, "requested_runtime": "codex"},
                        )
                        self.assertEqual(unsupported.status_code, 422, unsupported.text)
                        self._verify_native_monitor(client, base_url, agent_id)
            finally:
                self._stop(agent)
                self._stop(gateway)

    def _verify_native_monitor(self, client: httpx.Client, base_url: str, agent_id: str) -> None:
        endpoint = f"{base_url}/api/health-monitors"
        self.assertEqual(client.get(endpoint).json(), {"monitors": []})
        for invalid in ({"interval_minutes": 1}, {"interval_minutes": 15, "requested_runtime": "codex"}):
            self.assertEqual(client.put(f"{endpoint}/{agent_id}", json=invalid).status_code, 422)
        with httpx.Client(trust_env=False, timeout=10) as other:
            self.assertEqual(other.put(f"{endpoint}/{agent_id}", json={"interval_minutes": 15}).status_code, 401)
        configuration = client.put(f"{endpoint}/{agent_id}", json={"interval_minutes": 15})
        self.assertEqual(configuration.status_code, 200, configuration.text)
        self.assertEqual(configuration.json()["monitors"][0]["status"], "enabled")
        expiry = time.monotonic() + 10
        while time.monotonic() < expiry:
            schedule, = client.get(endpoint).json()["monitors"]
            if schedule["last_task_id"]:
                break
            time.sleep(0.1)
        else:
            self.fail("Opt-in native monitor did not dispatch")
        task_id = schedule["last_task_id"]
        task = self._wait_for_status(client, base_url, task_id, "completed")
        self.assertEqual(task["target_selector"], {"agent_ids": [agent_id]})
        self.assertIsNone(task["requested_runtime"])
        monitor_evidence, = client.get(f"{base_url}/api/tasks/{task_id}/evidence").json()["evidence"]
        self.assertEqual(monitor_evidence["kind"], "health_monitor")
        snapshot = monitor_evidence["summary"]["observations"]
        self.assertEqual(set(snapshot), set(HEALTH_TOOLS))
        self.assertEqual(set(snapshot["get_network"]), {"fingerprint", "interface_count", "address_count"})
        self.assertEqual(client.put(f"{endpoint}/{agent_id}", json={"interval_minutes": None}).json(), {"monitors": []})

    def _verify_codex_step_cancel(
        self, client: httpx.Client, base_url: str, request: dict, provider: HealthProvider,
    ) -> None:
        provider.mode = "complete_with_pause"
        provider.complete_from = len(provider.requests)
        created = client.post(f"{base_url}/api/tasks", json=request)
        self.assertEqual(created.status_code, 202, created.text)
        task = created.json()["task"]
        skipped = next(step for step in task["steps"] if step["inputs"]["action"] == "get_network")
        try:
            self.assertTrue(provider.step_paused.wait(timeout=20))
            response = client.post(f"{base_url}/api/tasks/{task['id']}/steps/{skipped['id']}/cancel")
            self.assertEqual(response.json(), {"cancelled": True})
        finally:
            provider.step_release.set()
        final = self._wait_for_status(client, base_url, task["id"], "partially_completed")
        self.assertEqual(next(step for step in final["steps"] if step["id"] == skipped["id"])["status"], "cancelled")
        evidence, = client.get(f"{base_url}/api/tasks/{task['id']}/evidence").json()["evidence"]
        self.assertEqual(evidence["kind"], "health_check_partial")
        self.assertTrue(evidence["summary"]["incomplete"])
        self.assertEqual(evidence["summary"]["runtime"]["provider_id"], "codex")
        self.assertEqual(set(evidence["summary"]["observations"]), set(HEALTH_TOOLS) - {"get_network"})

    def _verify_codex_failure_and_cancel(
        self, client: httpx.Client, base_url: str, request: dict, provider: HealthProvider, db: Database,
    ) -> None:
        provider.mode = "skip_tools"
        failed = client.post(f"{base_url}/api/tasks", json=request)
        self.assertEqual(failed.status_code, 202, failed.text)
        failed_id = failed.json()["task"]["id"]
        self._wait_for_status(client, base_url, failed_id, "failed")
        self.assertEqual(client.get(f"{base_url}/api/tasks/{failed_id}/evidence").json()["evidence"], [])
        failed_events = client.get(f"{base_url}/api/tasks/{failed_id}/events").json()["events"]
        self.assertIn("未完成全部已授权观察", str(failed_events))

        provider.mode = "wait"
        provider.request_started.clear()
        created = client.post(f"{base_url}/api/tasks", json=request)
        self.assertEqual(created.status_code, 202, created.text)
        task_id = created.json()["task"]["id"]
        self.assertTrue(provider.request_started.wait(timeout=20))
        response = client.post(f"{base_url}/api/tasks/{task_id}/cancel")
        self.assertEqual(response.json(), {"cancelled": True})
        task = self._wait_for_status(client, base_url, task_id, "cancelled")
        self.assertTrue(all(step["status"] == "cancelled" for step in task["steps"]))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            events = client.get(f"{base_url}/api/tasks/{task_id}/events").json()["events"]
            finished = [event for event in events if event["event_type"] == "agent.runtime_finished"]
            if finished:
                payload = finished[0]["payload"]["payload"]
                session = db.get_runtime_session(payload["session_id"])
                if session["status"] == "cancelled":
                    break
            time.sleep(0.1)
        else:
            self.fail("Codex did not finish cancellation on the agent")
        self.assertEqual(client.get(f"{base_url}/api/tasks/{task_id}/evidence").json()["evidence"], [])

    def _verify_claude_failure_and_cancel(
        self, client: httpx.Client, base_url: str, request: dict, provider: ClaudeProvider,
    ) -> None:
        provider.mode = "unknown_action"
        rejected = client.post(f"{base_url}/api/tasks", json=request)
        self.assertEqual(rejected.status_code, 202, rejected.text)
        task_id = rejected.json()["task"]["id"]
        self._wait_for_status(client, base_url, task_id, "failed")
        endpoint = f"{base_url}/api/tasks/{task_id}"
        self.assertEqual(client.get(f"{endpoint}/evidence").json()["evidence"], [])
        events = client.get(f"{endpoint}/events").json()["events"]
        self.assertFalse(any(event["event_type"] == "action.requested" for event in events))
        self.assertNotIn("outside_health_catalog", json.dumps(events))

        provider.mode = "wait"
        provider.request_started.clear()
        created = client.post(f"{base_url}/api/tasks", json=request)
        self.assertEqual(created.status_code, 202, created.text)
        task_id = created.json()["task"]["id"]
        endpoint = f"{base_url}/api/tasks/{task_id}"
        self.assertTrue(provider.request_started.wait(timeout=20))
        self.assertEqual(client.post(f"{endpoint}/cancel").json(), {"cancelled": True})
        self._wait_for_status(client, base_url, task_id, "cancelled")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            events = client.get(f"{endpoint}/events").json()["events"]
            if any(
                event["event_type"] == "agent.runtime_finished"
                and event["payload"]["payload"]["status"] == "cancelled" for event in events
            ):
                break
            time.sleep(0.1)
        else:
            self.fail("Claude did not finish cancellation on the agent")
        self.assertEqual(client.get(f"{endpoint}/evidence").json()["evidence"], [])
        self.assertFalse(any(event["event_type"] == "action.requested" for event in events))

    def _wait_for_status(
        self, client: httpx.Client, base_url: str, task_id: str, status: str,
    ) -> dict:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            task = client.get(f"{base_url}/api/tasks/{task_id}").json()["task"]
            if task["status"] in {"completed", "failed", "partially_completed", "cancelled"}:
                self.assertEqual(task["status"], status, task)
                return task
            time.sleep(0.1)
        self.fail(f"Task did not reach {status}: {task}")


if __name__ == "__main__":
    unittest.main()
