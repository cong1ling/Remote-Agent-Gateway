from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.agent_hub import AgentConnection, AgentHub
from gateway.database import ConflictError, Database
from gateway.operation_plan_service import OperationPlanDraft, OperationPlanService
from gateway.security import issue_token
from remote_agent.profiles.base import ProfileContext
from remote_agent.profiles.common.planned_action import check_workspace_target, parse_planned_action
from remote_agent.profiles.workspace_v1.profile import WorkspaceV1Profile
from tests.test_health_check_service import FakeWebSocket


class OperationPlanServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "test.db")
        self.db.initialize()
        self.user_id = self.db.create_user("owner@example.test", "fixture")["id"]
        self.hub = AgentHub()
        self.plans = OperationPlanService(self.db, self.hub)
        self.addAsyncCleanup(self.plans.shutdown)
        self.agent_id = "managed-device"
        self.features = {
            "task_protocol": 1, "event_stream": True, "planned_actions": True, "planned_l1_policy": True,
        }
        enroll, device = issue_token("enroll"), issue_token("agent")
        self.db.create_enrollment_token(self.user_id, "plan", enroll.prefix, enroll.digest, int(time.time()) + 60)
        self.db.register_agent(
            enroll.digest, self.agent_id, device.prefix, device.digest, "Managed test", "",
            [WorkspaceV1Profile(ProfileContext(self.root)).declaration()],
            {"agent_version": "0.2.3", "build_id": "fixture", "protocol_version": 2, "core_version": 6},
            self.features,
        )
        self.socket = FakeWebSocket()
        self.connection = AgentConnection(self.agent_id, self.user_id, self.socket, features=self.features)
        await self.hub.register(self.connection)

    def _draft(self, *, inspect: bool = False) -> OperationPlanDraft:
        return OperationPlanDraft.model_validate({
            "objective": "verify the managed task boundary",
            "steps": [{
                "key": "first", "agent_id": self.agent_id,
                "operation": "workspace.hash" if inspect else "workspace.create",
                "params": {"path": "notes.txt", **({} if inspect else {"content": "safe demo"})},
            }],
        })

    async def test_reconnect_requires_a_new_plan_even_if_same_device(self) -> None:
        task = self.plans.save(self.user_id, self._draft())
        task_id, version = task["id"], task["plan"]["hash"]
        self.plans.decide(self.user_id, task_id, version, approve=True)
        replacement = FakeWebSocket()
        await self.hub.register(AgentConnection(self.agent_id, self.user_id, replacement, features=self.features))
        with self.assertRaisesRegex(ConflictError, "连接"):
            self.plans.start(self.user_id, task_id, version)
        self.assertTrue(replacement.outgoing.empty())
        self.assertTrue(self.socket.outgoing.empty())
        self.assertEqual(self.db.list_task_events(self.user_id, task_id)[-1]["event_type"], "action.blocked")

    async def test_rejected_plan_never_reaches_the_device(self) -> None:
        task = self.plans.save(self.user_id, self._draft())
        task_id, version = task["id"], task["plan"]["hash"]
        self.assertEqual(task["plan"]["max_level"], "L1")
        rejected = self.plans.decide(self.user_id, task_id, version, approve=False)
        self.assertEqual(rejected["status"], "cancelled")
        self.assertEqual(rejected["approval"]["decision"], "deny")
        with self.assertRaises(ConflictError):
            self.plans.start(self.user_id, task_id, version)
        self.assertTrue(self.socket.outgoing.empty())

    async def test_maintenance_claim_is_human_only_and_bound_to_approved_open_window(self) -> None:
        now = int(time.time())
        draft = OperationPlanDraft.model_validate({
            **self._draft(inspect=True).model_dump(),
            "maintenance": {"starts_at": now + 60, "ends_at": now + 2400, "max_concurrency": 2},
        })
        submitted = self.plans.save(self.user_id, draft)
        task_id, version = submitted["id"], submitted["plan"]["hash"]
        self.assertEqual(submitted["status"], "waiting_approval")
        self.assertEqual(submitted["plan"]["maintenance"]["batches"], [["first"]])
        with self.assertRaises(ConflictError):
            self.plans.start(self.user_id, task_id, version)
        approved = self.plans.decide(self.user_id, task_id, version, approve=True)
        self.assertEqual(approved["approval"]["expires_at"], now + 2400)
        with self.assertRaisesRegex(ConflictError, "不在批准的维护窗口内"):
            self.plans.start(self.user_id, task_id, version)
        with patch("gateway.database.time.time", return_value=now + 2400):
            with self.assertRaisesRegex(ConflictError, "不在批准的维护窗口内"):
                self.plans.start(self.user_id, task_id, version)
        self.assertEqual(self.db.get_task(self.user_id, task_id)["status"], "approved")
        draft.maintenance.starts_at += 1
        revised = self.plans.save(self.user_id, draft, task_id=task_id, previous_hash=version)
        self.assertNotEqual(revised["plan"]["hash"], version)
        self.assertIsNone(revised["approval"])
        self.assertTrue(self.socket.outgoing.empty())

    async def test_cancelling_in_flight_step_reaches_the_bound_device(self) -> None:
        task = self.plans.save(self.user_id, self._draft(inspect=True))
        task_id, version = task["id"], task["plan"]["hash"]
        self.assertEqual(task["status"], "approved")
        self.assertFalse(task["plan"]["requires_approval"])
        step_id = task["steps"][0]["id"]
        self.plans.start(self.user_id, task_id, version)
        worker = self.plans.running[task_id]
        request = await asyncio.wait_for(self.socket.outgoing.get(), 2)
        self.assertEqual(request["type"], "task_start")
        self.assertTrue(await self.plans.cancel_step(self.user_id, task_id, step_id))
        cancel = await asyncio.wait_for(self.socket.outgoing.get(), 2)
        self.assertEqual(cancel["type"], "task_cancel")
        self.assertEqual(cancel["task_id"], request["task_id"])
        await asyncio.wait_for(worker, 2)
        self.assertEqual(self.db.get_task_step(step_id)["status"], "cancelled")
        self.assertFalse(self.db.list_task_evidence(self.user_id, task_id))

    async def test_claimed_plan_is_not_replayed_after_restart(self) -> None:
        task = self.plans.save(self.user_id, self._draft(inspect=True))
        task_id, version = task["id"], task["plan"]["hash"]
        self.db.claim_task_plan(self.user_id, task_id, version)
        self.db.recover_interrupted_tasks()
        self.assertEqual(self.db.get_task(self.user_id, task_id)["status"], "failed")
        with self.assertRaises(ConflictError):
            self.plans.start(self.user_id, task_id, version)
        self.assertTrue(self.socket.outgoing.empty())

    async def test_policy_revocation_before_broker_grant_is_not_restored_by_readding_path(self) -> None:
        self.db.update_agent_plan_policy(self.user_id, self.agent_id, ["notes.txt"])
        task = self.plans.save(self.user_id, self._draft())
        task_id, version = task["id"], task["plan"]["hash"]
        self.assertFalse(task["plan"]["requires_approval"])
        self.plans.start(self.user_id, task_id, version)
        worker = self.plans.running[task_id]
        started = await asyncio.wait_for(self.socket.outgoing.get(), 3)
        self.db.update_agent_plan_policy(self.user_id, self.agent_id, [])
        self.db.update_agent_plan_policy(self.user_id, self.agent_id, ["notes.txt"])
        step = started["steps"][0]
        await self.hub.resolve_task(self.connection, {
            **step, "v": 2, "type": "task_action_request", "task_id": started["task_id"],
            "plan_hash": version, "action_request_id": "action_" + "a" * 32,
            "profile": "workspace.v1", "action": "write", "risk_level": "L1",
        })
        decision = await asyncio.wait_for(self.socket.outgoing.get(), 3)
        self.assertEqual(decision["type"], "task_action_decision")
        self.assertEqual(decision["decision"], "deny")
        await self.hub.resolve_task(self.connection, {
            "v": 2, "type": "task_result", "task_id": started["task_id"], "ok": False,
        })
        await asyncio.wait_for(worker, 3)
        self.assertEqual(self.db.get_task_step(step["step_id"])["error"], "device_changed")
        self.assertFalse(self.db.list_task_evidence(self.user_id, task_id))
        self.assertNotIn("action.authorized", {
            entry["event_type"] for entry in self.db.list_task_events(self.user_id, task_id)
        })

    async def test_revoked_plan_key_cannot_dispatch_an_already_queued_step(self) -> None:
        secret = issue_token("mcp")
        key = self.db.create_mcp_key(self.user_id, "controller", secret.prefix, secret.digest, scope="plan")
        self.db.update_agent_plan_policy(self.user_id, self.agent_id, ["notes.txt"])
        task = self.plans.save(self.user_id, self._draft(), key_id=key["id"])
        task_id, version = task["id"], task["plan"]["hash"]
        self.plans.start(self.user_id, task_id, version, key_id=key["id"])
        self.db.revoke_mcp_key(self.user_id, key["id"])
        await asyncio.wait_for(self.plans.running[task_id], 3)
        self.assertTrue(self.socket.outgoing.empty())
        self.assertEqual(self.db.get_task_step(task["steps"][0]["id"])["error"], "key_revoked")
        self.assertFalse(self.db.list_task_evidence(self.user_id, task_id))

    async def test_cyclic_plan_is_rejected(self) -> None:
        draft = self._draft()
        draft.steps[0].depends_on = ["first"]
        with self.assertRaisesRegex(ValueError, "不能有环"):
            self.plans.save(self.user_id, draft)
        self.assertEqual(self.db.list_tasks(self.user_id), [])


class ManagedActionTests(unittest.TestCase):
    def test_refuses_sensitive_path_and_nonpreset_command(self) -> None:
        for path in ("../notes.txt", ".git/config.ini", "tokens.json", "aux.txt", "docs/readme.md "):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "L3"):
                parse_planned_action("workspace.hash", {"path": path})
        with self.assertRaisesRegex(ValueError, "固定|只允许"):
            parse_planned_action("shell.version", {"tool": "unsupported"})
        with self.assertRaisesRegex(ValueError, "不含凭据"):
            parse_planned_action("workspace.create", {"path": "notes.txt", "content": "token = synthetic-fixture"})

    def test_workspace_alias_is_denied_on_the_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.ini").write_text("synthetic", encoding="utf-8")
            try:
                os.link(root / "source.ini", root / "alias.ini")
            except OSError as exc:
                self.skipTest(f"Hard links unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "不能经过链接"):
                check_workspace_target(root, "alias.ini")


if __name__ == "__main__":
    unittest.main()
