from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gateway.agent_hub import AgentConnection, AgentHub
from gateway.database import Database
from gateway.device_directory import DeviceFilters
from gateway.health_check_service import HealthCheckError, HealthCheckService
from gateway.health_monitor import HealthMonitor
from gateway.profiles import validate_profile_declarations
from gateway.security import hash_token, hash_password, issue_token
from remote_agent.core.task_runtime import TaskRuntime
from remote_agent.profiles import build_profile_handlers


DECLARATIONS = [
    {
        "id": "system_observe.v1",
        "runtime": {
            "scope": "host",
            "mode": "read_only",
            "platform": "linux",
            "actions": [
                "get_host_info",
                "get_resources",
                "get_processes",
                "get_network",
            ],
        },
    }
]


class FakeWebSocket:
    def __init__(self) -> None:
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send_json(self, message: dict[str, Any]) -> None:
        await self.outgoing.put(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason


class HealthCheckServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.db")
        self.db.initialize()
        self.user = self.db.create_user(
            "health@example.com",
            hash_password("correct horse battery staple"),
        )
        self.hub = AgentHub(self.db)
        self.connection = await self._register_agent("agent-health")
        self.websocket = self.connection.websocket
        self.service = HealthCheckService(self.db, self.hub)

    async def _register_agent(self, agent_id: str) -> AgentConnection:
        enrollment = issue_token("enroll")
        self.db.create_enrollment_token(
            self.user["id"],
            "health-agent",
            enrollment.prefix,
            enrollment.digest,
            2_000_000_000,
        )
        credential = issue_token("agent")
        registered = self.db.register_agent(
            hash_token(enrollment.value),
            agent_id,
            credential.prefix,
            credential.digest,
            "Health Agent",
            "",
            validate_profile_declarations(DECLARATIONS),
            {
                "agent_version": "0.2.3",
                "core_version": 6,
                "protocol_version": 2,
                "build_id": "health-test",
            },
            {"task_protocol": 1, "event_stream": True, "action_interception": "full"},
            [],
        )
        self.assertEqual(registered["id"], agent_id)
        connection = AgentConnection(
            agent_id,
            self.user["id"],
            FakeWebSocket(),
            features={"task_protocol": 1, "event_stream": True, "health_step_cancel": True},
        )
        await self.hub.register(connection)
        return connection

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()
        self.tempdir.cleanup()

    async def test_health_task_collects_events_and_evidence(self) -> None:
        agent_task = asyncio.create_task(self._fake_agent(check_mismatched_action=True))
        self.addAsyncCleanup(self._stop, agent_task)
        task = self.service.create_task(self.user["id"], agent_ids=["agent-health"])
        current = await self._run_task(task["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["result"]["summary"]["healthy"], 1)
        events = self.db.list_task_events(self.user["id"], task["id"])
        self.assertTrue(any(event["event_type"] == "agent.action_completed" for event in events))
        self.assertEqual(sum(event["event_type"] == "action.blocked" for event in events), 1)
        blocked, = self.db.list_task_alerts(self.user["id"], task["id"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["basis"], {"reason": "action_not_allowed"})
        evidence = self.db.list_task_evidence(self.user["id"], task["id"])
        self.assertEqual(evidence[0]["summary"]["status"], "healthy")

    async def test_multi_device_check_preserves_evidence_when_one_agent_disconnects(self) -> None:
        offline = await self._register_agent("agent-offline")

        async def disconnect_on_start() -> None:
            message = await offline.websocket.outgoing.get()
            self.assertEqual(message["type"], "task_start")
            await self.hub.unregister(offline.agent_id, offline.websocket)

        agent_task = asyncio.create_task(self._fake_agent())
        disconnect = asyncio.create_task(disconnect_on_start())
        self.addAsyncCleanup(self._stop, agent_task)
        self.addAsyncCleanup(self._stop, disconnect)
        task = self.service.create_task(self.user["id"], agent_ids=["agent-health", "agent-offline"])
        current = await self._run_task(task["id"])
        self.assertEqual(current["status"], "partially_completed")
        self.assertEqual(current["result"]["summary"], {"healthy": 1, "warning": 0, "critical": 0, "failed": 1})
        evidence = self.db.list_task_evidence(self.user["id"], task["id"])
        self.assertEqual([item["agent_id"] for item in evidence], ["agent-health"])
        self.assertFalse(await self.service.cancel(self.user["id"], task["id"]))

    async def test_requested_runtime_and_ownership_are_required_before_dispatch(self) -> None:
        with self.assertRaises(HealthCheckError):
            self.service.create_task(self.user["id"], agent_ids=["agent-health"], requested_runtime="codex")
        other = self.db.create_user("other@example.com", hash_password("other account password"))
        with self.assertRaises(HealthCheckError):
            self.service.create_task(other["id"], agent_ids=["agent-health"])
        self.assertTrue(self.websocket.outgoing.empty())

    async def test_directory_selector_is_resolved_once_before_health_dispatch(self) -> None:
        user_id = self.user["id"]
        other = await self._register_agent("agent-other")
        self.db.update_agent(user_id, "agent-health", "Health Agent", "", environment="staging", tags=["canary"])
        selector = DeviceFilters(platform="linux", environment="staging", tags=["canary"], capability="system_observe.v1")
        for invalid in (DeviceFilters(), DeviceFilters(tags=["missing"])):
            with self.assertRaises(HealthCheckError):
                self.service.create_task(user_id, target_selector=invalid)
        with self.assertRaises(HealthCheckError):
            self.service.create_task(user_id, agent_ids=["agent-health"], target_selector=selector)
        with self.assertRaises(HealthCheckError):
            self.service.create_task(user_id, requested_runtime="codex", target_selector=selector)

        task = self.service.create_task(user_id, target_selector=selector)
        self.assertEqual(task["target_selector"], {"agent_ids": ["agent-health"]})
        self.db.update_agent(user_id, "agent-health", "Unlabelled", "", environment="", tags=[])
        self.db.update_agent(user_id, other.agent_id, "New canary", "", environment="staging", tags=["canary"])
        producer = asyncio.create_task(self._fake_agent())
        self.addAsyncCleanup(self._stop, producer)
        self.assertEqual((await self._run_task(task["id"]))["status"], "completed")
        self.assertTrue(other.websocket.outgoing.empty())
        self.assertEqual(
            [item["agent_id"] for item in self.db.list_task_evidence(user_id, task["id"])],
            ["agent-health"],
        )

    async def test_missing_observations_never_produce_healthy_evidence(self) -> None:
        async def reply_without_observing() -> None:
            start = await self.websocket.outgoing.get()
            await self.hub.resolve_task(self.connection, {
                "v": 2, "type": "task_result", "task_id": start["task_id"],
                "ok": True, "result": {"steps": []},
            })

        agent_task = asyncio.create_task(reply_without_observing())
        self.addAsyncCleanup(self._stop, agent_task)
        task = self.service.create_task(self.user["id"], agent_ids=["agent-health"])
        current = await self._run_task(task["id"])
        self.assertEqual(current["status"], "failed")
        self.assertEqual(self.db.list_task_evidence(self.user["id"], task["id"]), [])

    async def test_cancel_one_active_observation_keeps_only_verified_partial_evidence(self) -> None:
        connection = self.connection
        hub = self.hub
        active = asyncio.Event()
        terminated = asyncio.Event()

        class LoopbackSender:
            async def send(self, websocket: Any, message: dict[str, Any]) -> None:
                await hub.resolve_task(connection, message)

        async def wait_for_cancel(request_id: str, params: dict[str, Any]) -> dict[str, Any]:
            active.set()
            try:
                await asyncio.Future()
            finally:
                terminated.set()

        async def other_observation(action: str, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
            if action == "get_host_info":
                return await wait_for_cancel(request_id, params)
            return _result_for(action)

        handlers = build_profile_handlers(["system_observe.v1"], Path(self.tempdir.name))
        runtime = TaskRuntime(LoopbackSender(), handlers)

        async def receive() -> None:
            while True:
                message = await self.websocket.outgoing.get()
                if message["type"] == "task_start":
                    await runtime.dispatch(self.websocket, message)
                elif message["type"] == "task_action_decision":
                    await runtime.decide(message)
                elif message["type"] == "task_step_cancel":
                    runtime.cancel_health_step(message["task_id"], message["step_id"])

        producer = asyncio.create_task(receive())
        self.addAsyncCleanup(self._stop, producer)
        self.addAsyncCleanup(runtime.shutdown)
        user_id = self.user["id"]
        task = self.service.create_task(user_id, agent_ids=["agent-health"])
        host_step, resources_step, *_ = task["steps"]
        self.assertEqual(host_step["inputs"]["action"], "get_host_info")
        with patch.object(handlers["system_observe.v1"], "execute", side_effect=other_observation):
            run = self.service.enqueue(user_id, task["id"])
            await asyncio.wait_for(active.wait(), timeout=10)
            self.assertFalse(await self.service.cancel_step("other-owner", task["id"], host_step["id"]))
            self.assertTrue(await self.service.cancel_step(user_id, task["id"], host_step["id"]))
            self.assertFalse(await self.service.cancel_step(user_id, task["id"], host_step["id"]))
            await asyncio.wait_for(run, timeout=10)
        self.assertTrue(terminated.is_set())
        self.assertEqual(self.db.get_task(user_id, task["id"])["status"], "partially_completed")
        self.assertEqual(self.db.get_task_step(host_step["id"])["status"], "cancelled")
        self.assertEqual(self.db.get_task_step(resources_step["id"])["status"], "completed")
        only_evidence, = self.db.list_task_evidence(user_id, task["id"])
        self.assertEqual(only_evidence["kind"], "health_check_partial")
        self.assertEqual(only_evidence["summary"]["status"], "failed")
        self.assertEqual(set(only_evidence["summary"]["observations"]), {"get_resources", "get_processes", "get_network"})
        action = self.db.get_task_action_by_idempotency(f"{task['id']}:{host_step['id']}:system_observe.v1:get_host_info")
        self.assertEqual(action["status"], "cancelled")

    async def test_monitor_redacts_observations_detects_change_and_requires_rearm_after_stop(self) -> None:
        user_id, agent_id = self.user["id"], self.connection.agent_id
        monitor = HealthMonitor(self.db, self.hub, self.service)
        self.addAsyncCleanup(monitor.shutdown)
        self.assertEqual(monitor.list(user_id), [])
        self.assertFalse(await monitor.configure("other-owner", agent_id, 15))
        self.assertTrue(await monitor.configure(user_id, agent_id, 15))
        self.assertEqual(monitor.list("other-owner"), [])
        overrides: dict[str, dict[str, Any]] = {}
        producer = asyncio.create_task(self._fake_agent(overrides=overrides))
        self.addAsyncCleanup(self._stop, producer)

        async def sample() -> str:
            monitor.start_due(monitor.list(user_id)[0]["next_due_at"])
            task_id, run = monitor._runs[agent_id]
            await asyncio.wait_for(run, timeout=10)
            return task_id

        first_id = await sample()
        self.assertEqual(self.db.list_task_alerts(user_id, first_id), [])
        overrides["get_network"] = {"addresses": ["192.0.2.42"]}
        overrides["get_host_info"] = {"release": "private-kernel-release"}
        overrides["get_processes"] = {"processes": [{"pid": index, "name": "private-process"} for index in range(21)]}
        second_id = await sample()
        evidence, = self.db.list_task_evidence(user_id, second_id)
        self.assertEqual(evidence["kind"], "health_monitor")
        task = self.db.get_task(user_id, second_id)
        self.assertIsNone(task["requested_runtime"])
        alerts = self.db.list_task_alerts(user_id, second_id)
        self.assertEqual({entry["category"] for entry in alerts}, {
            "monitor_os_changed", "monitor_network_changed", "monitor_process_count",
        })
        snapshot = json.dumps((evidence, alerts, task, self.db.list_task_steps(user_id, second_id)))
        for raw in ("192.0.2.42", "private-kernel-release", "private-process", "health-agent", '"path": "/"'):
            self.assertNotIn(raw, snapshot)
        self.db.set_execution_stop("operation", stopped=True, user_id=user_id, subject="health_monitor")
        self.db.set_execution_stop("operation", stopped=False, user_id=user_id, subject="health_monitor")
        self.assertEqual(monitor.list(user_id)[0]["status"], "suspended")
        monitor.start_due(monitor.list(user_id)[0]["next_due_at"])
        self.assertEqual(monitor.list(user_id)[0]["last_task_id"], second_id)
        self.assertTrue(await monitor.configure(user_id, agent_id, 15))
        self.assertEqual(monitor.list(user_id)[0]["status"], "enabled")
        restart_at = monitor.list(user_id)[0]["next_due_at"] + 1
        self.db.defer_overdue_health_monitors(restart_at)
        monitor.start_due(restart_at)
        self.assertIsNone(monitor.list(user_id)[0]["last_task_id"])
        monitor.start_due(monitor.list(user_id)[0]["expires_at"])
        self.assertIsNone(monitor.list(user_id)[0]["last_task_id"])
        self.assertTrue(await monitor.configure(user_id, agent_id, None))
        self.assertEqual(monitor.list(user_id), [])

    async def test_monitor_reports_one_failure_burst_without_remote_error_details(self) -> None:
        user_id, agent_id = self.user["id"], self.connection.agent_id
        monitor = HealthMonitor(self.db, self.hub, self.service)
        self.addAsyncCleanup(monitor.shutdown)

        async def reject() -> None:
            while True:
                message = await self.websocket.outgoing.get()
                if message["type"] == "task_start":
                    await self.hub.resolve_task(self.connection, {
                        "v": 2, "type": "task_error", "task_id": message["task_id"], "error": "private agent error",
                    })

        producer = asyncio.create_task(reject())
        self.addAsyncCleanup(self._stop, producer)
        await monitor.configure(user_id, agent_id, 30)
        for index in range(4):
            monitor.start_due(monitor.list(user_id)[0]["next_due_at"])
            task_id, run = monitor._runs[agent_id]
            await asyncio.wait_for(run, timeout=10)
            self.assertEqual(self.db.list_task_evidence(user_id, task_id), [])
            self.assertNotIn("private agent error", json.dumps(self.db.list_task_events(user_id, task_id)))
            alerts = self.db.list_task_alerts(user_id, task_id)
            self.assertEqual([alert["category"] for alert in alerts], ["monitor_failures"] if index == 2 else [])

    def test_health_alert_basis_uses_metrics_without_paths_or_host_details(self) -> None:
        status, alerts = self.service._assess({
            "get_resources": {
                "cpu": {"logical_count": 2, "load_average": [3.5, 2.0, 1.0]},
                "memory": {"utilization_percent": 97.5},
                "disks": [{"total_bytes": 1000, "free_bytes": 80, "path": "private/path"}],
            },
            "get_processes": {"available": True},
            "get_network": {"interfaces": [{"name": "lo"}]},
        }, "agent-health", "task-observation")
        self.assertEqual(status, "warning")
        self.assertEqual({alert["category"] for alert in alerts}, {"cpu_load", "memory", "disk"})
        self.assertTrue(all(alert["action"] == "system_observe.v1/get_resources" for alert in alerts))
        self.assertNotIn("private/path", repr(alerts))

    async def _run_task(self, task_id: str) -> dict[str, Any]:
        self.service.enqueue(self.user["id"], task_id)
        async with asyncio.timeout(10):
            while True:
                current = self.db.get_task(self.user["id"], task_id)
                if current["status"] in {"completed", "failed", "partially_completed"}:
                    return current
                await asyncio.sleep(0.02)

    @staticmethod
    async def _stop(agent_task: asyncio.Task[None]) -> None:
        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)

    async def _fake_agent(
        self, *, check_mismatched_action: bool = False, overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        while True:
            message = await self.websocket.outgoing.get()
            if message["type"] == "task_start":
                await self.hub.resolve_task(
                    self.connection,
                    {
                        "v": 2,
                        "type": "task_ack",
                        "task_id": message["task_id"],
                        "status": "accepted",
                    },
                )
                action_ids: list[str] = []
                remote_sequence = 0
                for step in message["steps"]:
                    action_request_id = f"action_{uuid.uuid4().hex}"
                    action_ids.append(action_request_id)
                    action_request = {
                        "v": 2,
                        "type": "task_action_request",
                        "task_id": message["task_id"],
                        "step_id": step["step_id"],
                        "action_request_id": action_request_id,
                        "profile": step["profile"],
                        "action": step["action"],
                        "params": {},
                        "risk_level": "L0",
                    }
                    if check_mismatched_action and remote_sequence == 0:
                        await self.hub.resolve_task(
                            self.connection, {**action_request, "profile": "shell.v1", "action": "exec"},
                        )
                        self.assertEqual((await self.websocket.outgoing.get())["decision"], "deny")
                    await self.hub.resolve_task(self.connection, action_request)
                    decision = await self.websocket.outgoing.get()
                    self.assertEqual(decision["type"], "task_action_decision")
                    self.assertEqual(decision["decision"], "allow")
                    remote_sequence += 1
                    await self.hub.resolve_task(
                        self.connection,
                        {
                            "v": 2,
                            "type": "task_event",
                            "task_id": message["task_id"],
                            "event_id": f"event_{uuid.uuid4().hex}",
                            "event_type": "action_completed",
                            "sequence": remote_sequence,
                            "step_id": step["step_id"],
                            "payload": {},
                        },
                    )
                steps = [
                    {
                        "step_id": step["step_id"],
                        "action_request_id": action_id,
                        "profile": "system_observe.v1",
                        "action": step["action"],
                        "result": {**_result_for(step["action"]), **(overrides or {}).get(step["action"], {})},
                    }
                    for step, action_id in zip(
                        message["steps"],
                        action_ids,
                        strict=True,
                    )
                ]
                await self.hub.resolve_task(
                    self.connection,
                    {
                        "v": 2,
                        "type": "task_result",
                        "task_id": message["task_id"],
                        "ok": True,
                        "result": {"steps": steps},
                    },
                )


def _result_for(action: str) -> dict[str, Any]:
    base = {"observed_at": "2026-11-22T00:00:00.000Z", "observed_at_unix": 1.0}
    if action == "get_host_info":
        return {
            **base,
            "os": "linux",
            "system": "Linux",
            "release": "test",
            "version": "test",
            "architecture": "x86_64",
            "processor": "test",
            "hostname": "health-agent",
            "python": "3.13",
            "pid": 1,
        }
    if action == "get_resources":
        return {
            **base,
            "cpu": {"logical_count": 2, "load_average": [0.1, 0.1, 0.1]},
            "memory": {
                "total_bytes": 100,
                "available_bytes": 80,
                "used_bytes": 20,
                "utilization_percent": 20,
            },
            "disks": [
                {
                    "path": "/",
                    "total_bytes": 100,
                    "used_bytes": 20,
                    "free_bytes": 80,
                }
            ],
        }
    if action == "get_processes":
        return {**base, "available": True, "processes": [], "truncated": False}
    return {
        **base,
        "hostname": "health-agent",
        "interfaces": [{"index": 1, "name": "lo"}],
        "addresses": ["127.0.0.1"],
    }


if __name__ == "__main__":
    unittest.main()
