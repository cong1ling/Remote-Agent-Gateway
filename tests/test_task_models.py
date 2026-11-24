import unittest

from gateway.task_models import (
    ActionRequest,
    InvalidTaskTransition,
    RiskDecision,
    StepRecord,
    TaskRecord,
    TaskStatus,
)


class TaskModelTests(unittest.TestCase):
    def test_task_status_transitions_reject_skipping_approval(self):
        task = TaskRecord.new("user-1", "检查设备")

        task.transition(TaskStatus.PLANNED)
        task.transition(TaskStatus.WAITING_APPROVAL)

        with self.assertRaises(InvalidTaskTransition):
            task.transition(TaskStatus.RUNNING)

    def test_task_can_be_planned_and_approved(self):
        task = TaskRecord.new("user-1", "检查设备")

        for status in (
            TaskStatus.PLANNED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.APPROVED,
            TaskStatus.QUEUED,
        ):
            task.transition(status)

        self.assertEqual(task.status, TaskStatus.QUEUED)

    def test_step_and_action_have_stable_idempotency_keys(self):
        task = TaskRecord.new("user-1", "检查设备")
        step = StepRecord.new(task.id, "health_check")
        action = ActionRequest.new(
            task.id,
            step.id,
            "agent-1",
            "system_observe.v1",
            "get_host_info",
            {},
            approval_requirement=RiskDecision.ALLOW,
        )

        self.assertTrue(step.idempotency_key.startswith(task.id))
        self.assertEqual(
            action.idempotency_key,
            f"{task.id}:{step.id}:system_observe.v1:get_host_info",
        )

    def test_task_and_action_validate_required_fields(self):
        with self.assertRaises(ValueError):
            TaskRecord.new("", "检查设备")
        with self.assertRaises(ValueError):
            ActionRequest.new("task", "step", "agent", "profile", "", {})


if __name__ == "__main__":
    unittest.main()
