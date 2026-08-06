from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from remote_agent.core import AgentConfig, FatalAgentError, RemoteAgent, load_manifest
from remote_agent.profiles import build_profile_handlers


class RemoteAgentProfileTests(unittest.TestCase):
    def test_manifest_selects_actual_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"runtime": "python", "core_version": 1, "profiles": ["shell.v1"]}),
                encoding="utf-8",
            )
            manifest = load_manifest(manifest_path)
            handlers = build_profile_handlers(manifest.profiles, root)
            agent = RemoteAgent(
                gateway_url="ws://example.test/ws/agent",
                config_store=AgentConfig(root / "config.json"),
                enrollment_token="enroll_test",
                name="test",
                description="",
                handlers=handlers,
            )
        self.assertEqual(agent.profile_ids, ["shell.v1"])
        self.assertEqual(agent.hello_message()["profiles"], ["shell.v1"])

    def test_unknown_profile_cannot_be_declared_without_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FatalAgentError):
                build_profile_handlers(["gpio.v1"], Path(directory))


if __name__ == "__main__":
    unittest.main()
