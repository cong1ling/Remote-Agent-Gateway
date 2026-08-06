from __future__ import annotations

import asyncio
import unittest
from typing import Any

from gateway.agent_hub import AgentConnection, AgentHub, AgentOfflineError


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class AgentHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = AgentHub()
        self.socket_a = FakeWebSocket()
        self.socket_b = FakeWebSocket()
        self.connection_a = AgentConnection("agent-a", "user-a", self.socket_a)
        await self.hub.register(self.connection_a)
        await self.hub.register(AgentConnection("agent-b", "user-b", self.socket_b))

    async def test_result_can_only_resolve_its_own_agent_request(self) -> None:
        task = asyncio.create_task(
            self.hub.execute("agent-a", "shell.v1", "exec", {"command": "pwd"}, 2)
        )
        await asyncio.sleep(0)
        self.assertEqual(self.socket_a.messages[0]["v"], 2)
        request_id = self.socket_a.messages[0]["request_id"]

        await self.hub.resolve(
            "agent-b",
            {"type": "result", "request_id": request_id, "ok": True, "result": {"stdout": "bad"}},
        )
        self.assertFalse(task.done())

        await self.hub.resolve(
            "agent-a",
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
            "agent-b",
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


if __name__ == "__main__":
    unittest.main()
