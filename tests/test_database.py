from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path

from gateway.database import ConflictError, Database
from gateway.security import hash_password, hash_token, issue_token, verify_password


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
            ["shell.v1"],
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
                ["shell.v1"],
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
            ["shell.v1"],
        )

        self.assertIsNotNone(self.db.get_owned_agent(owner["id"], agent_id))
        self.assertIsNone(self.db.get_owned_agent(other["id"], agent_id))
        self.assertEqual(self.db.list_agents(other["id"]), [])
        self.assertTrue(self.db.revoke_agent(owner["id"], agent_id))
        self.assertEqual(self.db.list_agents(owner["id"]), [])

    def test_revoked_mcp_key_cannot_authenticate(self) -> None:
        user = self.create_user()
        token = issue_token("mcp")
        key = self.db.create_mcp_key(user["id"], "Claude", token.prefix, token.digest)
        self.assertEqual(self.db.verify_mcp_key(token.digest)["user_id"], user["id"])
        self.assertTrue(self.db.revoke_mcp_key(user["id"], key["id"]))
        self.assertIsNone(self.db.verify_mcp_key(token.digest))


if __name__ == "__main__":
    unittest.main()
