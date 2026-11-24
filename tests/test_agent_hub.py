from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from gateway.agent_hub import AgentBusyError, AgentConnection, AgentExecutionStopped, AgentHub, AgentOfflineError
from gateway.database import Database
from gateway.health_check_service import _runtime_info
from remote_agent.core.limits import MAX_CONCURRENT_REQUESTS_PER_AGENT
from remote_agent.runtimes.codex import CODEX_ADAPTER_VERSION, CODEX_VERSION


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class SlowCancelWebSocket(FakeWebSocket):
    async def send_json(self, message: dict[str, Any]) -> None:
        if message.get("type") == "cancel":
            await asyncio.sleep(0.01)
        await super().send_json(message)


class ExecutionStopHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / "controls.db")
        self.db.initialize()
        self.hub = AgentHub(self.db)
        self.socket = FakeWebSocket()
        self.connection = AgentConnection(
            "agent-a", "account-a", self.socket,
            features={"task_protocol": 1, "runtime_adapter": True, "action_interception": "limited"},
            runtimes=[{
                "provider_id": "codex", "mode": "managed_limited", "status": "available",
                "supported_profiles": ["system_observe.v1"],
                "runtime_version": CODEX_VERSION, "adapter_version": CODEX_ADAPTER_VERSION,
            }],
        )
        await self.hub.register(self.connection)

    async def test_device_stop_cancels_direct_request_and_preserves_other_devices(self) -> None:
        other_socket = FakeWebSocket()
        other = AgentConnection("agent-b", "account-a", other_socket)
        await self.hub.register(other)
        targeted = asyncio.create_task(self.hub.execute("agent-a", "workspace.v1", "read", {}, 3))
        unaffected = asyncio.create_task(self.hub.execute("agent-b", "workspace.v1", "read", {}, 3))
        await asyncio.sleep(0)
        request_id = self.socket.messages[0]["request_id"]
        self.db.set_execution_stop("device", user_id="account-a", agent_id="agent-a", stopped=True)
        self.assertEqual(await self.hub.enforce_execution_stops("account-a"), 1)
        self.assertEqual(self.socket.messages[-1], {"v": 2, "type": "cancel", "request_id": request_id})
        with self.assertRaises(AgentExecutionStopped):
            await targeted
        with self.assertRaises(AgentExecutionStopped):
            await self.hub.execute("agent-a", "workspace.v1", "read", {}, 3)
        self.assertTrue(self.hub.is_online("agent-a"))
        self.assertEqual(len(self.socket.messages), 2)
        self.assertEqual(len(other_socket.messages), 1)
        await self.hub.resolve(other, {
            "type": "result", "request_id": other_socket.messages[0]["request_id"], "ok": True, "result": {"ok": True},
        })
        self.assertEqual(await unaffected, {"ok": True})

    async def test_runtime_stop_cancels_codex_but_not_native_task(self) -> None:
        model = asyncio.create_task(self.hub.execute_task("agent-a", "model", "fixture", [], 3, requested_runtime="codex"))
        native = asyncio.create_task(self.hub.execute_task("agent-a", "native", "fixture", [], 3))
        await asyncio.sleep(0)
        self.db.set_execution_stop("runtime", user_id="account-a", agent_id="agent-a", subject="codex", stopped=True)
        self.assertEqual(await self.hub.enforce_execution_stops("account-a"), 1)
        with self.assertRaises(AgentExecutionStopped):
            await model
        self.assertFalse(self.hub.supports_task_runtime("agent-a", "codex"))
        self.assertTrue(self.hub.supports_task_runtime("agent-a", None))
        self.assertEqual([(message["type"], message["task_id"]) for message in self.socket.messages], [
            ("task_start", "model"), ("task_start", "native"), ("task_cancel", "model"),
        ])
        await self.hub.resolve_task(self.connection, {
            "v": 2, "type": "task_result", "task_id": "native", "ok": True, "result": {"observed": True},
        })
        self.assertEqual(await native, {"observed": True})

    async def test_stop_then_resume_does_not_authorize_action_waiting_to_send(self) -> None:
        evaluating = asyncio.Event()
        continue_decision = asyncio.Event()

        async def approve(message: dict[str, Any]) -> dict[str, Any]:
            evaluating.set()
            await continue_decision.wait()
            return {"decision": "allow"}

        task = asyncio.create_task(self.hub.execute_task("agent-a", "waiting", "fixture", [], 3, on_message=approve))
        await asyncio.sleep(0)
        await self.hub.resolve_task(self.connection, {
            "v": 2, "type": "task_action_request", "task_id": "waiting", "action_request_id": "action-test",
            "step_id": "step", "profile": "system_observe.v1", "action": "get_host_info", "params": {},
        })
        await asyncio.wait_for(evaluating.wait(), 2)
        self.db.set_execution_stop("account", user_id="account-a", stopped=True)
        self.db.set_execution_stop("account", user_id="account-a", stopped=False)
        continue_decision.set()
        with self.assertRaises(AgentExecutionStopped):
            await task
        self.assertEqual([message["type"] for message in self.socket.messages], ["task_start", "task_cancel"])

    async def test_cancelled_health_step_replaces_an_in_flight_allow_decision(self) -> None:
        self.connection.features["health_step_cancel"] = True
        evaluating = asyncio.Event()
        continue_decision = asyncio.Event()

        async def approve(message: dict[str, Any]) -> dict[str, Any]:
            if message["type"] == "task_action_request":
                evaluating.set()
                await continue_decision.wait()
                return {"decision": "allow"}
            return {}

        task = asyncio.create_task(self.hub.execute_task(
            "agent-a", "health", "fixture", [{"step_id": "observe"}], 5, on_message=approve,
        ))
        await asyncio.sleep(0)
        await self.hub.resolve_task(self.connection, {
            "v": 2, "type": "task_action_request", "task_id": "health", "action_request_id": "action-test",
            "step_id": "observe", "profile": "system_observe.v1", "action": "get_host_info", "params": {},
        })
        await asyncio.wait_for(evaluating.wait(), 2)
        self.assertFalse(self.hub.can_cancel_health_step("agent-a", "health", "unrelated"))
        self.assertTrue(self.hub.can_cancel_health_step("agent-a", "health", "observe"))
        await self.hub.cancel_health_step("agent-a", "health", "observe")
        self.assertFalse(self.hub.can_cancel_health_step("agent-a", "health", "observe"))
        continue_decision.set()
        await self.hub.resolve_task(self.connection, {
            "v": 2, "type": "task_result", "task_id": "health", "ok": True, "result": {"steps": []},
        })
        self.assertEqual(await task, {"steps": []})
        self.assertEqual([message["type"] for message in self.socket.messages], [
            "task_start", "task_step_cancel", "task_action_decision",
        ])
        self.assertEqual(self.socket.messages[-1]["decision"], "deny")
        self.assertEqual(self.socket.messages[-1]["policy_context"], {"step_cancelled": True})


class AgentHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = AgentHub()
        self.socket_a = FakeWebSocket()
        self.socket_b = FakeWebSocket()
        self.connection_a = AgentConnection("agent-a", "user-a", self.socket_a)
        await self.hub.register(self.connection_a)
        await self.hub.register(AgentConnection("agent-b", "user-b", self.socket_b))

    def test_health_runtime_dispatch_and_evidence_require_compatible_versions(self) -> None:
        self.connection_a.features.update({
            "task_protocol": 1, "runtime_adapter": True, "action_interception": "limited",
        })
        advertised = {
            "provider_id": "codex", "mode": "managed_limited", "status": "available",
            "supported_profiles": ["system_observe.v1"],
            "runtime_version": CODEX_VERSION, "adapter_version": CODEX_ADAPTER_VERSION,
        }
        self.connection_a.runtimes = [advertised]
        self.assertTrue(self.hub.supports_task_runtime("agent-a", "codex"))
        session = {**advertised, "session_id": "fixture-turn"}
        self.assertEqual(_runtime_info(session, "codex")["adapter_version"], CODEX_ADAPTER_VERSION)
        for field in ("runtime_version", "adapter_version"):
            self.connection_a.runtimes = [{**advertised, field: "99.0.0"}]
            self.assertFalse(self.hub.supports_task_runtime("agent-a", "codex"))
            with self.assertRaises(ValueError):
                _runtime_info({**session, field: "99.0.0"}, "codex")
        self.connection_a.runtimes = [{**advertised, "provider_id": "unknown"}]
        self.assertFalse(self.hub.supports_task_runtime("agent-a", "unknown"))
        self.assertTrue(self.hub.supports_task_runtime("agent-a", None))

    async def test_result_can_only_resolve_its_own_agent_request(self) -> None:
        task = asyncio.create_task(
            self.hub.execute("agent-a", "shell.v1", "exec", {"command": "pwd"}, 2)
        )
        await asyncio.sleep(0)
        self.assertEqual(self.socket_a.messages[0]["v"], 2)
        request_id = self.socket_a.messages[0]["request_id"]

        await self.hub.resolve(
            self.hub._connections["agent-b"],
            {"type": "result", "request_id": request_id, "ok": True, "result": {"stdout": "bad"}},
        )
        self.assertFalse(task.done())

        await self.hub.resolve(
            self.connection_a,
            {"type": "result", "request_id": request_id, "ok": True, "result": {"stdout": "ok"}},
        )
        self.assertEqual((await task)["stdout"], "ok")

    async def test_disconnect_only_fails_requests_for_that_agent(self) -> None:
        task_a = asyncio.create_task(
            self.hub.execute("agent-a", "shell.v1", "exec", {"command": "a"}, 2)
        )
        task_b = asyncio.create_task(
            self.hub.execute("agent-b", "shell.v1", "exec", {"command": "b"}, 2)
        )
        await asyncio.sleep(0)

        await self.hub.unregister("agent-a", self.socket_a)
        request_b = self.socket_b.messages[0]["request_id"]
        await self.hub.resolve(
            self.hub._connections["agent-b"],
            {"type": "result", "request_id": request_b, "ok": True, "result": {"stdout": "b"}},
        )

        with self.assertRaises(AgentOfflineError):
            await task_a
        self.assertEqual((await task_b)["stdout"], "b")

    async def test_heartbeat_unregisters_stale_connection_and_fails_pending_requests(self) -> None:
        task = asyncio.create_task(
            self.hub.execute("agent-a", "shell.v1", "exec", {"command": "pwd"}, 2)
        )
        await asyncio.sleep(0)
        self.connection_a.last_pong -= 120

        await self.hub.heartbeat(timeout=60)

        self.assertTrue(self.socket_a.closed)
        self.assertFalse(self.hub.is_online("agent-a"))
        with self.assertRaises(AgentOfflineError):
            await task

    async def test_concurrent_request_limit_is_per_agent_and_releases_slots(self) -> None:
        tasks = [
            asyncio.create_task(
                self.hub.execute(
                    "agent-a",
                    "shell.v1",
                    "exec",
                    {"command": str(index)},
                    2,
                )
            )
            for index in range(MAX_CONCURRENT_REQUESTS_PER_AGENT)
        ]
        await asyncio.sleep(0)
        self.assertEqual(len(self.socket_a.messages), MAX_CONCURRENT_REQUESTS_PER_AGENT)

        with self.assertRaisesRegex(AgentBusyError, "Agent 当前忙碌"):
            await self.hub.execute(
                "agent-a", "shell.v1", "exec", {"command": "overflow"}, 2
            )

        other_agent = asyncio.create_task(
            self.hub.execute("agent-b", "shell.v1", "exec", {"command": "ok"}, 2)
        )
        await asyncio.sleep(0)
        request_b = self.socket_b.messages[-1]["request_id"]
        await self.hub.resolve(
            self.hub._connections["agent-b"],
            {"type": "result", "request_id": request_b, "ok": True, "result": {}},
        )
        self.assertEqual(await other_agent, {})

        first_request = self.socket_a.messages[0]["request_id"]
        await self.hub.resolve(
            self.connection_a,
            {"type": "result", "request_id": first_request, "ok": True, "result": {}},
        )
        self.assertEqual(await tasks[0], {})

        replacement = asyncio.create_task(
            self.hub.execute(
                "agent-a", "shell.v1", "exec", {"command": "replacement"}, 2
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(len(self.socket_a.messages), MAX_CONCURRENT_REQUESTS_PER_AGENT + 1)

        for task in [*tasks[1:], replacement]:
            task.cancel()
        await asyncio.gather(*tasks[1:], replacement, return_exceptions=True)

    async def test_cancelling_caller_sends_cancel_to_remote_agent(self) -> None:
        task = asyncio.create_task(
            self.hub.execute("agent-a", "shell.v1", "exec", {"command": "long"}, 60)
        )
        await asyncio.sleep(0)
        request_id = self.socket_a.messages[0]["request_id"]

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            self.socket_a.messages[-1],
            {"v": 2, "type": "cancel", "request_id": request_id},
        )

    async def test_cancelling_caller_waits_for_cancel_send_to_finish(self) -> None:
        socket = SlowCancelWebSocket()
        hub = AgentHub()
        await hub.register(AgentConnection("slow", "user", socket))
        task = asyncio.create_task(hub.execute("slow", "shell.v1", "exec", {}, 60))
        await asyncio.sleep(0)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual([message["type"] for message in socket.messages], ["request", "cancel"])

    async def test_replacing_connection_fails_old_pending_and_isolates_results(self) -> None:
        task = asyncio.create_task(
            self.hub.execute("agent-a", "shell.v1", "exec", {"command": "old"}, 60)
        )
        await asyncio.sleep(0)
        request_id = self.socket_a.messages[0]["request_id"]
        replacement_socket = FakeWebSocket()
        replacement = AgentConnection("agent-a", "user-a", replacement_socket)

        await self.hub.register(replacement)
        await self.hub.resolve(
            replacement,
            {"type": "result", "request_id": request_id, "ok": True, "result": {}},
        )

        self.assertTrue(self.socket_a.closed)
        with self.assertRaises(AgentOfflineError):
            await task


if __name__ == "__main__":
    unittest.main()
