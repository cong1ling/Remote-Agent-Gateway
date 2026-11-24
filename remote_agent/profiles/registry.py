from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from remote_agent.core.errors import FatalAgentError

from .base import ProfileContext, ProfileHandler
from .common.managed_contracts import BUILD_PROFILE, LOCAL_PLAN_PROFILES, SERVICE_PROFILE

ProfileFactory = Callable[[ProfileContext], ProfileHandler]


def _build_shell(context: ProfileContext) -> ProfileHandler:
    from .shell_v1 import ShellV1Profile

    return ShellV1Profile(context)


def _build_workspace(context: ProfileContext) -> ProfileHandler:
    from .workspace_v1 import WorkspaceV1Profile

    return WorkspaceV1Profile(context)


def _build_system_observe(context: ProfileContext) -> ProfileHandler:
    from .system_observe_v1 import SystemObserveV1Profile

    return SystemObserveV1Profile(context)


def _build_service_control(context: ProfileContext) -> ProfileHandler:
    from .service_control_v1 import ServiceControlV1Profile

    return ServiceControlV1Profile(context)


def _build_build_verify(context: ProfileContext) -> ProfileHandler:
    from .build_verify_v1 import BuildVerifyV1Profile
    from .build_verify_v1.archive import inspect_zip, read_snapshot

    if context.build_archive is None:
        raise ValueError("build_verify.v1 需要设备本地 --build-archive")
    try:
        data, _digest = read_snapshot(context.build_archive, context.workdir)
        inspect_zip(data)
    except ValueError:
        raise ValueError("--build-archive 必须是工作区外的有效本地源码包") from None
    return BuildVerifyV1Profile(context)


PROFILE_FACTORIES: dict[str, ProfileFactory] = {
    "shell.v1": _build_shell,
    "workspace.v1": _build_workspace,
    "system_observe.v1": _build_system_observe,
    SERVICE_PROFILE: _build_service_control,
    BUILD_PROFILE: _build_build_verify,
}


def build_profile_handlers(
    profile_ids: tuple[str, ...] | list[str],
    workdir: Path,
    *,
    service_config: Path | None = None,
    build_archive: Path | None = None,
) -> dict[str, ProfileHandler]:
    if "shell.v1" in profile_ids and LOCAL_PLAN_PROFILES.intersection(profile_ids):
        raise FatalAgentError("本地服务或构建 Profile 不得与旧版直连 Shell 混装")
    if service_config is not None and SERVICE_PROFILE not in profile_ids:
        raise FatalAgentError("--service-config 需要装配 service_control.v1")
    if build_archive is not None and BUILD_PROFILE not in profile_ids:
        raise FatalAgentError("--build-archive 需要装配 build_verify.v1")
    context = ProfileContext(workdir=workdir.resolve(), service_config=service_config, build_archive=build_archive)
    handlers: dict[str, ProfileHandler] = {}
    for profile_id in profile_ids:
        factory = PROFILE_FACTORIES.get(profile_id)
        if factory is None:
            raise FatalAgentError(f"当前 Python Agent 未实现 Profile: {profile_id}")
        try:
            handler = factory(context)
        except ValueError as exc:
            raise FatalAgentError(str(exc)) from exc
        if handler.profile_id != profile_id:
            raise FatalAgentError(
                f"Profile 工厂返回了错误的 ID: {profile_id} -> {handler.profile_id}"
            )
        handlers[handler.profile_id] = handler
    return handlers
