"""Run with python -m tests.phase1_site_acceptance --help from the repository root."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import ipaddress
import json
import logging
import re
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from remote_agent.runtimes.claude import CLAUDE_ADAPTER_VERSION, CLAUDE_VERSION

JsonObject = dict[str, Any]
PLATFORMS = {"windows": "Windows", "macos": "Darwin", "linux": "Linux"}
PROFILE = "system_observe.v1"
ACTIONS = ("get_host_info", "get_resources", "get_processes", "get_network")
CODEX = {
    "provider_id": "codex", "runtime_version": "0.169.0",
    "adapter_version": "0.1.0", "mode": "managed_limited",
}
RUNTIME_EVIDENCE = {
    "codex": CODEX,
    "claude": {
        "provider_id": "claude", "runtime_version": CLAUDE_VERSION,
        "adapter_version": CLAUDE_ADAPTER_VERSION, "mode": "managed_limited",
    },
}
SESSION_COOKIE = "remote_gateway_session"
AGENT_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
HTTP_TIMEOUT = 10
POLL_INTERVAL = 0.5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MANUAL_CHECKS = (
    "Confirm the Agent IDs belong to three distinct physical Windows, macOS and Linux devices.",
    "Confirm the authorized Codex account and real model provider were used, not a fixture.",
    "Confirm the platform install/service templates are actually installed and running.",
)
EVENT_TYPES = frozenset({
    "task.created", "task.planned", "task.approved", "task.queued", "task.started",
    "task.completed", "device.started", "device.completed", "agent.acknowledged",
    "agent.step_started", "agent.action_started", "agent.action_completed", "agent.result",
    "action.requested", "agent.runtime_started", "agent.runtime_finished",
})


class AcceptanceError(Exception):
    pass


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AcceptanceError("Invalid arguments; use --help for usage.") from None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _object(value: object, message: str) -> JsonObject:
    _require(isinstance(value, dict), message)
    return value


def _records(value: object, message: str) -> list[JsonObject]:
    _require(isinstance(value, list) and all(isinstance(item, dict) for item in value), message)
    return value


def _strings(value: object, message: str) -> list[str]:
    _require(isinstance(value, list) and all(isinstance(item, str) for item in value), message)
    return value


def _identifier(value: object, prefix: str, message: str) -> str:
    _require(isinstance(value, str) and re.fullmatch(rf"{prefix}_[0-9a-f]{{32}}", value) is not None, message)
    return value


def _gateway_origin(value: str) -> str:
    message = "Use an HTTPS Gateway origin, or HTTP on an explicit local loopback address."
    _require(value.isascii() and all(32 < ord(char) < 127 for char in value), message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AcceptanceError(message) from None
    _require(
        parsed.scheme in ("https", "http") and parsed.path in ("", "/")
        and not any(char in value for char in "?#")
        and re.fullmatch(r"(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]+)?", parsed.netloc) is not None
        and port != 0,
        message,
    )
    if parsed.scheme == "http":
        if parsed.hostname == "localhost":
            return "http://127.0.0.1" + (f":{port}" if port is not None else "")
        try:
            _require(ipaddress.ip_address(parsed.hostname).is_loopback, message)
        except ValueError:
            raise AcceptanceError(message) from None
    return f"{parsed.scheme}://{parsed.netloc}"


def _unique_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant")


async def _request(
    client: httpx.AsyncClient, method: str, path: str, stage: str, *, status: int = 200,
    body: JsonObject | None = None,
) -> JsonObject:
    try:
        async with asyncio.timeout(HTTP_TIMEOUT):
            async with client.stream(method, path, json=body) as response:
                _require(response.status_code == status, f"{stage}: unexpected HTTP {response.status_code}.")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    data.extend(chunk)
                    _require(len(data) <= MAX_RESPONSE_BYTES, f"{stage}: response too large.")
        decoded = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError):
        raise AcceptanceError(f"{stage}: connection, TLS, or request timeout failure.") from None
    except (ValueError, RecursionError):
        raise AcceptanceError(f"{stage}: invalid JSON response.") from None
    return _object(decoded, f"{stage}: expected a JSON object.")


def _secret(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt, stream=sys.stderr)


async def _authenticate(client: httpx.AsyncClient, use_session: bool) -> None:
    if use_session:
        cookie = _secret("Existing Gateway session cookie value: ")
        _require(re.fullmatch(r"session_[a-zA-Z0-9_-]{43}", cookie) is not None, "Invalid session cookie format.")
        client.cookies.set(SESSION_COOKIE, cookie, path="/")
    else:
        print("Existing Gateway account email: ", end="", file=sys.stderr, flush=True)
        email = input().strip()
        password = _secret("Gateway account password: ")
        _require(bool(email and password), "Gateway login requires an email and password.")
        await _request(client, "POST", "/api/auth/login", "Login", body={"email": email, "password": password})
    session = await _request(client, "GET", "/api/session", "Session")
    _require(session.get("authenticated") is True, "Gateway session is not authenticated.")


def _device(agent_id: str, agent: JsonObject, providers: tuple[str, ...] = ("codex",)) -> JsonObject:
    message = "Directory must show each selected Agent online, owner-enrolled, and ready for the requested L0 runtimes."
    device = _object(agent.get("device"), message)
    features = _object(agent.get("features"), message)
    version = _object(agent.get("version"), message)
    platform = device.get("platform")
    _require(
        agent.get("id") == agent_id and agent.get("online") is True and agent.get("trust_level") == "owner_enrolled"
        and device.get("device_type") == "computer" and isinstance(platform, str) and platform in PLATFORMS
        and isinstance(device.get("platform_version"), str) and bool(device["platform_version"].strip())
        and type(features.get("task_protocol")) is int and features["task_protocol"] == 1
        and features.get("event_stream") is True and features.get("runtime_adapter") is True
        and features.get("action_interception") == "limited",
        message,
    )
    capabilities = _strings(agent.get("capability_ids"), message)
    _require({PROFILE, *(f"{PROFILE}/{action}" for action in ACTIONS)}.issubset(capabilities), message)
    profiles = [
        declaration for declaration in _records(agent.get("profile_declarations"), message)
        if declaration.get("id") == PROFILE
    ]
    _require(len(profiles) == 1, message)
    profile = _object(profiles[0].get("runtime"), message)
    actions = _strings(profile.get("actions"), message)
    _require(
        profile.get("scope") == "host" and profile.get("mode") == "read_only"
        and profile.get("platform") == platform and sorted(actions) == sorted(ACTIONS),
        message,
    )
    declarations = _records(agent.get("runtimes"), message)
    ready = _strings(agent.get("health_runtimes"), message)
    for provider in providers:
        runtimes = [runtime for runtime in declarations if runtime.get("provider_id") == provider]
        _require(
            len(runtimes) == 1 and provider in ready
            and all(runtimes[0].get(key) == expected for key, expected in RUNTIME_EVIDENCE[provider].items())
            and runtimes[0].get("status") == "available"
            and PROFILE in _strings(runtimes[0].get("supported_profiles"), message),
            message,
        )
    agent_version = version.get("agent_version")
    _require(
        isinstance(agent_version, str) and re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,10}){2}", agent_version) is not None
        and type(version.get("core_version")) is int and version["core_version"] > 0
        and type(version.get("protocol_version")) is int and version["protocol_version"] == 2,
        "Directory lacks a compatible Agent version.",
    )
    return {
        "agent_id": agent_id, "platform": platform, "agent_version": agent_version,
        "core_version": version["core_version"], "protocol_version": version["protocol_version"],
    }


def _check_task_scope(task: JsonObject, task_id: str, agent_ids: set[str], runtime: str | None) -> None:
    message = "Task ID, target scope, runtime, or read-only policy does not match the request."
    selector = _object(task.get("target_selector"), message)
    ids = _strings(selector.get("agent_ids"), message)
    _require(
        task.get("id") == task_id and set(selector) == {"agent_ids"} and len(ids) == 3 and set(ids) == agent_ids
        and "requested_runtime" in task and task["requested_runtime"] == runtime and task.get("plan") is None
        and task.get("risk_policy") == {"max_level": "L0", "mode": "read_only"}
        and _object(task.get("approval_policy"), message).get("required") is False,
        message,
    )
    budget = _object(task.get("resource_budget"), message)
    _require(
        type(budget.get("max_devices")) is int and budget["max_devices"] == len(agent_ids)
        and type(budget.get("max_actions_per_device")) is int and budget["max_actions_per_device"] == len(ACTIONS)
        and type(budget.get("timeout_seconds")) is int and 1 <= budget["timeout_seconds"] <= 120,
        message,
    )


def _steps(task: JsonObject, task_id: str, agent_ids: set[str]) -> dict[str, JsonObject]:
    message = "Expected exactly four completed, parameterless L0 observation steps per Agent."
    steps: dict[str, JsonObject] = {}
    remaining = {(agent_id, action) for agent_id in agent_ids for action in ACTIONS}
    for step in _records(task.get("steps"), message):
        step_id = _identifier(step.get("id"), "step", message)
        inputs = _object(step.get("inputs"), message)
        agent_id, action = step.get("target_agent_id"), inputs.get("action")
        _require(
            isinstance(agent_id, str) and isinstance(action, str) and (agent_id, action) in remaining
            and step_id not in steps and step.get("task_id") == task_id
            and step.get("status") == "completed" and step.get("action_type") == f"{PROFILE}/{action}"
            and inputs == {"profile": PROFILE, "action": action, "params": {}, "mcp_tool": f"remote_{action}"}
            and _object(step.get("expected_result"), message).get("read_only") is True
            and isinstance(step.get("result"), dict),
            message,
        )
        remaining.remove((agent_id, action))
        steps[step_id] = step
    _require(not remaining, message)
    return steps


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, RecursionError):
        raise AcceptanceError("Invalid canonical JSON in task evidence.") from None


def _check_observation(action: str, value: object, platform: str) -> None:
    message = f"Invalid {action} observation for the declared platform."
    observation = _object(value, message)
    timestamp = observation.get("observed_at")
    unix = observation.get("observed_at_unix")
    _require(
        isinstance(timestamp, str)
        and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", timestamp) is not None
        and type(unix) in (int, float) and 0 < unix < 253402300800,
        message,
    )
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        raise AcceptanceError(message) from None
    if action == "get_host_info":
        _require(
            observation.get("os") == platform and observation.get("system") == PLATFORMS[platform]
            and all(isinstance(observation.get(key), str) for key in ("release", "version", "architecture", "hostname", "python"))
            and type(observation.get("pid")) is int and observation["pid"] >= 0,
            message,
        )
    elif action == "get_resources":
        cpu = _object(observation.get("cpu"), message)
        logical_count = cpu.get("logical_count")
        memory = observation.get("memory")
        disks = _records(observation.get("disks"), message)
        _require(
            "logical_count" in cpu and (logical_count is None or type(logical_count) is int and logical_count > 0)
            and "memory" in observation
            and (memory is None or isinstance(memory, dict) and type(memory.get("total_bytes")) is int and memory["total_bytes"] > 0)
            and 1 <= len(disks) <= 16,
            message,
        )
        for disk in disks:
            _require(all(type(disk.get(key)) is int and disk[key] >= 0 for key in ("total_bytes", "used_bytes", "free_bytes")), message)
    elif action == "get_processes":
        _require(type(observation.get("available")) is bool and type(observation.get("truncated")) is bool, message)
        for process in _records(observation.get("processes"), message):
            _require(type(process.get("pid")) is int and isinstance(process.get("name"), str), message)
    else:
        _require(isinstance(observation.get("hostname"), str), message)
        _strings(observation.get("addresses"), message)
        for interface in _records(observation.get("interfaces"), message):
            _require(type(interface.get("index")) is int and isinstance(interface.get("name"), str), message)


def _by_agent(value: object, agent_ids: set[str], message: str) -> dict[str, JsonObject]:
    records = {}
    for record in _records(value, message):
        agent_id = record.get("agent_id")
        _require(isinstance(agent_id, str) and agent_id in agent_ids and agent_id not in records, message)
        records[agent_id] = record
    _require(records.keys() == agent_ids, message)
    return records


def _evidence(
    value: object, task: JsonObject, steps: dict[str, JsonObject], devices: dict[str, JsonObject],
    task_id: str, runtime: str | None,
) -> tuple[list[JsonObject], JsonObject]:
    message = "Evidence must bind the completed steps, task summary, and SHA-256 to the three selected Agents."
    agent_ids = set(devices)
    result = _object(task.get("result"), message)
    _require(result.get("task_id") == task_id, message)
    device_results = _by_agent(result.get("devices"), agent_ids, message)
    evidence = _by_agent(value, agent_ids, message)
    step_results = {(step["target_agent_id"], step["inputs"]["action"]): step["result"] for step in steps.values()}
    counts = {"healthy": 0, "warning": 0, "critical": 0, "failed": 0}
    sessions: set[str] = set()
    evidence_ids: set[str] = set()
    reports = []
    for agent_id, device in devices.items():
        record = evidence[agent_id]
        evidence_id = _identifier(record.get("id"), "evidence", message)
        summary = _object(record.get("summary"), message)
        serialized = _canonical_json(summary)
        digest = hashlib.sha256(serialized).hexdigest()
        status = summary.get("status")
        _require(
            evidence_id not in evidence_ids and record.get("task_id") == task_id
            and record.get("kind") == "health_check" and record.get("step_id") is None
            and record.get("content_hash") == digest and serialized == _canonical_json(device_results[agent_id])
            and summary.get("agent_id") == agent_id and status in ("healthy", "warning"),
            message,
        )
        evidence_ids.add(evidence_id)
        observations = _object(summary.get("observations"), message)
        _require(observations.keys() == set(ACTIONS), message)
        for action in ACTIONS:
            _check_observation(action, observations[action], device["platform"])
            _require(_canonical_json(observations[action]) == _canonical_json(step_results[agent_id, action]), message)
        alerts = _records(summary.get("alerts"), message)
        _require(bool(alerts) == (status == "warning"), message)
        counts[status] += 1
        public_runtime = None
        runtime_message = "Missing, duplicate, or inconsistent Runtime evidence."
        _require("runtime" in summary, runtime_message)
        if runtime is not None:
            runtime_evidence = _object(summary["runtime"], runtime_message)
            session_id = _identifier(runtime_evidence.get("session_id"), "runtime", runtime_message)
            public_runtime = {**RUNTIME_EVIDENCE[runtime], "session_id": session_id}
            _require(runtime_evidence == public_runtime and session_id not in sessions, runtime_message)
            sessions.add(session_id)
        else:
            _require(summary["runtime"] is None, runtime_message)
        reports.append({**device, "evidence_hash": digest, "runtime": public_runtime})
    totals = _object(result.get("summary"), message)
    _require(totals == counts and all(type(count) is int for count in totals.values()), message)
    return reports, counts


def _agent_payload(event: JsonObject, message: str) -> JsonObject:
    wrapper = _object(event.get("payload"), message)
    event_id = _identifier(event.get("id"), "event", message)
    _require(
        wrapper.get("remote_event_id") == event_id
        and type(wrapper.get("remote_sequence")) is int and wrapper["remote_sequence"] > 0,
        message,
    )
    return _object(wrapper.get("payload"), message)


def _events(
    value: object, task_id: str, steps: dict[str, JsonObject], reports: list[JsonObject], counts: JsonObject,
) -> None:
    message = "Task event log is incomplete, out of scope, or contains an unexpected action."
    by_type = defaultdict(list)
    events = _records(value, message)
    agent_ids = {report["agent_id"] for report in reports}
    event_ids: set[str] = set()
    for sequence, event in enumerate(events, start=1):
        kind, agent_id = event.get("event_type"), event.get("agent_id")
        event_id = event.get("id")
        _require(
            event.get("task_id") == task_id and type(event.get("sequence")) is int and event["sequence"] == sequence
            and isinstance(event_id, str) and bool(event_id) and event_id not in event_ids
            and isinstance(kind, str) and kind in EVENT_TYPES
            and (agent_id is None or isinstance(agent_id, str) and agent_id in agent_ids),
            message,
        )
        event_ids.add(event_id)
        by_type[kind].append(event)
    _require(
        len(by_type["task.created"]) == len(by_type["task.completed"]) == 1
        and events[0].get("event_type") == "task.created" and events[0].get("payload") == {"kind": "health_check"}
        and events[-1].get("event_type") == "task.completed"
        and _canonical_json(events[-1].get("payload")) == _canonical_json({"status": "completed", "summary": counts}),
        message,
    )
    message = "Expected four matching L0 allow decisions and action completions per Agent."
    authorizations = {}
    request_ids = set()
    for event in by_type["action.requested"]:
        step_id = _identifier(event.get("step_id"), "step", message)
        _require(step_id in steps and step_id not in authorizations, message)
        payload = _object(event.get("payload"), message)
        request_id = _identifier(payload.get("action_request_id"), "action", message)
        step = steps[step_id]
        action = step["inputs"]["action"]
        policy = _object(payload.get("policy"), message)
        _require(
            event.get("agent_id") == step["target_agent_id"] and request_id not in request_ids
            and payload.get("profile") == PROFILE and payload.get("action") == action
            and policy.get("decision") == "allow" and policy.get("risk_level") == "L0"
            and policy.get("matched_rules") == ["health_check.read_only"]
            and policy.get("allowed_scope") == {"profile": PROFILE, "action": action},
            message,
        )
        request_ids.add(request_id)
        authorizations[step_id] = (event["sequence"], request_id)
    _require(authorizations.keys() == steps.keys(), message)
    completions = {}
    for event in by_type["agent.action_completed"]:
        step_id = _identifier(event.get("step_id"), "step", message)
        _require(step_id in steps and step_id not in completions, message)
        sequence, request_id = authorizations[step_id]
        step = steps[step_id]
        _require(
            event.get("agent_id") == step["target_agent_id"] and event["sequence"] > sequence
            and _agent_payload(event, message) == {
                "step_id": step_id, "action_request_id": request_id,
                "profile": PROFILE, "action": step["inputs"]["action"],
            },
            message,
        )
        completions[step_id] = event["sequence"]
    _require(completions.keys() == steps.keys(), message)
    message = "Expected one successful Agent result after its completed L0 actions."
    results = _by_agent(by_type["agent.result"], agent_ids, message)
    for agent_id, result in results.items():
        _require(
            result.get("step_id") is None and _object(result.get("payload"), message).get("ok") is True
            and all(
                completions[step_id] < result["sequence"]
                for step_id, step in steps.items() if step["target_agent_id"] == agent_id
            ),
            message,
        )
    starts, finishes = by_type["agent.runtime_started"], by_type["agent.runtime_finished"]
    message = "Runtime start/finish events do not bind the runtime session to its L0 actions."
    if reports[0]["runtime"] is None:
        _require(not starts and not finishes, message)
        return
    start_by_agent = _by_agent(starts, agent_ids, message)
    finish_by_agent = _by_agent(finishes, agent_ids, message)
    for report in reports:
        agent_id, runtime = report["agent_id"], report["runtime"]
        start, finish = start_by_agent[agent_id], finish_by_agent[agent_id]
        _require(
            start.get("step_id") is None and finish.get("step_id") is None
            and _agent_payload(start, message) == runtime
            and _agent_payload(finish, message) == {"session_id": runtime["session_id"], "status": "completed"}
            and finish["sequence"] < results[agent_id]["sequence"]
            and all(
                start["sequence"] < authorizations[step_id][0] < completions[step_id] < finish["sequence"]
                for step_id, step in steps.items() if step["target_agent_id"] == agent_id
            ),
            message,
        )


async def _health_task(
    client: httpx.AsyncClient, devices: dict[str, JsonObject], runtime: str | None, timeout: int,
) -> JsonObject:
    task_id = None
    mode = runtime or "native"
    try:
        async with asyncio.timeout(timeout):
            created = await _request(client, "POST", "/api/tasks", "Create health task", status=202, body={
                "kind": "health_check", "agent_ids": list(devices), "requested_runtime": runtime,
            })
            task = _object(created.get("task"), "Missing health task.")
            task_id = _identifier(task.get("id"), "task", "Invalid health Task ID.")
            endpoint = f"/api/tasks/{task_id}"
            _check_task_scope(task, task_id, set(devices), runtime)
            while True:
                detail = await _request(client, "GET", endpoint, "Health task detail")
                task = _object(detail.get("task"), "Missing health task detail.")
                _check_task_scope(task, task_id, set(devices), runtime)
                status = task.get("status")
                if status == "completed":
                    break
                _require(status in ("queued", "running"), "Health task did not complete successfully.")
                await asyncio.sleep(POLL_INTERVAL)
            steps = _steps(task, task_id, set(devices))
            evidence = await _request(client, "GET", f"{endpoint}/evidence", "Health evidence")
            reports, counts = _evidence(evidence.get("evidence"), task, steps, devices, task_id, runtime)
            events = await _request(client, "GET", f"{endpoint}/events", "Health events")
            _events(events.get("events"), task_id, steps, reports, counts)
            return {"task_id": task_id, "mode": mode, "devices": reports}
    except (AcceptanceError, TimeoutError) as exc:
        location = f"{mode} task {task_id}" if task_id else f"{mode} task"
        reason = str(exc) if isinstance(exc, AcceptanceError) else "Timed out; check the task in Gateway before rerunning."
        raise AcceptanceError(f"{location}: {reason}") from None


async def _run(origin: str, agent_ids: list[str], use_session: bool, timeout: int, *, verify_claude: bool = False) -> JsonObject:
    providers = ("codex", "claude") if verify_claude else ("codex",)
    async with httpx.AsyncClient(
        base_url=origin, timeout=HTTP_TIMEOUT, verify=True, trust_env=False, follow_redirects=False,
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
    ) as client:
        await _authenticate(client, use_session)
        devices = {}
        for agent_id in agent_ids:
            directory = await _request(client, "GET", f"/api/agents/{agent_id}", "Device directory")
            device = _device(agent_id, _object(directory.get("agent"), "Missing directory Agent."), providers)
            platform = device["platform"]
            _require(platform not in devices, "Select exactly one Agent from each of Windows, macOS, and Linux.")
            devices[platform] = device
        _require(devices.keys() == PLATFORMS.keys(), "Select exactly one Agent from each of Windows, macOS, and Linux.")
        selected = {devices[platform]["agent_id"]: devices[platform] for platform in PLATFORMS}
        tasks = [await _health_task(client, selected, runtime, timeout) for runtime in (None, *providers)]
        _require(len({task["task_id"] for task in tasks}) == len(tasks), "Each run must have a different Task ID.")
        checks = list(MANUAL_CHECKS)
        if verify_claude:
            checks.append("Confirm the dedicated Claude API key and real Anthropic model were used, not a fixture.")
        return {"tasks": tasks, "manual_checks_required": checks}


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        description="Verify fixed read-only health tasks for three authorized Agents through one Gateway.",
        epilog="A pass is not proof of physical devices, real model use, or installed templates. Redirect stdout to save JSON.",
        allow_abbrev=False,
    )
    parser.add_argument("--gateway", required=True, help="HTTPS origin; HTTP only for loopback (localhost uses IPv4)")
    parser.add_argument("--agent-ids", nargs=3, metavar="ID", required=True, help="Three distinct enrolled Agent IDs")
    parser.add_argument("--use-session", action="store_true", help="Prompt for an existing session cookie instead of login")
    parser.add_argument("--verify-claude", action="store_true", help="Also verify L0 Claude Code on the same three Agents")
    parser.add_argument("--task-timeout", type=int, default=150, metavar="SECONDS", help="Per task, 1-300 seconds (default: 150)")
    try:
        args = parser.parse_args(argv)
        origin = _gateway_origin(args.gateway)
        _require(
            all(AGENT_ID.fullmatch(agent_id) for agent_id in args.agent_ids) and len(set(args.agent_ids)) == 3,
            "Provide three distinct enrolled Agent IDs in UUID format.",
        )
        _require(1 <= args.task_timeout <= 300, "Task timeout must be between 1 and 300 seconds.")
        previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            report = asyncio.run(_run(origin, args.agent_ids, args.use_session, args.task_timeout, verify_claude=args.verify_claude))
        finally:
            logging.disable(previous_logging)
    except AcceptanceError as exc:
        print(f"Acceptance failed: {exc}", file=sys.stderr)
        return 1
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        print("Acceptance failed: unable to initialize or close the Gateway connection.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Acceptance stopped: a terminal with hidden credential input is required.", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
