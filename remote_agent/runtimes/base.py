from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RuntimeTask:
    task_id: str
    objective: str
    prompt: str
    actions: tuple[str, ...] = ()


@dataclass(slots=True)
class RuntimeSession:
    session_id: str
    provider_id: str
    provider_version: str
    adapter_version: str
    mode: str
    state: str = "starting"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_type: str
    payload: dict[str, Any]
    action_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActionDecision:
    action_request_id: str
    decision: str
    reason: str = ""
    policy_context: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


class LocalAgentRuntime(Protocol):
    def declaration(self) -> dict[str, Any]: ...

    async def start(self, task: RuntimeTask) -> RuntimeSession: ...

    async def events(self, session: RuntimeSession) -> AsyncIterator[RuntimeEvent]: ...

    async def decide_action(
        self,
        session: RuntimeSession,
        decision: ActionDecision,
    ) -> None: ...

    async def cancel(self, session: RuntimeSession) -> None: ...

    async def close(self, session: RuntimeSession) -> None: ...
