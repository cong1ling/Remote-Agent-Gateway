from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import Path

import httpx

from tests.phase1_site_acceptance import ACTIONS, AcceptanceError, _canonical_json, _check_observation
from tests.test_e2e_gateway_agent import GatewayAgentEndToEndTests
from tests.wsl_mixed_acceptance import wait_for


_BASE_LABEL = "com.remote-agent.gateway"
_LABEL_PATTERN = re.compile(r"com\.remote-agent\.gateway\.acceptance-[0-9a-f]{32}")


def _render(template: bytes, label: str, executable: Path, launcher: Path, config: Path, workdir: Path) -> bytes:
    job = plistlib.loads(template)
    if (
        not isinstance(job, dict) or job.keys() != {"Label", "RunAtLoad", "WorkingDirectory", "ProgramArguments"}
        or job["Label"] != _BASE_LABEL or not _LABEL_PATTERN.fullmatch(label) or job["RunAtLoad"] is not True
    ):
        raise ValueError("Expected the packaged LaunchAgent with a disposable label and no keep-alive")
    arguments = job["ProgramArguments"]
    if (
        not isinstance(arguments, list) or len(arguments) != 6 or arguments[2::2] != ["--config", "--workdir"]
        or not all(path.is_absolute() for path in (executable, launcher, config, workdir))
        or any(path.is_relative_to(workdir) for path in (executable, launcher, config))
    ):
        raise ValueError("Expected separate absolute paths and the existing device config")
    job["Label"] = label
    job["WorkingDirectory"] = str(workdir)
    job["ProgramArguments"] = [str(executable), str(launcher), "--config", str(config), "--workdir", str(workdir)]
    return plistlib.dumps(job)


