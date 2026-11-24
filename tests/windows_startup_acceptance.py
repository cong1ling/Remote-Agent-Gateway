from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import ExitStack
from pathlib import Path

import httpx

from tests.test_e2e_gateway_agent import GatewayAgentEndToEndTests
from tests.wsl_mixed_acceptance import wait_for


_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_SCHEDULER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$name = $env:RAG_ACCEPTANCE_TASK
if ($name -cnotmatch '^RAG-Acceptance-[a-f0-9]{32}$') { throw 'Invalid fixture task name' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run as a non-elevated, logged-on test user'
}
switch ($env:RAG_ACCEPTANCE_ACTION) {
    'sid' { $identity.User.Value }
    'register' {
        if (Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue) {
            throw 'Temporary task already exists'
        }
        Register-ScheduledTask -TaskName $name -TaskPath '\' -Xml ([IO.File]::ReadAllText($env:RAG_ACCEPTANCE_XML)) | Out-Null
    }
    'start' { Start-ScheduledTask -TaskName $name -TaskPath '\' }
    'stop' { Stop-ScheduledTask -TaskName $name -TaskPath '\' }
    'cleanup' {
        if ($task = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue) {
            try {
                if ($task.State -eq 'Running') { Stop-ScheduledTask -InputObject $task }
            } finally { Unregister-ScheduledTask -InputObject $task -Confirm:$false }
        }
        if (Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue) {
            throw 'Temporary task was not removed'
        }
    }
    default { throw 'Invalid fixture operation' }
}
"""


def _scheduler(name: str, action: str, xml_file: Path) -> str:
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _SCHEDULER],
        env={
            **os.environ, "RAG_ACCEPTANCE_TASK": name, "RAG_ACCEPTANCE_ACTION": action,
            "RAG_ACCEPTANCE_XML": str(xml_file),
        },
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"Task Scheduler {action} failed for disposable task {name}: {result.stderr.strip()}")
    return result.stdout.strip()


def _render(xml: bytes, sid: str, executable: Path, launcher: Path, config: Path, workdir: Path, output: Path) -> None:
    ET.register_namespace("", _TASK_NS)
    task = ET.fromstring(xml)
    namespace = {"task": _TASK_NS}
    trigger = task.find("task:Triggers/task:LogonTrigger", namespace)
    if trigger is None or not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
        raise ValueError("Expected a Windows logon trigger and current user SID")
    excluded = ET.Element(f"{{{_TASK_NS}}}Enabled")
    excluded.text = "false"
    trigger.insert(1, excluded)
    values = {
        "task:Triggers/task:LogonTrigger/task:UserId": sid,
        "task:Principals/task:Principal/task:UserId": sid,
        "task:Actions/task:Exec/task:Command": str(executable),
        "task:Actions/task:Exec/task:Arguments": subprocess.list2cmdline([
            str(launcher), "--config", str(config), "--workdir", str(workdir),
        ]),
        "task:Actions/task:Exec/task:WorkingDirectory": str(workdir),
    }
    for path, value in values.items():
        node = task.find(path, namespace)
        if node is None:
            raise ValueError("Generated task template is incomplete")
        node.text = value
    ET.ElementTree(task).write(output, encoding="utf-8", xml_declaration=False)


def verify() -> None:
    suite = GatewayAgentEndToEndTests
    with ExitStack() as cleanup:
        root = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="rag-windows-startup-")))
        workdir = root / "isolated workspace"
        workdir.mkdir()
        executable = Path(sys.executable)
        task_name = f"RAG-Acceptance-{uuid.uuid4().hex}"
        xml_file = root / "startup.xml"
        sid = _scheduler(task_name, "sid", xml_file)
        port = suite._free_port()
        origin = f"http://127.0.0.1:{port}"
        env = {
            **os.environ, "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(port), "GATEWAY_PUBLIC_URL": origin,
            "GATEWAY_DATA_DIR": str(root / "data"), "GATEWAY_DATABASE": str(root / "gateway.db"),
            "GATEWAY_SECURE_COOKIES": "false", "PYTHONUTF8": "1",
        }
        gateway_log = root / "gateway.log"
        with gateway_log.open("wb") as stream:
            gateway = subprocess.Popen(
                [str(executable), "-m", "gateway"], stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        cleanup.callback(suite._stop, gateway)
        client = cleanup.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=5))
        wait_for(lambda: client.get("/api/health").status_code == 200, gateway, gateway_log)
        client.post("/api/auth/register", json={
            "email": "startup-fixture@example.test", "password": uuid.uuid4().hex,
        }).raise_for_status()
        bundle = client.post("/api/agent-bundles", json={
            "name": "Scheduled Windows fixture", "runtime": "python", "profiles": ["system_observe.v1"],
        }).raise_for_status().json()
        with zipfile.ZipFile(io.BytesIO(client.get(bundle["bundle"]["download_url"]).raise_for_status().content)) as zip_file:
            zip_file.extractall(root / "bundle")
        launcher, = (root / "bundle").rglob("run.py")
        template = (launcher.parent / "remote-agent.task.xml.example").read_bytes()
        config = root / "state" / "config.json"
        _render(template, sid, executable, launcher, config, workdir, xml_file)

        def agents() -> list[dict]:
            return client.get("/api/agents").raise_for_status().json()["agents"]

        enrollment_log = root / "enrollment.log"
        with enrollment_log.open("wb") as stream:
            foreground = subprocess.Popen(
                [
                    str(executable), str(launcher), "--gateway", f"ws://127.0.0.1:{port}/ws/agent",
                    "--token", bundle["secret"], "--config", str(config), "--workdir", str(workdir),
                ],
                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        try:
            agent_id = wait_for(lambda: next((agent["id"] for agent in agents() if agent["online"] and config.is_file()), None),
                                foreground, enrollment_log)
        finally:
            suite._stop(foreground)

        def ready(online: bool) -> bool:
            records = agents()
            return len(records) == 1 and records[0]["id"] == agent_id and records[0]["online"] is online

        def health() -> str:
            task_id = client.post("/api/tasks", json={
                "kind": "health_check", "agent_ids": [agent_id], "requested_runtime": None,
            }).raise_for_status().json()["task"]["id"]
            endpoint = f"/api/tasks/{task_id}"

            def finished() -> dict | None:
                task = client.get(endpoint).raise_for_status().json()["task"]
                return task if task["status"] not in {"queued", "running"} else None

            result = wait_for(finished, gateway, gateway_log)
            if result["status"] != "completed":
                raise RuntimeError(f"Scheduled Agent health verification failed for {task_id}")
            evidence, = client.get(f"{endpoint}/evidence").raise_for_status().json()["evidence"]
            summary = evidence["summary"]
            if evidence["agent_id"] != agent_id or summary["observations"]["get_host_info"]["os"] != "windows":
                raise RuntimeError("Health evidence does not match the enrolled Windows Agent")
            return task_id

        wait_for(lambda: ready(False), gateway, gateway_log)
        _scheduler(task_name, "register", xml_file)
        cleanup.callback(_scheduler, task_name, "cleanup", xml_file)
        task_ids = []
        for _ in range(2):
            _scheduler(task_name, "start", xml_file)
            wait_for(lambda: ready(True), gateway, gateway_log)
            task_ids.append(health())
            _scheduler(task_name, "stop", xml_file)
            wait_for(lambda: ready(False), gateway, gateway_log)
    print(f"Windows Task Scheduler: two on-demand starts, same enrollment, stopped and removed; Tasks {', '.join(task_ids)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Opt-in Windows startup test. Uses a disposable task with its logon trigger disabled.")
    parser.add_argument("--confirm-temporary-task", action="store_true")
    args = parser.parse_args()
    if os.name != "nt" or not args.confirm_temporary_task:
        parser.error("Run on Windows and explicitly pass --confirm-temporary-task")
    verify()


if __name__ == "__main__":
    main()
