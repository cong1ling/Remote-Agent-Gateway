from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from remote_agent.runtimes.claude import CLAUDE_ADAPTER_VERSION, CLAUDE_VERSION
from remote_agent.runtimes.codex import CODEX_ADAPTER_VERSION, CODEX_VERSION
from remote_agent.runtimes.health import HEALTH_PROFILE


@dataclass(frozen=True, slots=True)
class HealthRuntimeContract:
    provider_id: str
    versions: frozenset[tuple[str, str]]
    mode: str = "managed_limited"
    action_interception: str = "limited"

    def matches(self, value: dict[str, Any]) -> bool:
        version, adapter = value.get("runtime_version"), value.get("adapter_version")
        return (
            value.get("provider_id") == self.provider_id
            and value.get("mode") == self.mode
            and isinstance(version, str)
            and isinstance(adapter, str)
            and (version, adapter) in self.versions
        )

    def accepts(self, value: dict[str, Any]) -> bool:
        return (
            self.matches(value)
            and value.get("status") == "available"
            and HEALTH_PROFILE in value.get("supported_profiles", ())
        )


HEALTH_RUNTIME_CATALOG = {
    "codex": HealthRuntimeContract("codex", frozenset({(CODEX_VERSION, CODEX_ADAPTER_VERSION)})),
    "claude": HealthRuntimeContract("claude", frozenset({(CLAUDE_VERSION, CLAUDE_ADAPTER_VERSION)})),
}