def _launchctl(*arguments: str, check: bool = True) -> int:
    result = subprocess.run(
        ["/bin/launchctl", *arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=25,
    )
    if check and result.returncode:
        raise RuntimeError(f"launchctl {arguments[0]} failed ({result.returncode}); use a logged-in macOS test account")
    return result.returncode


def _unload(target: str) -> None:
    if not _launchctl("print", target, check=False):
        _launchctl("bootout", target)
    if not _launchctl("print", target, check=False):
        raise RuntimeError(f"Disposable LaunchAgent remains loaded: {target}")


def _health(client: httpx.Client, gateway: subprocess.Popen, log: Path, agent_id: str) -> dict:
    task_id = client.post("/api/tasks", json={
        "kind": "health_check", "agent_ids": [agent_id], "requested_runtime": None,
    }).raise_for_status().json()["task"]["id"]
    endpoint = f"/api/tasks/{task_id}"

    def finished() -> dict | None:
        task = client.get(endpoint).raise_for_status().json()["task"]
        return task if task["status"] not in {"queued", "running"} else None

    task = wait_for(finished, gateway, log)
    if task["status"] != "completed":
        raise RuntimeError(f"LaunchAgent health verification failed for {task_id}")
    evidence, = client.get(f"{endpoint}/evidence").raise_for_status().json()["evidence"]
    summary = evidence["summary"]
    observations = summary["observations"]
    digest = hashlib.sha256(_canonical_json(summary)).hexdigest()
    if (
        evidence["agent_id"] != agent_id or evidence["kind"] != "health_check"
        or summary["runtime"] is not None or observations.keys() != set(ACTIONS)
        or evidence["content_hash"] != digest
    ):
        raise RuntimeError(f"Health evidence does not match the enrolled macOS Agent: {task_id}")
    for action, observation in observations.items():
        _check_observation(action, observation, "macos")
    if observations["get_host_info"]["privileges"]["is_root"] is not False:
        raise RuntimeError(f"LaunchAgent did not run as the unprivileged test user: {task_id}")
    return {"task_id": task_id, "evidence_id": evidence["id"], "evidence_sha256": digest}


def verify() -> dict:
    if sys.platform != "darwin" or os.geteuid() == 0:
        raise RuntimeError("Use a non-root macOS account with an existing GUI session")
    domain = f"gui/{os.getuid()}"
    _launchctl("print", domain)
    label = f"{_BASE_LABEL}.acceptance-{uuid.uuid4().hex}"
    target = f"{domain}/{label}"
    if _launchctl("print", target, check=False) == 0:
        raise RuntimeError("Disposable LaunchAgent label already exists")

    suite = GatewayAgentEndToEndTests
    with ExitStack() as cleanup:
        root = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="rag-macos-startup-"))).resolve()
        workdir = root / "isolated workspace"
        workdir.mkdir()
        config = root / "state" / "config.json"
        port = suite._free_port()
        origin = f"http://127.0.0.1:{port}"
        env = {
            **os.environ, "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(port), "GATEWAY_PUBLIC_URL": origin,
            "GATEWAY_DATA_DIR": str(root / "data"), "GATEWAY_DATABASE": str(root / "gateway.db"),
            "GATEWAY_SECURE_COOKIES": "false", "NO_PROXY": "127.0.0.1,localhost",
        }
        gateway_log = root / "gateway.log"
        with gateway_log.open("wb") as stream:
            gateway = subprocess.Popen(
                [sys.executable, "-m", "gateway"], env=env, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT,
            )
        cleanup.callback(suite._stop, gateway)
        client = cleanup.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=5))
        wait_for(lambda: client.get("/api/health").status_code == 200, gateway, gateway_log)
        client.post("/api/auth/register", json={
            "email": "macos-startup-fixture@example.test", "password": uuid.uuid4().hex,
        }).raise_for_status()
        bundle = client.post("/api/agent-bundles", json={
            "name": "Temporary macOS fixture", "runtime": "python", "profiles": ["system_observe.v1"],
        }).raise_for_status().json()
        payload = client.get(bundle["bundle"]["download_url"]).raise_for_status().content
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            archive.extractall(root / "bundle")
        launcher, = (root / "bundle").rglob("run.py")
        template = (launcher.parent / f"{_BASE_LABEL}.plist.example").read_bytes()
        plist_file = root / f"{label}.plist"
        plist_file.write_bytes(_render(template, label, Path(sys.executable), launcher, config, workdir))
        subprocess.run(["/usr/bin/plutil", "-lint", str(plist_file)], check=True, stdout=subprocess.DEVNULL, timeout=5)

        def agents() -> list[dict]:
            return client.get("/api/agents").raise_for_status().json()["agents"]

        enrollment_log = root / "enrollment.log"
        with enrollment_log.open("wb") as stream:
            foreground = subprocess.Popen(
                [sys.executable, str(launcher), "--gateway", f"ws://127.0.0.1:{port}/ws/agent",
                 "--token", bundle["secret"], "--config", str(config), "--workdir", str(workdir)],
                env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            )
        try:
            agent_id = wait_for(lambda: next((agent["id"] for agent in agents() if agent["online"] and config.is_file()), None),
                                foreground, enrollment_log)
        finally:
            suite._stop(foreground)
        credentials_before = config.read_bytes()

        def ready(online: bool) -> bool:
            records = agents()
            return len(records) == 1 and records[0]["id"] == agent_id and records[0]["online"] is online

        wait_for(lambda: ready(False), gateway, gateway_log)
        cleanup.callback(_unload, target)
        checks = []
        for _ in range(2):
            _launchctl("bootstrap", domain, str(plist_file))
            wait_for(lambda: ready(True), gateway, gateway_log)
            checks.append(_health(client, gateway, gateway_log, agent_id))
            _unload(target)
            wait_for(lambda: ready(False), gateway, gateway_log)
        if config.read_bytes() != credentials_before:
            raise RuntimeError("Restart changed the local device credentials")
    return {
        "platform": "macos", "macos_version": platform.mac_ver()[0], "architecture": platform.machine(),
        "python": platform.python_version(), "agent_id": agent_id, "bundle_sha256": hashlib.sha256(payload).hexdigest(),
        "checks": checks, "temporary_job_removed": True, "login_trigger_tested": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the packaged LaunchAgent with a temporary job in the current GUI session.")
    parser.add_argument("--confirm-temporary-launchagent", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or not args.confirm_temporary_launchagent:
        parser.error("Run on macOS and explicitly pass --confirm-temporary-launchagent")
    try:
        report = verify()
    except (AcceptanceError, RuntimeError, OSError, ValueError, subprocess.SubprocessError, httpx.HTTPError) as error:
        sys.exit(f"macOS startup verification failed: {error}")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
