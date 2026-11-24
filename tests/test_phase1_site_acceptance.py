from __future__ import annotations

import hashlib
import io
import json
import logging
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from httpx import AsyncClient

from gateway.agent_hub import AgentConnection, AgentHub
from gateway.app import create_app
from gateway.config import Settings
from gateway.security import hash_password, issue_token
from remote_agent.core.task_runtime import TaskRuntime
from remote_agent.runtimes.codex import CodexRuntime
from tests import phase1_site_acceptance as site
from tests.codex_health_provider import HEALTH_TOOLS
from tests.test_codex_runtime import FakeJsonRpcClient
from tests.test_health_check_service import _result_for


GATEWAY = "https://private-gateway.example.test:8443"
AGENT_IDS = [f"10000000-0000-4000-8000-{index:012x}" for index in range(1, 4)]
SYSTEMS = {"windows": "Windows", "macos": "Darwin", "linux": "Linux"}
PROFILE = "system_observe.v1"
CODEX = {"provider_id": "codex", "runtime_version": "0.169.0", "adapter_version": "0.1.0", "mode": "managed_limited"}
EMAIL = "operator@example.test"
PASSWORD = "mock-password-never-print"
SESSION = "session_" + "X" * 43
HOSTNAME = "private-device.example.test"
ADDRESS = "192.0.2.18"


def _digest(summary: dict) -> str:
    return hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _observation(action: str, platform: str) -> dict:
    result = _result_for(action)
    if action == "get_host_info":
        result.update(os=platform, system=SYSTEMS[platform], hostname=HOSTNAME)
    elif action == "get_network":
        result.update(hostname=HOSTNAME, addresses=[ADDRESS])
    elif action == "get_resources" and platform == "macos":
        result["memory"] = {"total_bytes": 100}
    return result


