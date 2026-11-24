from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gateway.runtime_catalog import HEALTH_RUNTIME_CATALOG
from remote_agent.runtimes.base import ActionDecision, RuntimeTask
from remote_agent.runtimes.claude import CLAUDE_FIXTURE_KEY, CLAUDE_KEY_ENV, ClaudeRuntime, _parse_actions
from remote_agent.runtimes.health import OBSERVATION_TOOLS


def _cli_record(plan: dict) -> dict:
    return {
        "type": "result", "subtype": "success", "stop_reason": "end_turn", "is_error": False,
        "permission_denials": [], "num_turns": 1, "result": json.dumps(plan),
    }


class ClaudeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_only_exact_successful_plan_emits_the_fixed_actions(self) -> None:
        actions = list(reversed(OBSERVATION_TOOLS))
        record = _cli_record({"actions": actions})
        self.assertEqual(_parse_actions(json.dumps(record).encode()), actions)
        for invalid in (
            {**record, "stop_reason": "max_tokens"},
            {**record, "num_turns": True},
            {**record, "permission_denials": [{"reason": "not permitted"}]},
            {**record, "result": '{"actions":[],"actions":[]}'},
            _cli_record({"actions": actions[:3] + [actions[0]]}),
            _cli_record({"actions": actions, "args": {}}),
            _cli_record({"actions": [*actions[:3], {"action": actions[3]}]}),
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "四个无参"):
                _parse_actions(json.dumps(invalid).encode())

    def test_fixture_mode_never_reads_the_dedicated_production_credential(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {CLAUDE_KEY_ENV: "dedicated-example", "ANTHROPIC_API_KEY": "ambient-example"}),
            patch("subprocess.run") as version_check,
        ):
            workdir = Path(root) / "work"
            workdir.mkdir()
            executable = Path(root) / ("fixture.exe" if os.name == "nt" else "fixture")
            executable.touch()
            executable.chmod(0o700)
            version_check.return_value.stdout = "2.1.306 (Claude Code)\n"
            runtime = ClaudeRuntime.from_installation(
                executable, "claude-sonnet-4-6", workdir=workdir, fixture_port=8001,
            )
            self.assertTrue(HEALTH_RUNTIME_CATALOG["claude"].accepts(runtime.declaration()))
            self.assertNotIn("ANTHROPIC_API_KEY", version_check.call_args.kwargs["env"])
            fixture_env = runtime._environment(str(workdir), model_access=True)
            self.assertEqual(fixture_env["ANTHROPIC_API_KEY"], CLAUDE_FIXTURE_KEY)
            self.assertEqual(fixture_env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8001")
            self.assertNotIn(CLAUDE_KEY_ENV, fixture_env)
            runtime = ClaudeRuntime.from_installation(executable, "claude-sonnet-4-6", workdir=workdir)
            live_env = runtime._environment(str(workdir), model_access=True)
            self.assertEqual(live_env["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
            self.assertEqual(live_env["ANTHROPIC_API_KEY"], "dedicated-example")
            with self.assertRaisesRegex(ValueError, "设备凭据"):
                ClaudeRuntime.from_installation(
                    executable, "claude-sonnet-4-6", workdir=workdir, config_env=CLAUDE_KEY_ENV.lower(),
                )
            with self.assertRaisesRegex(ValueError, "工作区外"):
                ClaudeRuntime.from_installation(executable, "claude-sonnet-4-6", workdir=Path(root))
            version_check.return_value.stdout = "2.1.303 (Claude Code)\n"
            with self.assertRaisesRegex(ValueError, "仅支持"):
                ClaudeRuntime.from_installation(executable, "claude-sonnet-4-6", workdir=workdir)

    async def test_each_request_has_to_get_the_matching_broker_decision(self) -> None:
        runtime = ClaudeRuntime(Path("fixture"), "claude-sonnet-4-6", api_key=CLAUDE_FIXTURE_KEY, fixture_port=8001)
        actions = list(OBSERVATION_TOOLS)
        session = await runtime.start(RuntimeTask("task", "private title", "untrusted prompt", tuple(actions)))
        self.addAsyncCleanup(runtime.close, session)
        with patch.object(runtime, "_request_plan", AsyncMock(return_value=actions.copy())):
            async for event in runtime.events(session):
                if event.event_type == "turn_completed":
                    break
                with self.assertRaisesRegex(ValueError, "与会话不匹配"):
                    await runtime.decide_action(session, ActionDecision("another-request", "allow"))
                self.assertEqual(event.payload, {"action": actions.pop(0)})
                await runtime.decide_action(session, ActionDecision(event.action_request_id, "deny"))
                with self.assertRaisesRegex(ValueError, "与会话不匹配"):
                    await runtime.decide_action(session, ActionDecision(event.action_request_id, "allow"))
        self.assertEqual(actions, [])
        self.assertEqual(session.state, "completed")
