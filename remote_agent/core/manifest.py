from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import FatalAgentError


@dataclass(frozen=True, slots=True)
class AgentManifest:
    runtime: str
    core_version: int
    profiles: tuple[str, ...]


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parents[1] / "manifest.json"


def load_manifest(path: Path | None = None) -> AgentManifest:
    manifest_path = (path or default_manifest_path()).expanduser().resolve()
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise FatalAgentError(f"无法读取 Agent Manifest: {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise FatalAgentError("Agent Manifest 必须是 JSON 对象")
    runtime = raw.get("runtime")
    core_version = raw.get("core_version")
    profiles = raw.get("profiles")
    if runtime != "python":
        raise FatalAgentError(f"当前运行时不支持 Manifest runtime: {runtime}")
    if core_version != 1:
        raise FatalAgentError(f"当前 Agent 不支持 core_version: {core_version}")
    if not isinstance(profiles, list) or not profiles or not all(
        isinstance(profile, str) and profile for profile in profiles
    ):
        raise FatalAgentError("Agent Manifest 必须声明至少一个 Profile")
    deduplicated = tuple(dict.fromkeys(profiles))
    return AgentManifest(runtime=runtime, core_version=core_version, profiles=deduplicated)