class TaskFixture:
    def __init__(self, runtime_id: str | None) -> None:
        start = {None: 0, "codex": 100, "claude": 200}[runtime_id]
        self.id = f"task_{start:032x}"
        self.endpoint = f"/api/tasks/{self.id}"
        self.events: list[dict] = []
        self.evidence: list[dict] = []
        steps: list[dict] = []
        outcomes: list[dict] = []
        counts = {"healthy": 2, "warning": 1, "critical": 0, "failed": 0}
        self._event("task.created", {"kind": "health_check"})
        self._event("task.started", {})
        for index, (agent_id, platform) in enumerate(zip(AGENT_IDS, SYSTEMS, strict=True), start=start + 1):
            runtime = {**site.RUNTIME_EVIDENCE[runtime_id], "session_id": f"runtime_{index:032x}"} if runtime_id else None
            self._event("device.started", {"agent_id": agent_id}, agent_id=agent_id)
            if runtime is not None:
                self._event("agent.runtime_started", deepcopy(runtime), agent_id=agent_id, remote_sequence=1)
            observations = {}
            for action_index, action in enumerate(HEALTH_TOOLS, start=1):
                number = index * len(HEALTH_TOOLS) + action_index
                step_id = f"step_{number:032x}"
                request_id = f"action_{number:032x}"
                result = _observation(action, platform)
                observations[action] = result
                steps.append({
                    "id": step_id, "task_id": self.id, "target_agent_id": agent_id,
                    "status": "completed", "action_type": f"{PROFILE}/{action}",
                    "inputs": {"profile": PROFILE, "action": action, "params": {}, "mcp_tool": f"remote_{action}"},
                    "expected_result": {"read_only": True}, "result": deepcopy(result),
                })
                scope = {"profile": PROFILE, "action": action}
                self._event(
                    "action.requested", {**scope, "action_request_id": request_id, "policy": {
                        "decision": "allow", "risk_level": "L0", "allowed_scope": scope,
                        "matched_rules": ["health_check.read_only"], "reason": "fixed observations",
                    }}, agent_id=agent_id, step_id=step_id,
                )
                self._event(
                    "agent.action_completed", {**scope, "action_request_id": request_id, "step_id": step_id},
                    agent_id=agent_id, step_id=step_id, remote_sequence=action_index + int(runtime_id is not None),
                )
            if runtime is not None:
                self._event(
                    "agent.runtime_finished", {"session_id": runtime["session_id"], "status": "completed"},
                    agent_id=agent_id, remote_sequence=6,
                )
            self._event("agent.result", {"ok": True}, agent_id=agent_id)
            summary = {
                "agent_id": agent_id, "status": "warning" if platform == "macos" else "healthy",
                "observations": observations, "runtime": runtime,
                "alerts": [{"category": "memory_observation", "severity": "warning"}] if platform == "macos" else [],
            }
            self.evidence.append({
                "id": f"evidence_{index:032x}", "task_id": self.id, "kind": "health_check",
                "agent_id": agent_id, "step_id": None, "content_hash": _digest(summary), "summary": summary,
            })
            outcomes.append(deepcopy(summary))
            self._event("device.completed", {"agent_id": agent_id, "status": summary["status"]}, agent_id=agent_id)
        self._event("task.completed", {"status": "completed", "summary": deepcopy(counts)})
        self.detail = {
            "id": self.id, "status": "completed", "target_selector": {"agent_ids": AGENT_IDS.copy()},
            "requested_runtime": runtime_id, "risk_policy": {"max_level": "L0", "mode": "read_only"},
            "approval_policy": {"required": False},
            "resource_budget": {"max_devices": 3, "max_actions_per_device": 4, "timeout_seconds": 120},
            "steps": steps, "result": {"task_id": self.id, "devices": outcomes, "summary": counts},
        }
        self.created = {**deepcopy(self.detail), "status": "queued", "result": None}

    def _event(
        self, kind: str, payload: dict, *, agent_id: str | None = None, step_id: str | None = None,
        remote_sequence: int | None = None,
    ) -> None:
        sequence = len(self.events) + 1
        event_id = f"event_{sequence:032x}"
        if remote_sequence is not None:
            payload = {"remote_event_id": event_id, "remote_sequence": remote_sequence, "payload": payload}
        self.events.append({
            "id": event_id, "task_id": self.id, "sequence": sequence, "event_type": kind,
            "agent_id": agent_id, "step_id": step_id, "payload": payload,
        })

    def first_event(self, event_type: str) -> dict:
        return next(event for event in self.events if event["event_type"] == event_type)


class GatewayFixture:
    def __init__(self) -> None:
        self.agents = {}
        for agent_id, platform in zip(AGENT_IDS, SYSTEMS, strict=True):
            self.agents[agent_id] = {
                "id": agent_id, "online": True, "trust_level": "owner_enrolled",
                "device": {"device_type": "computer", "platform": platform, "platform_version": "mock"},
                "version": {"agent_version": "0.2.3", "core_version": 6, "protocol_version": 2, "build_id": HOSTNAME},
                "features": {"task_protocol": 1, "event_stream": True, "runtime_adapter": True, "action_interception": "limited"},
                "health_runtimes": ["codex"],
                "capability_ids": [PROFILE, *(f"{PROFILE}/{action}" for action in HEALTH_TOOLS)],
                "profile_declarations": [{"id": PROFILE, "runtime": {
                    "platform": platform, "scope": "host", "mode": "read_only", "actions": list(HEALTH_TOOLS),
                }}],
                "runtimes": [{**CODEX, "status": "available", "supported_profiles": [PROFILE]}],
            }
        self.native = TaskFixture(None)
        self.codex = TaskFixture("codex")
        self.claude = TaskFixture("claude")
        self.requests: list[tuple[str, str]] = []
        self.submissions: list[dict] = []
        self.overrides: dict[str, httpx.Response] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https" and request.url.host == "private-gateway.example.test"
        assert request.url.port == 8443 and not request.url.query
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if path in self.overrides:
            return self.overrides[path]
        if (method, path) == ("POST", "/api/auth/login"):
            assert json.loads(request.content) == {"email": EMAIL, "password": PASSWORD}
            return httpx.Response(200, json={"user": {"email": EMAIL}}, headers={
                "set-cookie": f"remote_gateway_session={SESSION}; Path=/; HttpOnly; Secure",
            })
        cookies = SimpleCookie(request.headers.get("cookie", ""))
        assert cookies["remote_gateway_session"].value == SESSION
        if (method, path) == ("GET", "/api/session"):
            return httpx.Response(200, json={"authenticated": True, "mcp_url": GATEWAY + "/mcp/"})
        if method == "GET" and path.startswith("/api/agents/"):
            agent = self.agents.get(path.removeprefix("/api/agents/"))
            return httpx.Response(200, json={"agent": agent}) if agent is not None else httpx.Response(404)
        if (method, path) == ("POST", "/api/tasks"):
            submitted = json.loads(request.content)
            self.submissions.append(submitted)
            task = {None: self.native, "codex": self.codex, "claude": self.claude}[submitted["requested_runtime"]]
            return httpx.Response(202, json={"task": task.created})
        assert method == "GET"
        for task in (self.native, self.codex, self.claude):
            if path == task.endpoint:
                return httpx.Response(200, json={"task": task.detail})
            if path == task.endpoint + "/events":
                return httpx.Response(200, json={"events": task.events})
            if path == task.endpoint + "/evidence":
                return httpx.Response(200, json={"evidence": task.evidence})
        raise AssertionError("Unexpected request")


