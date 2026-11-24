from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from remote_agent.runtimes.base import ActionDecision, RuntimeTask
from remote_agent.runtimes.codex import CodexRuntime
from remote_agent.runtimes.jsonrpc import JsonRpcError, sanitized_environment


class FakeJsonRpcClient:
    def __init__(self, events: list[dict[str, Any]] | None = None):
        self.events_to_emit = list(events or [])
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[object, dict[str, Any]]] = []
        self.rejected: list[object] = []
        self.closed = False

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30,
    ) -> dict[str, Any]:
        del timeout
        self.requests.append((method, dict(params or {})))
        if method == "initialize":
            return {"userAgent": "Codex Desktop/0.169.0 (test)"}
        if method == "thread/start":
            return {
                "thread": {"id": "thread-1"},
                "approvalPolicy": "never",
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "instructionSources": [],
            }
        return {"turn": {"id": "turn-1"}}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.notifications.append((method, dict(params or {})))

    async def respond(self, request_id: object, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    async def reject(self, request_id: object) -> None:
        self.rejected.append(request_id)

    async def events(self):
        for event in self.events_to_emit:
            yield event

    async def close(self) -> None:
        self.closed = True


class CodexRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, client: FakeJsonRpcClient) -> CodexRuntime:
        async def factory(*args: Any, **kwargs: Any) -> FakeJsonRpcClient:
            del args, kwargs
            return client

        return CodexRuntime(Path("codex"), Path("isolated-home"), client_factory=factory)

    async def test_codex_app_server_lifecycle_and_event_mapping(self) -> None:
        client = FakeJsonRpcClient([
            {
                "id": 7,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-1", "turnId": "turn-1", "callId": "call-1",
                    "tool": "get_host_info", "arguments": {},
                },
            },
            {"method": "item/completed", "params": {"item": {"text": "do-not-store"}}},
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
            },
        ])
        runtime = self._runtime(client)
        session = await runtime.start(RuntimeTask(
            "task-1", "健康检查", "只读检查设备", actions=("get_host_info",),
        ))
        self.assertEqual(session.provider_version, "0.169.0")
        self.assertEqual(session.metadata["thread_id"], "thread-1")
        self.assertEqual(client.requests[0][1]["capabilities"]["experimentalApi"], True)
        thread_params = client.requests[1][1]
        self.assertEqual(thread_params["environments"], [])
        self.assertEqual(thread_params["sandbox"], "read-only")
        self.assertTrue(thread_params["ephemeral"])
        self.assertEqual([item["name"] for item in thread_params["dynamicTools"]], ["get_host_info"])
        self.assertEqual(client.requests[2][1]["environments"], [])

        events = runtime.events(session)
        event = await anext(events)
        self.assertEqual(event.event_type, "action_request")
        self.assertEqual(event.payload, {"action": "get_host_info"})
        await runtime.decide_action(session, ActionDecision(
            event.action_request_id, "allow",
            policy_context={
                "risk_level": "L0",
                "allowed_scope": {"profile": "system_observe.v1", "action": "get_host_info"},
            },
            result={"os": "windows"},
        ))
        self.assertEqual((await anext(events)).event_type, "turn_completed")
        await events.aclose()
        self.assertEqual(client.responses[0][0], 7)
        self.assertTrue(client.responses[0][1]["success"])
        self.assertIn('"os": "windows"', client.responses[0][1]["contentItems"][0]["text"])
        await runtime.cancel(session)
        self.assertEqual(
            client.requests[-1],
            ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}),
        )
        await runtime.close(session)
        self.assertTrue(client.closed)
        self.assertFalse(Path(thread_params["cwd"]).exists())

    async def test_codex_never_grants_native_approvals(self) -> None:
        for method, response in (
            ("item/commandExecution/requestApproval", {"decision": "cancel"}),
            ("item/fileChange/requestApproval", {"decision": "cancel"}),
            ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
        ):
            with self.subTest(method=method):
                client = FakeJsonRpcClient([{"id": "native", "method": method, "params": {"token": "not-for-gateway"}}])
                runtime = self._runtime(client)
                session = await runtime.start(RuntimeTask("task", "x", "x", ("get_host_info",)))
                try:
                    events = runtime.events(session)
                    self.assertEqual((await anext(events)).event_type, "action_blocked")
                    with self.assertRaises(JsonRpcError):
                        await anext(events)
                    self.assertEqual(client.responses, [("native", response)])
                finally:
                    await runtime.close(session)

    async def test_extra_parameters_and_unknown_requests_never_reach_broker(self) -> None:
        client = FakeJsonRpcClient([
            {"id": 1, "method": "item/tool/call", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "callId": "one",
                "tool": "get_host_info", "arguments": {"token": "not-for-gateway"},
            }},
            {"id": 2, "method": "unknown", "params": {}},
        ])
        runtime = self._runtime(client)
        session = await runtime.start(RuntimeTask("task", "x", "x", ("get_host_info",)))
        try:
            events = runtime.events(session)
            self.assertEqual((await anext(events)).event_type, "action_blocked")
            self.assertEqual((await anext(events)).event_type, "action_blocked")
            with self.assertRaises(JsonRpcError):
                await anext(events)
            self.assertFalse(client.responses[0][1]["success"])
            self.assertNotIn("not-for-gateway", str(client.responses))
            self.assertEqual(client.rejected, [2])
        finally:
            await runtime.close(session)

    async def test_early_exit_does_not_complete_turn(self) -> None:
        client = FakeJsonRpcClient()
        runtime = self._runtime(client)
        session = await runtime.start(RuntimeTask("task", "x", "x", ("get_host_info",)))
        try:
            with self.assertRaisesRegex(JsonRpcError, "完成前断开"):
                await anext(runtime.events(session))
        finally:
            await runtime.close(session)

    def test_environment_redacts_agent_credentials(self) -> None:
        environment = sanitized_environment(
            {
                "REMOTE_AGENT_CREDENTIAL": "secret",
                "OPENAI_API_KEY": "provider-secret",
                "PATH": "path",
            }
        )
        self.assertNotIn("REMOTE_AGENT_CREDENTIAL", environment)
        self.assertEqual(environment["OPENAI_API_KEY"], "provider-secret")
        self.assertEqual(sanitized_environment({"Path": "credential"}, exclude="PATH"), {})


if __name__ == "__main__":
    unittest.main()
