from __future__ import annotations

import unittest

from gateway.agent_protocol import (
    CURRENT_AGENT_VERSION,
    CURRENT_CORE_VERSION,
    agent_version_status,
    validate_agent_features,
    validate_agent_version,
    validate_runtime_declarations,
)


class AgentVersionTests(unittest.TestCase):
    def test_version_declaration_is_validated(self) -> None:
        version = validate_agent_version(
            {
                "agent_version": "0.1.0",
                "core_version": 1,
                "protocol_version": 2,
                "build_id": "bundle-1234",
            },
            2,
        )
        self.assertEqual(version["build_id"], "bundle-1234")

        with self.assertRaisesRegex(ValueError, "握手协议版本不一致"):
            validate_agent_version(
                {
                    "agent_version": "0.1.0",
                    "core_version": 1,
                    "protocol_version": 1,
                    "build_id": "bundle-1234",
                },
                2,
            )

    def test_version_status_distinguishes_upgrade_and_incompatibility(self) -> None:
        self.assertEqual(
            agent_version_status(
                {
                    "agent_version": CURRENT_AGENT_VERSION,
                    "core_version": CURRENT_CORE_VERSION,
                    "protocol_version": 2,
                    "build_id": "current",
                }
            ),
            "current",
        )
        self.assertEqual(
            agent_version_status(
                {
                    "agent_version": "0.1.0",
                    "core_version": 1,
                    "protocol_version": 2,
                    "build_id": "old",
                }
            ),
            "update_available",
        )
        self.assertEqual(
            agent_version_status(
                {
                    "agent_version": CURRENT_AGENT_VERSION,
                    "core_version": CURRENT_CORE_VERSION,
                    "protocol_version": 1,
                    "build_id": "incompatible",
                }
            ),
            "incompatible",
        )
        self.assertEqual(agent_version_status({}), "unknown")

    def test_legacy_agent_has_disabled_optional_features(self) -> None:
        features = validate_agent_features(None)
        self.assertEqual(features["task_protocol"], 0)
        self.assertFalse(features["event_stream"])
        self.assertEqual(features["action_interception"], "none")

    def test_task_features_and_runtime_declarations_are_normalized(self) -> None:
        features = validate_agent_features(
            {
                "task_protocol": 1,
                "event_stream": True,
                "runtime_adapter": True,
                "action_interception": "limited",
                "evidence": True,
            }
        )
        self.assertEqual(features["task_protocol"], 1)
        runtimes = validate_runtime_declarations(
            [
                {
                    "provider_id": "codex",
                    "runtime_version": "0.169.0",
                    "adapter_version": "0.1.0",
                    "status": "available",
                    "mode": "managed_limited",
                    "supported_profiles": ["system_observe.v1"],
                }
            ]
        )
        self.assertEqual(runtimes[0]["provider_id"], "codex")
        with self.assertRaisesRegex(ValueError, "event_stream"):
            validate_agent_features({"task_protocol": 1})


if __name__ == "__main__":
    unittest.main()