class InProcessAgent:
    def __init__(self, hub: AgentHub, agent: dict, user_id: str) -> None:
        self.hub = hub
        self.connection = AgentConnection(
            agent["id"], user_id, self, features=agent["features"], runtimes=agent["runtimes"],
        )
        self.platform = agent["device"]["platform"]
        with patch("remote_agent.runtimes.codex.sanitized_environment", return_value={}):
            codex = CodexRuntime(Path("not-a-real-binary"), Path("not-a-real-home"), client_factory=self.fake_app_server)
        profile = SimpleNamespace(actions=HEALTH_TOOLS, execute=self.observe, cancel=AsyncMock())
        self.tasks = TaskRuntime(self, {PROFILE: profile}, {"codex": codex})

    @staticmethod
    async def fake_app_server(*args: object, **kwargs: object) -> FakeJsonRpcClient:
        calls = [{
            "id": index, "method": "item/tool/call",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1", "callId": f"call-{index}",
                "tool": action, "arguments": {},
            },
        } for index, action in enumerate(HEALTH_TOOLS, start=1)]
        return FakeJsonRpcClient([*calls, {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        }])

    async def observe(self, action: str, request_id: str, params: dict) -> dict:
        assert not params
        return _observation(action, self.platform)

    async def send_json(self, message: dict) -> None:
        if message["type"] == "task_start":
            await self.tasks.dispatch(self, message)
        elif message["type"] == "task_action_decision":
            await self.tasks.decide(message)
        elif message["type"] == "task_cancel":
            await self.tasks.cancel(message["task_id"])
        else:
            raise AssertionError("Unexpected device message")

    async def send(self, websocket: InProcessAgent, message: dict) -> None:
        await self.hub.resolve_task(self.connection, message)

    async def close(self, code: int, reason: str) -> None:
        await self.tasks.shutdown()


