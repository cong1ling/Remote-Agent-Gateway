from __future__ import annotations

from pathlib import Path

from remote_agent.core.errors import FatalAgentError

from .base import ProfileHandler


def build_profile_handlers(
    profile_ids: tuple[str, ...] | list[str],
    workdir: Path,
) -> dict[str, ProfileHandler]:
    handlers: dict[str, ProfileHandler] = {}
    for profile_id in profile_ids:
        if profile_id == "shell.v1":
            from .shell_v1 import ShellV1Profile

            handler: ProfileHandler = ShellV1Profile(workdir)
        else:
            raise FatalAgentError(f"当前 Python Agent 未实现 Profile: {profile_id}")
        handlers[handler.profile_id] = handler
    return handlers


__all__ = ["ProfileHandler", "build_profile_handlers"]
