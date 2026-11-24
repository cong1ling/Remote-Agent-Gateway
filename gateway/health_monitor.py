from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from typing import Any

from .agent_hub import AgentHub
from .database import ConflictError, Database
from .health_check_service import HealthCheckError, HealthCheckService
from .health_signals import monitor_changes


MONITOR_DURATION_SECONDS = 24 * 60 * 60
MAX_MONITORS_PER_OWNER = 8
MAX_CONCURRENT_MONITORS = 4
FAILURE_ALERT_THRESHOLD = 3
LOGGER = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, db: Database, hub: AgentHub, health_checks: HealthCheckService) -> None:
        self.db = db
        self.hub = hub
        self.health_checks = health_checks
        self._runs: dict[str, tuple[str, asyncio.Task[None]]] = {}

    def list(self, user_id: str) -> list[dict[str, Any]]:
        schedules = self.db.list_health_monitors(user_id)
        now = int(time.time())
        for schedule in schedules:
            revision = schedule.pop("execution_revision")
            schedule["status"] = (
                "expired" if schedule["expires_at"] <= now else
                "suspended" if self._blocked(user_id, schedule["agent_id"], revision) else "enabled"
            )
        return schedules

    async def configure(self, user_id: str, agent_id: str, interval_minutes: int | None) -> bool:
        agent = self.db.get_owned_agent(user_id, agent_id)
        if agent is None:
            return False
        revision = self.hub.execution_revision()
        if interval_minutes is not None:
            if "system_observe.v1" not in agent["profiles"] or agent["features"].get("task_protocol") != 1:
                raise ConflictError("设备没有声明健康检查能力")
            if self._blocked(user_id, agent_id, revision):
                raise ConflictError("设备或巡检已暂停；请先恢复并重新授权")
        now = int(time.time())
        if not self.db.set_health_monitor(
            user_id, agent_id, interval_minutes, now=now, expires_at=now + MONITOR_DURATION_SECONDS,
            max_per_owner=MAX_MONITORS_PER_OWNER, execution_revision=revision,
        ):
            return False
        running = self._runs.get(agent_id)
        if running:
            await self.health_checks.cancel(user_id, running[0])
        return True

    def _blocked(self, user_id: str, agent_id: str, revision: int) -> bool:
        return self.hub.execution_blocked(user_id, agent_id, operation="health_monitor", revision=revision)

    async def loop(self) -> None:
        while True:
            try:
                self.start_due(int(time.time()))
            except sqlite3.Error:
                LOGGER.exception("无法读取巡检调度状态")
            await asyncio.sleep(2)

    def start_due(self, now: int) -> None:
        slots = MAX_CONCURRENT_MONITORS - len(self._runs)
        if not slots:
            return
        for schedule in self.db.claim_due_health_monitors(now, slots):
            user_id, agent_id = schedule["user_id"], schedule["agent_id"]
            revision = schedule["execution_revision"]
            if agent_id in self._runs or self._blocked(user_id, agent_id, revision):
                continue
            try:
                task = self.health_checks.create_task(
                    user_id, agent_ids=[agent_id], objective="定时只读健康巡检",
                    monitor_expires_at=schedule["expires_at"], execution_revision=revision,
                )
            except HealthCheckError:
                continue
            task_id = task["id"]
            self.db.record_health_monitor_task(agent_id, schedule["lease_id"], task_id)
            run = self.health_checks.enqueue(user_id, task_id)
            self._runs[agent_id] = (task_id, asyncio.create_task(self._finish(schedule, task_id, run)))

    async def _finish(self, schedule: dict[str, Any], task_id: str, run: asyncio.Task[None]) -> None:
        user_id, agent_id = schedule["user_id"], schedule["agent_id"]
        try:
            await run
            if self._blocked(user_id, agent_id, schedule["execution_revision"]):
                return
            task = self.db.get_task(user_id, task_id)
            if task is None or task["status"] not in {"completed", "failed"}:
                return
            if any(step["status"] == "cancelled" for step in self.db.list_task_steps(user_id, task_id)):
                return
            evidence = self.db.list_task_evidence(user_id, task_id)
            sample = evidence[0]["summary"]["observations"] if (
                task["status"] == "completed" and len(evidence) == 1 and evidence[0]["kind"] == "health_monitor"
            ) else None
            failures = 0 if sample is not None else min(schedule["consecutive_failures"] + 1, FAILURE_ALERT_THRESHOLD + 1)
            if not self.db.record_health_monitor_sample(agent_id, schedule["lease_id"], sample, failures):
                return
            if failures == FAILURE_ALERT_THRESHOLD:
                self._alert(task_id, agent_id, "monitor_failures", "连续三次巡检任务未通过验证",
                            {"consecutive_failures": failures})
            if sample is not None:
                for action, category, trigger, basis in monitor_changes(schedule["sample"], sample):
                    step = next(
                        step for step in self.db.list_task_steps(user_id, task_id)
                        if step["inputs"]["action"] == action
                    )
                    self._alert(task_id, agent_id, category, trigger, basis, action=step["action_type"], step_id=step["id"])
        except sqlite3.Error:
            LOGGER.exception("无法保存巡检证据状态")
        finally:
            self._runs.pop(agent_id, None)

    def _alert(
        self, task_id: str, agent_id: str, category: str, trigger: str, basis: dict[str, Any],
        *, action: str | None = None, step_id: str | None = None,
    ) -> None:
        self.db.create_task_alert(
            alert_id=f"alert_{uuid.uuid4().hex}", task_id=task_id, agent_id=agent_id, severity="warning",
            category=category, trigger=trigger, recommendation="核对该设备近期变更，必要时人工复查",
            basis=basis, action=action, step_id=step_id,
        )

    async def shutdown(self) -> None:
        cleanup = [run for _, run in self._runs.values()]
        for run in cleanup:
            run.cancel()
        if cleanup:
            await asyncio.gather(*cleanup, return_exceptions=True)
        self._runs.clear()