class Phase1SiteWireContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_routes_with_mock_agents_and_codex_app_server(self) -> None:
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.CRITICAL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(Settings(
                host="127.0.0.1", port=8443, public_url=GATEWAY, data_dir=root, database_path=root / "test.db",
                session_days=1, enrollment_minutes=30, max_command_timeout=3600, secure_cookies=True,
            ))
            async with app.router.lifespan_context(app):
                db = app.state.database
                hub = app.state.agent_hub
                user = db.create_user(EMAIL, hash_password(PASSWORD))
                for agent in GatewayFixture().agents.values():
                    enrollment, credential = issue_token("enroll"), issue_token("agent")
                    db.create_enrollment_token(user["id"], "Fixture", enrollment.prefix, enrollment.digest, int(time.time()) + 60)
                    self.assertIsNotNone(db.register_agent(
                        enrollment.digest, agent["id"], credential.prefix, credential.digest, "Fixture", "",
                        agent["profile_declarations"], agent["version"], agent["features"], agent["runtimes"],
                        device=agent["device"],
                    ))
                    await hub.register(InProcessAgent(hub, agent, user["id"]).connection)
                with (
                    patch.object(site.httpx, "AsyncClient", side_effect=lambda **kwargs: AsyncClient(
                        **kwargs, transport=httpx.ASGITransport(app=app),
                    )),
                    patch("builtins.input", return_value=EMAIL),
                    patch.object(site.getpass, "getpass", return_value=PASSWORD),
                    patch.object(site, "POLL_INTERVAL", 0.01),
                    redirect_stderr(io.StringIO()),
                ):
                    report = await site._run(GATEWAY, AGENT_IDS, False, 10)
        native, codex = report["tasks"]
        self.assertNotEqual(native["task_id"], codex["task_id"])
        self.assertEqual([record["platform"] for record in native["devices"]], list(SYSTEMS))
        self.assertTrue(all(record["runtime"] is None for record in native["devices"]))
        self.assertEqual(len({record["runtime"]["session_id"] for record in codex["devices"]}), 3)


