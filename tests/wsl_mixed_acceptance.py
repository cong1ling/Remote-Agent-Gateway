from __future__ import annotations

import argparse
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path

import httpx

from tests.test_e2e_gateway_agent import GatewayAgentEndToEndTests


WINDOWS_LAUNCHER = """
import subprocess
import sys

process = subprocess.Popen([sys.executable, *sys.argv[1:]], stdin=subprocess.DEVNULL)
try:
    sys.stdin.buffer.read(1)
finally:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
"""


def windows_path(path: Path) -> str:
    return subprocess.check_output(["wslpath", "-w", os.fspath(path)], text=True).strip()


def close_windows(process: subprocess.Popen) -> None:
    try:
        process.stdin.write(b"x")
        process.stdin.close()
    except OSError:
        pass
    process.wait(timeout=12)


def wait_for(lookup, process: subprocess.Popen, log: Path):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if result := lookup():
                return result
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Fixture did not reach expected state: {log.read_text(errors='replace')[-3000:]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Windows and mirrored Ubuntu WSL Agents through one loopback Gateway")
    parser.add_argument("windows_venv", type=Path, help="Windows virtual environment as a WSL-mounted path")
    args = parser.parse_args()
    shared = args.windows_venv.resolve()
    windows_python = shared / "Scripts" / "python.exe"
    if not windows_python.is_file():
        parser.error("the Windows virtual environment must already contain Scripts/python.exe and project dependencies")
    mode = subprocess.check_output(["wslinfo", "--networking-mode"], text=True).strip()
    if mode != "mirrored":
        parser.error("this loopback-only fixture requires WSL mirrored networking; it does not change networking settings")
    suite = GatewayAgentEndToEndTests
    with ExitStack() as cleanup:
        linux_root = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="rag-linux-agent-")))
        win_root = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="rag-windows-agent-", dir=shared)))
        port = suite._free_port()
        base = f"http://127.0.0.1:{port}"
        environment = {
            **os.environ,
            "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(port), "GATEWAY_PUBLIC_URL": base,
            "GATEWAY_SECURE_COOKIES": "false", "GATEWAY_DATA_DIR": str(linux_root / "data"),
            "GATEWAY_DATABASE": str(linux_root / "gateway.db"), "NO_PROXY": "127.0.0.1,localhost",
        }
        gateway_log = linux_root / "gateway.log"
        with gateway_log.open("wb") as output:
            gateway = subprocess.Popen(
                [sys.executable, "-m", "gateway"], env=environment, stdout=output, stderr=subprocess.STDOUT,
            )
        cleanup.callback(suite._stop, gateway)
        client = cleanup.enter_context(httpx.Client(base_url=base, trust_env=False, timeout=10))
        wait_for(lambda: client.get("/api/health").status_code == 200, gateway, gateway_log)
        client.post("/api/auth/register", json={
            "email": "mixed@example.test", "password": "Disposable two systems acceptance 42",
        }).raise_for_status()

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as source:
            source.writestr("sample.py", "def value():\n    return 42\n")
            source.writestr("tests/test_sample.py", "import unittest\nfrom sample import value\n\n"
                            "class FixtureTest(unittest.TestCase):\n"
                            "    def test_value(self):\n        self.assertEqual(value(), 42)\n")
        source_zip = output.getvalue()
        source_hash = hashlib.sha256(source_zip).hexdigest()

        def enroll(name: str, device_root: Path, *, windows: bool) -> dict:
            package = client.post("/api/agent-bundles", json={
                "name": name, "runtime": "python", "profiles": ["system_observe.v1", "build_verify.v1"],
            }).raise_for_status().json()
            payload = client.get(package["bundle"]["download_url"]).raise_for_status().content
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                archive.extractall(device_root)
            launcher = next(device_root.rglob("run.py"))
            workspace = device_root / "workspace"
            workspace.mkdir()
            pinned_source = device_root / "source.zip"
            pinned_source.write_bytes(source_zip)
            render_path = windows_path if windows else os.fspath
            args = [
                render_path(launcher), "--gateway", f"ws://127.0.0.1:{port}/ws/agent",
                "--token", package["secret"], "--workdir", render_path(workspace),
                "--build-archive", render_path(pinned_source),
            ]
            command = [os.fspath(windows_python), "-c", WINDOWS_LAUNCHER] if windows else [sys.executable]
            log = device_root / f"{name}.log"
            with log.open("wb") as stream:
                agent = subprocess.Popen([*command, *args], env=environment, stdin=subprocess.PIPE if windows else subprocess.DEVNULL,
                                         stdout=stream, stderr=subprocess.STDOUT)
            cleanup.callback(close_windows if windows else suite._stop, agent)
            return wait_for(lambda: next((
                item for item in client.get("/api/agents").raise_for_status().json()["agents"]
                if item["name"] == name and item["online"]
            ), None), agent, log)

        linux = enroll("WSL Linux fixture", linux_root, windows=False)
        windows = enroll("Windows fixture", win_root, windows=True)
        actual = {next(profile["runtime"]["platform"] for profile in agent["profile_declarations"]
                       if profile["id"] == "system_observe.v1") for agent in (linux, windows)}
        assert actual == {"linux", "windows"}, actual

        def result(task_id: str) -> list[dict]:
            endpoint = f"/api/tasks/{task_id}"
            task = wait_for(
                lambda: (item if (item := client.get(endpoint).raise_for_status().json()["task"])["status"]
                         not in {"running", "queued"} else None),
                gateway, gateway_log,
            )
            assert task["status"] == "completed", task
            return client.get(f"{endpoint}/evidence").raise_for_status().json()["evidence"]

        health = client.post("/api/tasks", json={
            "kind": "health_check", "agent_ids": [linux["id"], windows["id"]],
        }).raise_for_status().json()["task"]["id"]
        assert {record["agent_id"] for record in result(health)} == {linux["id"], windows["id"]}

        plan = client.post("/api/task-plans", json={
            "objective": "Same trusted snapshot on Windows and Linux",
            "steps": [
                {"key": "linux", "agent_id": linux["id"], "operation": "build.verify",
                 "params": {"source_sha256": source_hash}},
                {"key": "windows", "agent_id": windows["id"], "operation": "build.verify",
                 "params": {"source_sha256": source_hash}, "depends_on": ["linux"]},
            ],
        }).raise_for_status().json()["task"]
        build = plan["id"]
        bound = {"plan_hash": plan["plan"]["hash"]}
        assert client.post(f"/api/tasks/{build}/execute", json=bound).status_code == 409
        client.post(f"/api/tasks/{build}/approval", json={**bound, "decision": "approve"}).raise_for_status()
        client.post(f"/api/tasks/{build}/execute", json=bound).raise_for_status()
        records = result(build)
        assert {entry["agent_id"] for entry in records} == {linux["id"], windows["id"]}
        assert all(entry["summary"]["verified"] and entry["summary"]["source_sha256"] == source_hash
                   and entry["summary"]["tests_run"] == 1 for entry in records)
        print(f"Mixed OS acceptance: one Gateway, Windows + Ubuntu WSL; native health {health}; "
              f"approved same-source builds {build}; SHA-256 {source_hash}", flush=True)


if __name__ == "__main__":
    main()
