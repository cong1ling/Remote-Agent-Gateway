from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    CURRENT_AGENT_VERSION,
    CURRENT_CORE_VERSION,
)
from gateway.profiles import SHELL_MAX_CAPTURE_BYTES, SHELL_TRUNCATION_MARKER
from remote_agent.core import (
    AGENT_VERSION,
    CORE_VERSION,
    AgentConfig,
    FatalAgentError,
    RemoteAgent,
    load_manifest,
)
from remote_agent.core.runtime import PROTOCOL_VERSION
from remote_agent.profiles import build_profile_handlers
from remote_agent.profiles.shell_v1 import MAX_CAPTURE_BYTES, TRUNCATION_MARKER


class RemoteAgentProfileTests(unittest.TestCase):
    def test_manifest_selects_actual_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "runtime": "python",
                        "agent_version": AGENT_VERSION,
                        "core_version": CORE_VERSION,
                        "build_id": "profile-test",
                        "profiles": ["shell.v1"],
                    }
                ),
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
                build_id=manifest.build_id,
                handlers=handlers,
            )
        self.assertEqual(agent.profile_ids, ["shell.v1"])
        hello = agent.hello_message()
        self.assertEqual(PROTOCOL_VERSION, AGENT_PROTOCOL_VERSION)
        self.assertEqual(AGENT_VERSION, CURRENT_AGENT_VERSION)
        self.assertEqual(CORE_VERSION, CURRENT_CORE_VERSION)
        self.assertEqual(hello["v"], 2)
        self.assertEqual(
            hello["version"],
            {
                "agent_version": "0.1.0",
                "core_version": 1,
                "protocol_version": 2,
                "build_id": "profile-test",
            },
        )
        self.assertEqual(hello["profiles"][0]["id"], "shell.v1")
        self.assertIn(
            hello["profiles"][0]["runtime"]["dialect"],
            {"powershell", "cmd", "posix-sh"},
        )
        self.assertEqual(MAX_CAPTURE_BYTES, SHELL_MAX_CAPTURE_BYTES)
        self.assertEqual(TRUNCATION_MARKER.decode("ascii"), SHELL_TRUNCATION_MARKER)

    def test_unknown_profile_cannot_be_declared_without_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FatalAgentError):
                build_profile_handlers(["gpio.v1"], Path(directory))


if __name__ == "__main__":
    unittest.main()
