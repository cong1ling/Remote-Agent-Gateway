from __future__ import annotations

from remote_agent.profiles.common.managed_contracts import BUILD_PROFILE, SERVICE_PROFILE


OPERATION_GROUPS = {
    "workspace_changes": "工作区写入与编辑",
    "service_restart": "已登记应用恢复",
    "build_verify": "源码包构建与测试",
    "legacy_shell": "旧版直连 Shell",
    "health_monitor": "定时只读巡检",
}
EXECUTION_STOPPED = "execution_stopped"
STOP_EXPLANATION = "执行已被停机控制阻止或原授权已失效。恢复后请重新核对状态并提交新任务。"


def operation_group(profile: str, action: str, *, planned: bool = False) -> str:
    if profile == "workspace.v1" and action in {"write", "edit"}:
        return "workspace_changes"
    if profile == SERVICE_PROFILE and action == "restart":
        return "service_restart"
    if profile == BUILD_PROFILE and action == "verify":
        return "build_verify"
    if profile == "shell.v1" and action == "exec" and not planned:
        return "legacy_shell"
    return ""
