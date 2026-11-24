from __future__ import annotations

import hashlib
import io
import json
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

from tests import test_e2e_gateway_agent as gateway_e2e
from tests.managed_service_fixture import ManagedServiceFixture


class ManagedResourcesE2ETests(unittest.TestCase):
    def test_approved_application_recovery_and_same_source_verification(self) -> None:
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
            workspace = root / "workspace"
            workspace.mkdir()
            app_config = workspace / "service.ini"
            original = b"mode=broken\n"
            app_config.write_bytes(original)
            fixture = cleanup.enter_context(ManagedServiceFixture(app_config))
            old_pid = fixture.worker_pid
            source_config = root / "services.json"
            fixture.write_agent_config(source_config)
            source_archive, target_archive = root / "source.zip", root / "target.zip"
            archive = self._source_archive(expected_value=6)
            for path in (source_archive, target_archive):
                path.write_bytes(archive)
            source_digest = hashlib.sha256(archive).hexdigest()
            lifecycle = gateway_e2e.GatewayAgentEndToEndTests
            port = lifecycle._free_port()
            base_url = f"http://127.0.0.1:{port}"
            environment = {
                **os.environ, "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(port),
                "GATEWAY_PUBLIC_URL": base_url, "GATEWAY_SECURE_COOKIES": "false",
                "GATEWAY_DATA_DIR": str(root / "gateway-data"), "GATEWAY_DATABASE": str(root / "gateway.db"),
                "NO_PROXY": "127.0.0.1,localhost",
            }

            def launch(label: str, command: list[str], workdir: Path) -> tuple[subprocess.Popen, Path]:
                log = root / f"{label}.log"
                with log.open("wb") as output:
                    process = subprocess.Popen(command, cwd=workdir, env=environment, stdout=output, stderr=subprocess.STDOUT)
                cleanup.callback(lifecycle._stop, process)
                return process, log

            gateway, gateway_log = launch("gateway", [sys.executable, "-m", "gateway"], Path(__file__).resolve().parents[1])
            client = cleanup.enter_context(httpx.Client(base_url=base_url, trust_env=False, timeout=10))
            self._until(lambda: client.get("/api/health").status_code == 200, gateway, gateway_log)
            client.post("/api/auth/register", json={
                "email": "managed@example.test", "password": "managed resources fixture 42",
            }).raise_for_status()
            scoped = client.post("/api/mcp-keys", json={"name": "controller", "scope": "plan"}).raise_for_status().json()
            legacy = client.post("/api/mcp-keys", json={"name": "old", "scope": "legacy"}).raise_for_status().json()

            def enroll(name: str, profiles: list[str], workdir: Path, arguments: list[str]) -> str:
                package = client.post("/api/agent-bundles", json={
                    "name": name, "runtime": "python", "profiles": profiles,
                }).raise_for_status().json()
                payload = client.get(package["bundle"]["download_url"]).raise_for_status().content
                with zipfile.ZipFile(io.BytesIO(payload)) as builder:
                    files = builder.namelist()
                    self.assertEqual(any("/profiles/build_verify_v1/" in path for path in files), "build_verify.v1" in profiles)
                    self.assertEqual(any("/profiles/service_control_v1/" in path for path in files), "service_control.v1" in profiles)
                    self.assertFalse(any(fixture.credential.encode("ascii") in builder.read(path) for path in files))
                    builder.extractall(root / name)
                entry = next((root / name).rglob("run.py"))
                process, log = launch(name, [
                    sys.executable, str(entry), "--gateway", f"ws://127.0.0.1:{port}/ws/agent",
                    "--token", package["secret"], "--workdir", str(workdir), *arguments,
                ], entry.parent)
                agent = self._until(lambda: next((
                    agent for agent in client.get("/api/agents").raise_for_status().json()["agents"]
                    if agent["online"] and agent["name"] == name
                ), None), process, log)
                self.assertTrue(agent["features"]["planned_actions"])
                return agent["id"]

            self.assertEqual(client.post("/api/agent-bundles", json={
                "name": "unsafe mix", "runtime": "python", "profiles": ["shell.v1", "service_control.v1"],
            }).status_code, 422)
            developer = enroll("developer", ["workspace.v1", "service_control.v1", "build_verify.v1"], workspace, [
                "--service-config", str(source_config), "--build-archive", str(source_archive),
            ])
            builder_workdir = root / "build-workdir"
            builder_workdir.mkdir()
            builder = enroll("builder", ["build_verify.v1"], builder_workdir, ["--build-archive", str(target_archive)])

            def tool(name: str, arguments: dict[str, Any], *, key: str = scoped["secret"]) -> dict[str, Any]:
                return lifecycle._mcp_call(client, base_url, key, 1, "tools/call", {
                    "name": name, "arguments": arguments,
                })

            def submit(
                objective: str, steps: list[dict[str, Any]], *, maintenance: dict[str, int] | None = None,
            ) -> dict[str, Any]:
                plan: dict[str, Any] = {"objective": objective, "steps": steps}
                if maintenance is not None:
                    plan["maintenance"] = maintenance
                response = tool("create_operation_plan", {"plan": plan})
                self.assertFalse(response.get("isError"), response)
                return response["structuredContent"]

            def await_task(task_id: str) -> dict[str, Any]:
                def done() -> dict[str, Any] | None:
                    task = client.get(f"/api/tasks/{task_id}").raise_for_status().json()["task"]
                    return task if task["status"] not in {"running", "queued"} else None
                return self._until(done, gateway, gateway_log, timeout=35)

            def verify_with_approval(draft: dict[str, Any], *, before_start: Any = None) -> dict[str, Any]:
                task_id, digest = draft["task_id"], draft["plan_hash"]
                endpoint = f"/api/tasks/{task_id}"
                binding = {"plan_hash": digest}
                self.assertEqual(draft["status"], "waiting_approval")
                self.assertEqual(client.post(f"{endpoint}/execute", json=binding).status_code, 409)
                self.assertTrue(tool("start_operation_plan", {"task_id": task_id, **binding})["isError"])
                client.post(f"{endpoint}/approval", json={**binding, "decision": "approve"}).raise_for_status()
                self.assertTrue(tool("start_operation_plan", {"task_id": task_id, **binding})["isError"])
                if before_start:
                    before_start()
                self.assertEqual(client.post(f"{endpoint}/execute", json=binding).status_code, 202)
                return await_task(task_id)

            def evidence(task_id: str) -> list[dict[str, Any]]:
                records = client.get(f"/api/tasks/{task_id}/evidence").raise_for_status().json()["evidence"]
                self.assertNotIn(fixture.credential, json.dumps(records))
                return records

            def events(task_id: str) -> list[dict[str, Any]]:
                records = client.get(f"/api/tasks/{task_id}/events").raise_for_status().json()["events"]
                self.assertNotIn(fixture.credential, json.dumps(records))
                return records

            capabilities = tool("get_agent_capabilities", {"agent_id": developer})["structuredContent"]["profiles"]
            managed = next(profile for profile in capabilities if profile["id"] == "service_control.v1")
            self.assertEqual(managed["runtime"], {"backend": "http-loopback.v1", "service_ids": ["demo"]})
            self.assertEqual(managed["actions"][1]["mcp_tool"], "create_operation_plan")
            self.assertNotIn(fixture.credential, json.dumps(capabilities))
            legacy_caps = tool("get_agent_capabilities", {"agent_id": developer}, key=legacy["secret"])["structuredContent"]
            legacy_profile = next(profile for profile in legacy_caps["profiles"] if profile["id"] == "service_control.v1")
            self.assertNotIn("mcp_tool", legacy_profile["actions"][1])

            inspect_step = {"key": "observe", "agent_id": developer, "operation": "service.inspect", "params": {"service_id": "demo"}}
            rejected = tool("create_operation_plan", {"plan": {"objective": "unregistered target", "steps": [
                {**inspect_step, "params": {"service_id": "not-registered"}},
            ]}})
            self.assertTrue(rejected["isError"])
            observation = submit("Diagnose disposable application", [inspect_step])
            tool("start_operation_plan", {"task_id": observation["task_id"], "plan_hash": observation["plan_hash"]})
            observed = await_task(observation["task_id"])
            status = evidence(observed["id"])[0]["summary"]
            self.assertEqual(status["state"], "degraded")
            self.assertEqual(status["recent_events"][0]["code"], "config_invalid")
            self.assertTrue(client.get(f"/api/tasks/{observed['id']}/alerts").json()["alerts"])
            readiness_url = f"http://127.0.0.1:{fixture.worker_port}/ready"
            self.assertEqual(client.get(readiness_url).status_code, 503)
            generation = status["generation"]
            repair = {"key": "recover", "agent_id": developer, "operation": "service.restart", "params": {
                "service_id": "demo", "expected_generation": generation,
            }}
            fix = {"key": "fix", "agent_id": developer, "operation": "workspace.edit", "params": {
                "path": "service.ini", "old_text": "mode=broken", "new_text": "mode=ready",
                "expected_sha256": hashlib.sha256(original).hexdigest(),
            }}

            def unchanged() -> None:
                self.assertEqual(fixture.restart_count, 0)
                self.assertEqual(fixture.worker_pid, old_pid)
                self.assertEqual(app_config.read_bytes(), original)

            recovery = verify_with_approval(submit("Repair and check fresh readiness", [
                fix, {**repair, "depends_on": ["fix"]},
                {**inspect_step, "depends_on": ["recover"]},
            ]), before_start=unchanged)
            self.assertEqual(recovery["status"], "completed", recovery)
            self.assertEqual(fixture.restart_count, 1)
            self.assertNotEqual(fixture.worker_pid, old_pid)
            self.assertEqual(app_config.read_bytes(), b"mode=ready\n")
            self.assertEqual(client.get(readiness_url).status_code, 200)
            recovered = next(item["summary"] for item in evidence(recovery["id"]) if item["summary"]["operation"] == "service.restart")
            self.assertEqual(recovered["previous_generation"], generation)
            self.assertNotEqual(recovered["generation"], generation)
            self.assertEqual(recovered["state"], "ready")
            self.assertIs(recovered["verified"], True)
            stale = verify_with_approval(submit("Refuse stale service generation", [repair]))
            self.assertEqual(stale["steps"][0]["error"], "service.generation_changed")
            self.assertEqual(fixture.restart_count, 1)

            def build_step(key: str, device_id: str, operation: str, digest: str, *depends_on: str) -> dict[str, Any]:
                return {"key": key, "agent_id": device_id, "operation": operation,
                        "params": {"source_sha256": digest}, "depends_on": list(depends_on)}

            now = int(time.time())
            maintenance = {"starts_at": now - 30, "ends_at": now + 300, "max_concurrency": 2}
            checked = verify_with_approval(submit("Validate identical immutable source on two devices", [
                build_step("source_a", developer, "build.source", source_digest),
                build_step("source_b", builder, "build.source", source_digest),
                build_step("verify_a", developer, "build.verify", source_digest, "source_a"),
                build_step("verify_b", builder, "build.verify", source_digest, "source_b", "verify_a"),
            ], maintenance=maintenance))
            self.assertEqual(checked["status"], "completed", checked)
            self.assertEqual(checked["plan"]["maintenance"]["batches"], [
                ["source_a", "source_b"], ["verify_a"], ["verify_b"],
            ])
            self.assertEqual(
                [event["payload"] for event in events(checked["id"]) if event["event_type"] == "maintenance.batch_finished"],
                [{"batch": number, "verified": True} for number in (1, 2, 3)],
            )
            build_evidence = [entry for entry in evidence(checked["id"]) if entry["summary"]["operation"] == "build.verify"]
            self.assertEqual({entry["agent_id"] for entry in build_evidence}, {developer, builder})
            self.assertEqual({entry["summary"]["source_sha256"] for entry in build_evidence}, {source_digest})
            self.assertTrue(all(entry["summary"]["tests_run"] == 1 and entry["summary"]["verified"] for entry in build_evidence))

            failing_archive = self._source_archive(expected_value=11)
            target_archive.write_bytes(failing_archive)
            stale_build = verify_with_approval(submit("Refuse a different snapshot before running", [
                build_step("report", builder, "build.verify", source_digest),
            ]))
            self.assertEqual(stale_build["steps"][0]["error"], "build.source_mismatch")
            self.assertEqual(evidence(stale_build["id"]), [])
            failing = verify_with_approval(submit("Keep structured failed test evidence", [
                build_step("report", builder, "build.verify", hashlib.sha256(failing_archive).hexdigest()),
            ]))
            self.assertEqual(failing["status"], "verification_failed")
            failed_evidence, = evidence(failing["id"])
            self.assertEqual(failed_evidence["kind"], "operation_plan_attempt")
            self.assertEqual(failed_evidence["summary"]["failures"], 1)
            self.assertIs(failed_evidence["summary"]["verified"], False)

            halted = verify_with_approval(submit("Stop remaining batches when a peer test fails", [
                build_step("observe", developer, "build.source", source_digest),
                build_step("fail", builder, "build.verify", hashlib.sha256(failing_archive).hexdigest()),
                build_step("remaining", developer, "build.verify", source_digest),
            ], maintenance=maintenance))
            self.assertEqual(halted["status"], "partially_completed")
            skipped = next(step for step in halted["steps"] if step["id"].endswith(":remaining"))
            self.assertEqual((skipped["status"], skipped["error"]), ("skipped", "previous_batch_failed"))
            timeline = events(halted["id"])
            self.assertEqual([event["payload"]["batch"] for event in timeline
                              if event["event_type"] == "maintenance.batch_started"], [1])
            self.assertTrue(any(event["event_type"] == "maintenance.stopped" for event in timeline))
            self.assertFalse(any(event["step_id"] == skipped["id"] and event["event_type"] == "action.authorized"
                                 for event in timeline))
            print(f"Managed acceptance: readiness 503 -> 200, approved recovery {recovery['id']}; "
                  f"two same-source Agents {checked['id']}; digest mismatch {stale_build['id']}; "
                  f"failed batch stopped {halted['id']}", flush=True)

    @staticmethod
    def _source_archive(*, expected_value: int) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("sample.py", "def double(value):\n    return value * 2\n")
            archive.writestr("tests/test_sample.py", (
                "import unittest\nfrom sample import double\n\n"
                "class DoubleTests(unittest.TestCase):\n"
                f"    def test_double(self):\n        self.assertEqual(double(3), {expected_value})\n"
            ))
        return output.getvalue()

    def _until(self, condition: Any, process: subprocess.Popen, log: Path, *, timeout: int = 20) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(log.read_text(errors="replace"))
            try:
                if result := condition():
                    return result
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        self.fail(f"Managed-resource fixture timed out ({log.name})")


if __name__ == "__main__":
    unittest.main()
