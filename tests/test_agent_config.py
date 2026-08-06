from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from remote_agent.core import AgentConfig, FatalAgentError


VALID_CONFIG = {
    "gateway_url": "ws://127.0.0.1:8000/ws/agent",
    "agent_id": "agent-test",
    "credential": "agent_secret",
}


class AgentConfigTests(unittest.TestCase):
    def test_new_enrollment_prepares_writable_directory_without_creating_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / ".agent" / "config.json"
            store = AgentConfig(path)
            saved = store.prepare_startup("enroll_test")
            self.assertIsNone(saved)
            self.assertTrue(path.parent.is_dir())
            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.iterdir()), [])

    def test_startup_rejects_missing_token_and_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentConfig(Path(directory) / "config.json")
            with self.assertRaisesRegex(FatalAgentError, "首次运行需要提供 --token"):
                store.prepare_startup(None)

    def test_existing_config_rejects_new_enrollment_token_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
            original = path.read_bytes()
            store = AgentConfig(path)
            with self.assertRaisesRegex(FatalAgentError, "已经绑定 Agent"):
                store.prepare_startup("enroll_new")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(store.prepare_startup(None), VALID_CONFIG)

    def test_unwritable_config_parent_fails_before_enrollment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "not-a-directory"
            blocker.write_text("blocked", encoding="utf-8")
            store = AgentConfig(blocker / "config.json")
            with self.assertRaisesRegex(FatalAgentError, "设备凭据位置不可写"):
                store.prepare_startup("enroll_test")

    def test_save_is_atomic_and_load_validates_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = AgentConfig(path)
            store.save(VALID_CONFIG)
            self.assertEqual(store.load(), VALID_CONFIG)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(FatalAgentError, "gateway_url"):
                store.load()


if __name__ == "__main__":
    unittest.main()
