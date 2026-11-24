from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from remote_agent.core.task_runtime import TaskRuntime
from remote_agent.profiles import build_profile_handlers


class FakeSender:
    async def send(self, websocket: Any, message: dict[str, Any]) -> None:
        websocket.messages.append(message)


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []


class TaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_before_task_runs_still_notifies_gateway(self) -> None:
        runtime = TaskRuntime(FakeSender(), {})
        websocket = FakeWebSocket()
        await runtime.dispatch(websocket, {"task_id": "task-cancelled"})
        await runtime.cancel("task-cancelled", websocket)
        self.assertEqual([message["type"] for message in websocket.messages], ["task_error"])
        self.assertEqual(runtime.active_count, 0)

    async def test_read_only_task_requests_actions_and_returns_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskRuntime(
                FakeSender(),
                build_profile_handlers(["system_observe.v1"], Path(directory)),
            )
            websocket = FakeWebSocket()
            await runtime.dispatch(
                websocket,
                {
                    "task_id": "task-health",
                    "objective": "健康检查",
                    "steps": [
                        {
                            "step_id": "step-host",
                            "profile": "system_observe.v1",
                            "action": "get_host_info",
                            "params": {},
                        }
                    ],
                },
            )
            for _ in range(50):
                request = next(
                    (
                        message
                        for message in websocket.messages
                        if message["type"] == "task_action_request"
                    ),
                    None,
                )
                if request is not None:
                    await runtime.decide(
                        {
                            "task_id": "task-health",
                            "action_request_id": request["action_request_id"],
                            "decision": "allow",
                            "policy_context": {
                                "risk_level": "L0",
                                "allowed_scope": {
                                    "profile": "system_observe.v1",
                                    "action": "get_host_info",
                                },
                            },
                        }
                    )
                    break
                await asyncio.sleep(0.01)
            for _ in range(100):
                if any(message["type"] == "task_result" for message in websocket.messages):
                    break
                await asyncio.sleep(0.01)
            result = next(
                message for message in websocket.messages if message["type"] == "task_result"
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["steps"][0]["action"], "get_host_info")
            self.assertTrue(
                all(
                    message["type"] != "task_action_request"
                    or message["profile"] == "system_observe.v1"
                    for message in websocket.messages
                )
            )


if __name__ == "__main__":
    unittest.main()
