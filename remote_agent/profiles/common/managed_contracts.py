from __future__ import annotations

import re
from typing import Any


SERVICE_PROFILE = "service_control.v1"
SERVICE_BACKEND = "http-loopback.v1"
BUILD_PROFILE = "build_verify.v1"
BUILD_BACKEND = "python-unittest-zip.v1"
LOCAL_PLAN_PROFILES = frozenset({SERVICE_PROFILE, BUILD_PROFILE})
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,39}")
GENERATION = re.compile(r"[0-9a-f]{32}")
SOURCE_SHA256 = re.compile(r"[0-9a-f]{64}")
SERVICE_STATES = {"ready", "degraded", "stopped", "starting"}
SERVICE_EVENTS = {
    "config_invalid", "process_exited", "dependency_unavailable", "starting",
    "ready", "restart_requested",
}
BUILD_COUNTS = ("tests_run", "failures", "errors", "skipped")


def project_service_status(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("service.invalid_response")
    state, generation, recent = value.get("state"), value.get("generation"), value.get("recent_events")
    if (
        not isinstance(state, str) or state not in SERVICE_STATES
        or not isinstance(generation, str) or GENERATION.fullmatch(generation) is None
        or not isinstance(recent, list) or len(recent) > 12
    ):
        raise ValueError("service.invalid_response")
    projected = []
    for event in recent:
        if not isinstance(event, dict):
            raise ValueError("service.invalid_response")
        code, observed_at = event.get("code"), event.get("observed_at")
        if (
            not isinstance(code, str) or code not in SERVICE_EVENTS
            or type(observed_at) is not int or not 0 < observed_at < 100_000_000_000
        ):
            raise ValueError("service.invalid_response")
        projected.append({"code": code, "observed_at": observed_at})
    return {"state": state, "generation": generation, "recent_events": projected}


def project_managed_result(operation: str, params: dict[str, str], value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("verification_failed: 设备返回了无效的验证结果")
    if operation in {"service.inspect", "service.restart"}:
        service_id = params["service_id"]
        if value.get("service_id") != service_id:
            raise ValueError("verification_failed: 服务结果与批准的目标不一致")
        status = {"service_id": service_id, **project_service_status(value)}
        if operation == "service.inspect":
            return status
        previous = params["expected_generation"]
        if value.get("previous_generation") != previous:
            raise ValueError("verification_failed: 服务恢复前的代际不一致")
        return {
            **status, "previous_generation": previous,
            "verified": status["state"] == "ready" and status["generation"] != previous,
        }
    digest = value.get("source_sha256")
    if not isinstance(digest, str) or SOURCE_SHA256.fullmatch(digest) is None:
        raise ValueError("verification_failed: 源码包 SHA-256 无效")
    if operation == "build.source":
        if "source_sha256" in params and digest != params["source_sha256"]:
            raise ValueError("build.source_mismatch")
        return {"source_sha256": digest}
    compiled = value.get("compiled")
    counts = {field: value.get(field) for field in BUILD_COUNTS}
    if (
        operation != "build.verify" or digest != params["source_sha256"] or type(compiled) is not bool
        or any(type(count) is not int or not 0 <= count <= 100_000 for count in counts.values())
    ):
        raise ValueError("verification_failed: 构建验证结果与批准参数不一致")
    if (
        counts["skipped"] > counts["tests_run"]
        or (not compiled and any(counts.values()))
    ):
        raise ValueError("verification_failed: 测试计数不一致")
    return {
        "source_sha256": digest, "compiled": compiled, **counts,
        "verified": compiled and counts["tests_run"] > counts["skipped"] and not (counts["failures"] or counts["errors"]),
    }
