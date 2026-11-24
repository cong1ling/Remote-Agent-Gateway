from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from gateway.database import ConflictError, Database
from gateway.security import hash_password, hash_token, issue_token, verify_password


SHELL_DECLARATIONS = [
    {
        "id": "shell.v1",
        "runtime": {
            "os": "windows",
            "dialect": "powershell",
            "executable": "powershell.exe",
        },
    }
]
AGENT_VERSION = {
    "agent_version": "0.1.0",
    "core_version": 1,
    "protocol_version": 2,
    "build_id": "test-build",
}


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create_user(self, email: str = "owner@example.com") -> dict:
        return self.db.create_user(email, hash_password("correct horse battery staple"))

    def test_password_hash_is_salted_and_verifiable(self) -> None:
        first = hash_password("correct horse battery staple")
        second = hash_password("correct horse battery staple")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("wrong password", first))

    def test_email_is_unique_case_insensitively(self) -> None:
        self.create_user("Owner@Example.com")
        with self.assertRaises(ConflictError):
            self.create_user("owner@example.com")

    def test_enrollment_token_is_consumed_once(self) -> None:
        user = self.create_user()
        enrollment = issue_token("enroll")
        self.db.create_enrollment_token(
            user["id"], "notebook", enrollment.prefix, enrollment.digest, int(time.time()) + 60
        )

        credential = issue_token("agent")
        agent_id = str(uuid.uuid4())
        registered = self.db.register_agent(
            enrollment.digest,
            agent_id,
            credential.prefix,
            credential.digest,
            "gpu-notebook",
            "模型训练环境",
            SHELL_DECLARATIONS,
            AGENT_VERSION,
        )
        self.assertEqual(registered["user_id"], user["id"])
        self.assertIsNone(
            self.db.register_agent(
                enrollment.digest,
                str(uuid.uuid4()),
                credential.prefix,
                hash_token("another"),
                "duplicate",
                "",
                SHELL_DECLARATIONS,
                AGENT_VERSION,
            )
        )

    def test_agent_queries_are_owner_scoped(self) -> None:
        owner = self.create_user()
        other = self.create_user("other@example.com")
        enrollment = issue_token("enroll")
        self.db.create_enrollment_token(
            owner["id"], "agent", enrollment.prefix, enrollment.digest, int(time.time()) + 60
        )
        credential = issue_token("agent")
        agent_id = str(uuid.uuid4())
        self.db.register_agent(
            enrollment.digest,
            agent_id,
            credential.prefix,
            credential.digest,
            "private-agent",
            "",
            SHELL_DECLARATIONS,
            AGENT_VERSION,
        )

        agent = self.db.get_owned_agent(owner["id"], agent_id)
        self.assertIsNotNone(agent)
        self.assertEqual(agent["profiles"], ["shell.v1"])
        self.assertEqual(agent["profile_declarations"], SHELL_DECLARATIONS)
        self.assertEqual(agent["version"], AGENT_VERSION)
        self.assertEqual(agent["shell_policy_mode"], "off")
        self.assertTrue(self.db.update_agent_shell_policy(owner["id"], agent_id, "standard"))
        self.assertEqual(
            self.db.get_owned_agent(owner["id"], agent_id)["shell_policy_mode"], "standard"
        )
        self.assertIsNone(self.db.get_owned_agent(other["id"], agent_id))
        self.assertEqual(self.db.list_agents(other["id"]), [])
        self.assertTrue(self.db.revoke_agent(owner["id"], agent_id))
        self.assertEqual(self.db.list_agents(owner["id"]), [])

    def test_legacy_profile_rows_are_not_treated_as_protocol_v2_capabilities(self) -> None:
        user = self.create_user()
        enrollment = issue_token("enroll")
        self.db.create_enrollment_token(
            user["id"], "legacy", enrollment.prefix, enrollment.digest, int(time.time()) + 60
        )
        credential = issue_token("agent")
        agent_id = str(uuid.uuid4())
        self.db.register_agent(
            enrollment.digest,
            agent_id,
            credential.prefix,
            credential.digest,
            "legacy-agent",
            "",
            ["shell.v1"],  # type: ignore[arg-type]
            {},
        )

        agent = self.db.get_owned_agent(user["id"], agent_id)
        self.assertEqual(agent["profiles"], [])
        self.assertEqual(agent["profile_declarations"], [])
        self.assertEqual(agent["version"], {})

    def test_initialize_migrates_existing_agent_table_with_safe_directory_defaults(self) -> None:
        path = Path(self.tempdir.name) / "legacy.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                """
                CREATE TABLE agents (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    profiles_json TEXT NOT NULL,
                    credential_prefix TEXT NOT NULL,
                    credential_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    revoked_at INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO agents (id, user_id, name, profiles_json, credential_prefix, "
                "credential_hash, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("existing", "owner", "legacy", "[]", "agent_legacy", "digest", 1, 1),
            )
            connection.commit()
        database = Database(path)
        database.initialize()
        database.initialize()
        with closing(sqlite3.connect(path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agents)")}
        self.assertIn("version_json", columns)
        self.assertIn("shell_policy_mode", columns)
        self.assertIn("plan_l1_paths_json", columns)
        self.assertIn("plan_policy_revision", columns)
        agent = database.get_owned_agent("owner", "existing")
        self.assertEqual(agent["device"], {})
        self.assertEqual(agent["tags"], [])
        self.assertIsNone(agent["environment"])

    def test_revoked_mcp_key_cannot_authenticate(self) -> None:
        user = self.create_user()
        token = issue_token("mcp")
        key = self.db.create_mcp_key(user["id"], "Claude", token.prefix, token.digest)
        verified = self.db.verify_mcp_key(token.digest)
        self.assertEqual(verified["user_id"], user["id"])
        self.assertEqual(verified["scope"], "legacy")
        self.assertTrue(self.db.revoke_mcp_key(user["id"], key["id"]))
        self.assertIsNone(self.db.verify_mcp_key(token.digest))

    def test_task_events_are_owner_scoped_and_idempotent(self) -> None:
        user = self.create_user()
        task = self.db.create_task(user["id"], "健康检查", target_selector={"agent_ids": ["a1"]})
        event = self.db.append_task_event(task["id"], "task.created", {"secret": "ignored"})
        same_event = self.db.append_task_event(
            task["id"],
            "task.created",
            {"different": True},
            event_id=event["id"],
        )
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(same_event["sequence"], event["sequence"])
        self.assertEqual(
            self.db.list_task_events(user["id"], task["id"])[0]["payload"]["secret"],
            "ignored",
        )
        other = self.create_user("other-task@example.com")
        self.assertEqual(self.db.list_task_events(other["id"], task["id"]), [])

    def test_task_action_idempotency_and_evidence_persistence(self) -> None:
        user = self.create_user()
        task = self.db.create_task(user["id"], "健康检查")
        step = self.db.create_task_step(
            step_id="step-1",
            task_id=task["id"],
            action_type="system_observe.v1/get_host_info",
            idempotency_key="task:step:host",
            target_agent_id="agent-1",
            inputs={"profile": "system_observe.v1", "action": "get_host_info"},
        )
        self.assertEqual(step["inputs"]["profile"], "system_observe.v1")
        self.db.create_task_action(
            action_id="action-1",
            task_id=task["id"],
            step_id=step["id"],
            agent_id="agent-1",
            profile="system_observe.v1",
            action="get_host_info",
            params={},
            idempotency_key="task:step:host:action",
        )
        with self.assertRaises(ConflictError):
            self.db.create_task_action(
                action_id="action-2",
                task_id=task["id"],
                step_id=step["id"],
                agent_id="agent-1",
                profile="system_observe.v1",
                action="get_host_info",
                params={},
                idempotency_key="task:step:host:action",
            )
        evidence = self.db.create_task_evidence(
            evidence_id="evidence-1",
            task_id=task["id"],
            kind="health_check",
            summary={"status": "healthy"},
            content_hash="a" * 64,
            agent_id="agent-1",
        )
        self.assertEqual(evidence["summary"]["status"], "healthy")

    def test_legacy_task_alert_migrates_without_overwriting_new_status(self) -> None:
        owner = self.create_user()
        task = self.db.create_task(owner["id"], "Legacy alert task")
        with self.db.connect() as connection:
            connection.execute(
                """
                CREATE TABLE task_alerts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent_id TEXT, severity TEXT NOT NULL,
                    category TEXT NOT NULL, trigger TEXT NOT NULL, recommendation TEXT NOT NULL,
                    status TEXT NOT NULL, created_at INTEGER NOT NULL, resolved_at INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO task_alerts VALUES (?, ?, NULL, 'warning', 'legacy', 'Legacy', 'Review', 'open', 1, NULL)",
                ("legacy-alert", task["id"]),
            )
        self.db.initialize()
        self.assertEqual(self.db.list_task_alerts(owner["id"], task["id"])[0]["status"], "open")
        self.assertTrue(self.db.update_alert(owner["id"], "legacy-alert", previous="open", status="acknowledged"))
        self.db.initialize()
        alert = self.db.alert_detail(owner["id"], "legacy-alert")
        self.assertEqual(alert["status"], "acknowledged")
        self.assertEqual(alert["history"][0]["actor_user_id"], owner["id"])
        self.assertEqual(alert["history"][0]["previous_status"], "open")

    def test_interrupted_health_tasks_close_pending_actions_and_keep_verified_evidence(self) -> None:
        user_id = self.create_user()["id"]
        enrollment, device = issue_token("enroll"), issue_token("agent")
        self.db.create_enrollment_token(user_id, "test", enrollment.prefix, enrollment.digest, int(time.time()) + 60)
        self.db.register_agent(
            enrollment.digest, "unverified", device.prefix, device.digest, "Interrupted device", "",
            SHELL_DECLARATIONS, AGENT_VERSION,
        )
        healthy = self.db.create_task(user_id, "Interrupted healthy task", status="running", risk_policy={"mode": "read_only"})
        queued = self.db.create_task(user_id, "Interrupted queued task", status="queued", risk_policy={"mode": "read_only"})
        step = self.db.create_task_step(
            step_id="interrupted-step", task_id=healthy["id"], action_type="system_observe.v1/get_host_info",
            idempotency_key="interrupted-step", target_agent_id="unverified",
        )
        action = self.db.create_task_action(
            action_id="interrupted-action", task_id=healthy["id"], step_id=step["id"], agent_id="unverified",
            profile="system_observe.v1", action="get_host_info", params={}, idempotency_key="interrupted-action",
        )
        self.db.update_task_step(step["id"], status="running")
        self.db.update_task_action(action["id"], status="allow")
        self.db.create_task_evidence(
            evidence_id="prior-evidence", task_id=healthy["id"], kind="health_check",
            summary={"status": "healthy"}, content_hash="a" * 64, agent_id="verified",
        )
        self.db.create_runtime_session(
            session_id="interrupted-session", agent_id="unverified", provider_id="codex",
            runtime_version="fixture", adapter_version="fixture", mode="managed_limited", status="running",
        )
        self.db.recover_interrupted_tasks()
        self.db.recover_interrupted_tasks()
        self.assertEqual(self.db.get_task(user_id, queued["id"])["status"], "failed")
        self.assertEqual(self.db.get_task(user_id, healthy["id"])["status"], "partially_completed")
        self.assertEqual(self.db.get_task_step(step["id"])["error"], "gateway_interrupted")
        self.assertEqual(self.db.get_task_action_by_idempotency("interrupted-action")["status"], "failed")
        self.assertEqual(self.db.get_runtime_session("interrupted-session")["status"], "failed")
        self.assertEqual(len(self.db.list_task_evidence(user_id, healthy["id"])), 1)
        events = self.db.list_task_events(user_id, healthy["id"])
        self.assertEqual([event["event_type"] for event in events], ["task.interrupted"])
        self.assertFalse(events[0]["payload"]["automatic_retry"])


if __name__ == "__main__":
    unittest.main()