class Phase1SiteAcceptanceTests(unittest.TestCase):
    def test_second_runtime_is_opt_in_and_requires_matching_device_and_session_evidence(self) -> None:
        gateway = GatewayFixture()
        status, output, _ = self._run(gateway, options=("--verify-claude",))
        self.assertEqual((status, output, gateway.submissions), (1, "", []))
        for agent in gateway.agents.values():
            agent["runtimes"].append({**site.RUNTIME_EVIDENCE["claude"], "status": "available", "supported_profiles": [PROFILE]})
            agent["health_runtimes"].append("claude")
        status, output, errors = self._run(gateway, options=("--verify-claude",))
        self.assertEqual(status, 0, errors)
        native, codex, claude = json.loads(output)["tasks"]
        self.assertEqual((native["mode"], codex["mode"], claude["mode"]), ("native", "codex", "claude"))
        self.assertEqual({device["runtime"]["provider_id"] for device in claude["devices"]}, {"claude"})

    def _run(
        self, gateway: GatewayFixture, *, url: str = GATEWAY, ids: list[str] | None = None,
        options: tuple[str, ...] = (), use_session: bool = False, poll_interval: float = 0,
    ) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        args = ["--gateway", url, "--agent-ids", *(AGENT_IDS if ids is None else ids), *options]
        if use_session:
            args.append("--use-session")
        with (
            patch.object(site.httpx, "AsyncClient", side_effect=lambda **kwargs: AsyncClient(
                **kwargs, transport=httpx.MockTransport(gateway.handle),
            )) as client,
            patch("builtins.input", return_value=EMAIL) as email_input,
            patch.object(site.getpass, "getpass", return_value=SESSION if use_session else PASSWORD) as hidden_input,
            patch.object(site, "POLL_INTERVAL", poll_interval),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = site.main(args)
        if client.called:
            client.assert_called_once()
            self.assertIs(client.call_args.kwargs["verify"], True)
            self.assertIs(client.call_args.kwargs["trust_env"], False)
            self.assertIs(client.call_args.kwargs["follow_redirects"], False)
        else:
            email_input.assert_not_called()
            hidden_input.assert_not_called()
        if use_session:
            email_input.assert_not_called()
        for text in (output.getvalue(), errors.getvalue()):
            for private in (GATEWAY, "private-gateway.example.test", EMAIL, PASSWORD, SESSION, HOSTNAME, ADDRESS, "observations"):
                self.assertNotIn(private, text)
        return status, output.getvalue(), errors.getvalue()

    def _assert_rejected(self, gateway: GatewayFixture, expected: str, **kwargs: object) -> None:
        status, output, errors = self._run(gateway, **kwargs)
        self.assertNotEqual(status, 0)
        self.assertEqual(output, "")
        self.assertIn(expected, errors)

    def test_reports_only_allowlisted_fields_for_two_fixed_health_tasks(self) -> None:
        gateway = GatewayFixture()
        status, output, errors = self._run(gateway, ids=list(reversed(AGENT_IDS)))
        self.assertEqual(status, 0, errors)
        self.assertEqual(gateway.submissions, [
            {"kind": "health_check", "agent_ids": AGENT_IDS, "requested_runtime": runtime}
            for runtime in (None, "codex")
        ])
        self.assertEqual([path for method, path in gateway.requests if method == "POST"], [
            "/api/auth/login", "/api/tasks", "/api/tasks",
        ])
        self.assertEqual([path for method, path in gateway.requests if path.startswith("/api/agents/")], [
            f"/api/agents/{agent_id}" for agent_id in reversed(AGENT_IDS)
        ])
        report = json.loads(output)
        self.assertEqual(set(report), {"tasks", "manual_checks_required"})
        self.assertEqual(len(report["manual_checks_required"]), 3)
        for actual, source, mode in zip(report["tasks"], (gateway.native, gateway.codex), ("native", "codex"), strict=True):
            self.assertEqual(actual["task_id"], source.id)
            self.assertEqual(actual["mode"], mode)
            for device, record, platform in zip(actual["devices"], source.evidence, SYSTEMS, strict=True):
                self.assertEqual(device, {
                    "agent_id": record["agent_id"], "platform": platform, "agent_version": "0.2.3",
                    "core_version": 6, "protocol_version": 2,
                    "evidence_hash": record["content_hash"], "runtime": record["summary"]["runtime"],
                })

    def test_can_reuse_a_session_without_a_password_or_login_request(self) -> None:
        gateway = GatewayFixture()
        status, _, errors = self._run(gateway, use_session=True)
        self.assertEqual(status, 0, errors)
        self.assertEqual(gateway.requests[0], ("GET", "/api/session"))
        self.assertNotIn(("POST", "/api/auth/login"), gateway.requests)

    def test_only_local_loopback_may_use_http(self) -> None:
        self.assertEqual(site._gateway_origin("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")
        self.assertEqual(site._gateway_origin("http://[::1]:8000"), "http://[::1]:8000")
        self.assertEqual(site._gateway_origin("http://localhost:8000"), "http://127.0.0.1:8000")
        for url in (
            "http://private-gateway.example.test", "http://192.0.2.18", "http://127.1", "http://localhost.example.test",
            f"https://{EMAIL}:{PASSWORD}@private-gateway.example.test", GATEWAY + "/subpath", GATEWAY + "?",
            GATEWAY + "#", "https://private-gateway.example.test\\other.example.test", GATEWAY + "\n",
        ):
            with self.subTest(url=url):
                gateway = GatewayFixture()
                self._assert_rejected(gateway, "loopback", url=url)
                self.assertEqual(gateway.requests, [])

    def test_rejects_duplicate_invalid_or_absent_ids_before_login(self) -> None:
        for ids in ([AGENT_IDS[0]] * 3, [*AGENT_IDS[:2], SESSION], []):
            with self.subTest(count=len(ids)):
                gateway = GatewayFixture()
                expected = "distinct" if ids else "Invalid arguments"
                self._assert_rejected(gateway, expected, ids=ids)
                self.assertEqual(gateway.requests, [])

    def test_login_and_session_failures_never_dispatch(self) -> None:
        for path, response, expected in (
            ("/api/auth/login", httpx.Response(401, text=f"{PASSWORD} {GATEWAY}"), "HTTP 401"),
            ("/api/auth/login", httpx.Response(307, headers={"location": "https://other.example.test/"}), "HTTP 307"),
            ("/api/session", httpx.Response(200, json={"authenticated": False, "cookie": SESSION}), "not authenticated"),
            ("/api/session", httpx.Response(200, text='{"authenticated":false,"authenticated":true}'), "invalid JSON"),
        ):
            with self.subTest(expected=expected):
                gateway = GatewayFixture()
                gateway.overrides[path] = response
                self._assert_rejected(gateway, expected)
                self.assertEqual(gateway.requests[-1][1], path)
                self.assertEqual(gateway.submissions, [])

    def test_directory_requires_ownership_platform_profile_and_pinned_limited_runtime(self) -> None:
        for field, value in (
            ("online", False), ("trust_level", "other"), ("capability_ids", []),
            ("version", {"agent_version": HOSTNAME}), ("health_runtimes", []),
            ("features", {"task_protocol": True, "action_interception": "full"}),
            ("runtimes", [{**CODEX, "runtime_version": "9.0.0", "status": "available", "supported_profiles": [PROFILE]}]),
        ):
            with self.subTest(field=field):
                gateway = GatewayFixture()
                gateway.agents[AGENT_IDS[0]][field] = value
                self._assert_rejected(gateway, "Directory")
                self.assertEqual(gateway.submissions, [])
        gateway = GatewayFixture()
        missing = gateway.agents.pop(AGENT_IDS[0])
        self._assert_rejected(gateway, "HTTP 404")
        self.assertEqual(gateway.submissions, [])
        gateway.agents[AGENT_IDS[0]] = missing
        duplicate = deepcopy(missing)
        gateway.agents[AGENT_IDS[1]] = {**duplicate, "id": AGENT_IDS[1]}
        self._assert_rejected(gateway, "exactly one Agent")
        self.assertEqual(gateway.submissions, [])

    def test_incorrect_scope_or_terminal_failure_stops_before_codex(self) -> None:
        gateway = GatewayFixture()
        gateway.native.created["target_selector"]["agent_ids"] = AGENT_IDS[:2]
        self._assert_rejected(gateway, "target scope")
        self.assertEqual(len(gateway.submissions), 1)
        for terminal in ("partially_completed", "failed", "cancelled", "unrecognized"):
            with self.subTest(terminal=terminal):
                gateway = GatewayFixture()
                gateway.native.detail["status"] = terminal
                self._assert_rejected(gateway, "did not complete successfully")
                self.assertEqual(len(gateway.submissions), 1)

    def test_wait_is_bounded_without_retries_or_extra_mutations(self) -> None:
        gateway = GatewayFixture()
        gateway.native.detail["status"] = "running"
        self._assert_rejected(gateway, "Timed out", options=("--task-timeout", "1"), poll_interval=0.05)
        self.assertEqual(len(gateway.submissions), 1)
        self.assertGreater(gateway.requests.count(("GET", gateway.native.endpoint)), 1)
        self.assertEqual([path for method, path in gateway.requests if method == "POST"], ["/api/auth/login", "/api/tasks"])

    def test_rejects_incomplete_or_non_read_only_steps(self) -> None:
        for mutation in ("missing", "failed", "parameter", "wrong_task"):
            with self.subTest(mutation=mutation):
                gateway = GatewayFixture()
                steps = gateway.native.detail["steps"]
                if mutation == "missing":
                    steps.pop()
                elif mutation == "failed":
                    steps[0]["status"] = "failed"
                elif mutation == "parameter":
                    steps[0]["inputs"]["params"] = {"target": ADDRESS}
                else:
                    steps[0]["task_id"] = gateway.codex.id
                self._assert_rejected(gateway, "completed, parameterless L0")

    def test_rejects_bad_evidence_hash_coverage_summary_and_step_binding(self) -> None:
        for mutation in ("hash", "duplicate", "duplicate_id", "summary", "step result", "wrong task"):
            with self.subTest(mutation=mutation):
                gateway = GatewayFixture()
                task = gateway.native
                if mutation == "hash":
                    task.evidence[0]["content_hash"] = "0" * 64
                elif mutation == "duplicate":
                    task.evidence[1] = deepcopy(task.evidence[0])
                elif mutation == "duplicate_id":
                    task.evidence[1]["id"] = task.evidence[0]["id"]
                elif mutation == "summary":
                    task.detail["result"]["summary"]["healthy"] = 3
                elif mutation == "step result":
                    task.detail["steps"][0]["result"]["hostname"] = "other-device.example.test"
                else:
                    task.evidence[0]["task_id"] = gateway.codex.id
                self._assert_rejected(gateway, "Evidence must bind")

    def test_rejects_platform_mismatch_even_with_matching_hash_and_step(self) -> None:
        gateway = GatewayFixture()
        task = gateway.native
        record = task.evidence[0]
        record["summary"]["observations"]["get_host_info"].update(os="linux", system="Linux")
        record["content_hash"] = _digest(record["summary"])
        task.detail["steps"][0]["result"] = deepcopy(record["summary"]["observations"]["get_host_info"])
        task.detail["result"]["devices"][0] = deepcopy(record["summary"])
        self._assert_rejected(gateway, "Invalid get_host_info observation")

    def test_rejects_forged_missing_or_duplicate_l0_action_events(self) -> None:
        for mutation in ("risk", "decision", "scope", "duplicate", "missing", "other_agent", "completion"):
            with self.subTest(mutation=mutation):
                gateway = GatewayFixture()
                task = gateway.native
                request = task.first_event("action.requested")
                completion = task.first_event("agent.action_completed")
                if mutation == "risk":
                    request["payload"]["policy"]["risk_level"] = "L1"
                elif mutation == "decision":
                    request["payload"]["policy"]["decision"] = "deny"
                elif mutation == "scope":
                    request["payload"]["policy"]["allowed_scope"]["profile"] = "shell.v1"
                elif mutation == "duplicate":
                    task.events.insert(task.events.index(request), {**deepcopy(request), "id": "duplicate-request"})
                elif mutation == "missing":
                    task.events.remove(request)
                elif mutation == "other_agent":
                    request["agent_id"] = AGENT_IDS[1]
                else:
                    completion["payload"]["payload"]["action_request_id"] = "action_" + "f" * 32
                for sequence, event in enumerate(task.events, start=1):
                    event["sequence"] = sequence
                self._assert_rejected(gateway, "L0 allow decisions")

    def test_rejects_blocked_actions_even_if_tasks_otherwise_complete(self) -> None:
        for blocked in ("action.blocked", "agent.action_blocked"):
            with self.subTest(blocked=blocked):
                gateway = GatewayFixture()
                gateway.native.first_event("device.started")["event_type"] = blocked
                self._assert_rejected(gateway, "unexpected action")

    def test_rejects_wrong_codex_runtime_or_unbound_session_events(self) -> None:
        for mutation in ("missing", "wrong_mode", "wrong_version", "duplicate", "flat_event", "session_mismatch", "failed_session"):
            with self.subTest(mutation=mutation):
                gateway = GatewayFixture()
                task = gateway.codex
                evidence = task.evidence[0]
                runtime = evidence["summary"]["runtime"]
                finish = task.first_event("agent.runtime_finished")
                if mutation == "missing":
                    evidence["summary"]["runtime"] = None
                elif mutation == "wrong_mode":
                    runtime["mode"] = "managed_full"
                elif mutation == "wrong_version":
                    runtime["adapter_version"] = "9.0.0"
                elif mutation == "duplicate":
                    runtime["session_id"] = task.evidence[1]["summary"]["runtime"]["session_id"]
                elif mutation == "flat_event":
                    finish["payload"] = finish["payload"]["payload"]
                elif mutation == "session_mismatch":
                    finish["payload"]["payload"]["session_id"] = "runtime_" + "f" * 32
                else:
                    finish["payload"]["payload"]["status"] = "failed"
                evidence["content_hash"] = _digest(evidence["summary"])
                task.detail["result"]["devices"][0] = deepcopy(evidence["summary"])
                self._assert_rejected(gateway, "Runtime")
                self.assertEqual(len(gateway.submissions), 2)

    def test_native_evidence_must_not_claim_a_runtime(self) -> None:
        gateway = GatewayFixture()
        record = gateway.native.evidence[0]
        record["summary"]["runtime"] = gateway.codex.evidence[0]["summary"]["runtime"]
        record["content_hash"] = _digest(record["summary"])
        gateway.native.detail["result"]["devices"][0] = deepcopy(record["summary"])
        self._assert_rejected(gateway, "Runtime")


if __name__ == "__main__":
    unittest.main()
