from __future__ import annotations

import unittest

from gateway.task_protocol import (
    TaskProtocolError,
    make_task_action_decision,
    make_task_cancel,
    make_task_start,
    validate_task_message,
)


class TaskProtocolTests(unittest.TestCase):
    def test_task_messages_require_task_identity_and_event_sequence(self) -> None:
        message = validate_task_message(
            {
                "v": 2,
                "type": "task_event",
                "task_id": "task-1",
                "event_id": "event-1",
                "event_type": "step_started",
                "sequence": 1,
                "payload": {},
            }
        )
        self.assertEqual(message["task_id"], "task-1")
        with self.assertRaises(TaskProtocolError):
            validate_task_message(
                {
                    "v": 2,
                    "type": "task_event",
                    "task_id": "task-1",
                    "event_id": "event-2",
                    "event_type": "step_started",
                    "sequence": 0,
                }
            )

    def test_gateway_builders_produce_feature_versioned_messages(self) -> None:
        start = make_task_start(
            "task-1",
            "健康检查",
            [
                {
                    "step_id": "step-1",
                    "profile": "system_observe.v1",
                    "action": "get_host_info",
                    "params": {},
                }
            ],
        )
        self.assertEqual(start["task_protocol"], 1)
        decision = make_task_action_decision("task-1", "action-1", "allow")
        self.assertEqual(validate_task_message(decision)["decision"], "allow")
        self.assertEqual(make_task_cancel("task-1")["type"], "task_cancel")


if __name__ == "__main__":
    unittest.main()
