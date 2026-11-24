"""Run the generated systemd unit once in Ubuntu WSL, with explicit fixture-only privileges.

From an authorized WSL root shell: /usr/bin/python3 -IBS tests/wsl_startup_acceptance.py --test-user nobody
Python 3.12, python3.12-venv, and access to public PyPI (or --wheelhouse) are required.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import pwd
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from contextlib import contextmanager
from http.cookies import SimpleCookie
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RUN = Path("/run")
UNITS = RUN / "systemd/system"
PROFILE = "system_observe.v1"
ACTIONS = {"get_host_info", "get_resources", "get_network", "get_processes"}
TEMPLATE_SHA256 = "8adff3448109a6e572f38435e8c271be6c3d639a069daadecf5917c63c65e25d"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(probe, description, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if result := probe():
            return result
        time.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {description}")


def systemctl(*args, check=True):
    return subprocess.run(
        ["/usr/bin/systemctl", "--system", "--no-pager", "--no-ask-password", *args],
        env=ENV, check=check, capture_output=True, text=True, timeout=18,
    )


def properties(unit):
    fields = (
        "LoadState", "ActiveState", "SubState", "MainPID", "InvocationID", "FragmentPath",
        "UnitFileState", "DropInPaths", "Result", "ExecMainCode", "ExecMainStatus", "NRestarts",
    )
    result = systemctl("show", f"--property={','.join(fields)}", "--", unit.name, check=False)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def job(action, unit):
    result = systemctl("--job-mode=ignore-dependencies", action, "--", unit.name, check=False)
    require(result.returncode == 0, f"{action} {unit.name}: {result.stderr.strip()}")


def same_path(path, original, parent):
    return (
        path.parent == parent and path.resolve() == path
        and os.path.samestat(path.lstat(), original) and path.lstat().st_uid == 0
    )


def uninstall(unit, original):
    require(same_path(unit, original, UNITS), f"Refusing to remove unexpected unit path {unit}")
    state = properties(unit)
    if state.get("FragmentPath") == str(unit):
        job("stop", unit)
        state = properties(unit)
        require(state.get("MainPID") == "0" and state.get("ActiveState") in {"inactive", "failed"},
                f"Unit did not stop; left {unit} for inspection")
        if state["ActiveState"] == "failed":
            systemctl("reset-failed", "--", unit.name)
    else:
        require(state.get("LoadState") == "not-found", f"Unexpected manager unit path for {unit}")
    unit.unlink()
    wait_for(lambda: properties(unit).get("LoadState") == "not-found", "unit removal", seconds=10)


def private_dir(path, identity):
    path.mkdir(mode=0o700)
    os.chown(path, identity["user"], identity["group"])
    return path


def prepare(root, identity, wheelhouse):
    source = root / "source"
    for package in ("gateway", "remote_agent"):
        for path in (REPO / package).rglob("*"):
            relative = path.relative_to(REPO)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            require(not path.is_symlink(), f"Unexpected symlink in application snapshot: {relative}")
            if path.is_file():
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    provision = private_dir(root / "provision", identity)
    temp_venv = provision / "venv"
    at = {**identity, "umask": 0o022}
    env = {**ENV, "HOME": str(provision), "TMPDIR": str(provision), "PIP_CONFIG_FILE": "/dev/null"}
    subprocess.run(["/usr/bin/python3", "-m", "venv", temp_venv], env=env, check=True, timeout=45, **at)
    dependencies = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    index = ["--no-index", "--find-links", wheelhouse] if wheelhouse else ["--index-url", "https://pypi.org/simple"]
    subprocess.run(
        [temp_venv / "bin/python", "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-cache-dir",
         "--no-input", "--timeout", "12", "--retries", "1", "install", "--quiet", "--only-binary=:all:",
         *index, "--", *dependencies],
        env=env, check=True, timeout=100, **at,
    )
    package_root = root / "agent"
    package_root.mkdir()
    shutil.copytree(temp_venv, package_root / ".venv", symlinks=True)
    return package_root, source


@contextmanager
def child(command, log, *, env, cwd, identity, stdin=subprocess.DEVNULL, pass_fds=()):
    with log.open("wb") as output:
        process = subprocess.Popen(
            command, env=env, cwd=cwd, stdin=stdin, stdout=output, stderr=subprocess.STDOUT,
            pass_fds=pass_fds, umask=0o077, **identity,
        )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError(f"Forced cleanup of fixture process {process.pid}")


class Gateway:
    def __init__(self, port):
        self.port = port
        self.cookie = None

    def request(self, endpoint, *, body=None, status=200, binary=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            connection.request("GET" if body is None else "POST", endpoint, json.dumps(body) if body else None, headers)
            response = connection.getresponse()
            require(response.status == status, f"{endpoint}: expected HTTP {status}, got {response.status}")
            if cookie := response.getheader("Set-Cookie"):
                self.cookie = SimpleCookie(cookie)["remote_gateway_session"].OutputString().split(";", 1)[0]
            return response.read() if binary else json.load(response)
        finally:
            connection.close()

    def ready(self, process):
        require(process.poll() is None, "Disposable Gateway exited during startup")
        try:
            return self.request("/api/health")
        except (OSError, http.client.HTTPException):
            return None

    def device(self, online, agent_id=None):
        agents = self.request("/api/agents")["agents"]
        require(len(agents) <= 1, "Fixture registered more than one device")
        if not agents:
            return None
        agent = agents[0]
        require(agent_id is None or agent["id"] == agent_id, "Enrolled device ID changed")
        return agent if agent["online"] is online else None

    def health(self, agent_id):
        task = self.request("/api/tasks", body={
            "kind": "health_check", "agent_ids": [agent_id], "requested_runtime": None,
        }, status=202)["task"]
        endpoint = f"/api/tasks/{task['id']}"

        def finished():
            current = self.request(endpoint)["task"]
            return current if current["status"] not in {"running", "queued"} else None

        task = wait_for(finished, "native health task")
        require(task["status"] == "completed", f"Task {task['id']}: {task['status']}")
        records = self.request(f"{endpoint}/evidence")["evidence"]
        require(len(records) == 1, f"Task {task['id']} did not produce one evidence record")
        record = records[0]
        summary = record["summary"]
        observations = summary["observations"]
        digest = hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        require(record["task_id"] == task["id"] and record["agent_id"] == summary["agent_id"] == agent_id
                and record["kind"] == "health_check" and record["content_hash"] == digest
                and summary["runtime"] is None and summary["status"] in {"healthy", "warning"}
                and observations.keys() == ACTIONS and observations["get_host_info"]["os"] == "linux"
                and task["result"]["devices"] == [summary], f"Invalid native evidence for {task['id']}")
        return {"task_id": task["id"], "evidence_id": record["id"], "sha256": digest, "observations": sorted(ACTIONS)}


def instance(unit, identity):
    state = properties(unit)
    require(state["FragmentPath"] == str(unit) and state["ActiveState"] == "active"
            and state["SubState"] == "running" and state["NRestarts"] == "0", f"Unit is not healthy: {state}")
    pid = int(state["MainPID"])
    require(pid > 1 and state["InvocationID"].strip("0"), f"Missing real service PID or invocation: {state}")
    process = dict(line.split(":\t", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":\t" in line)
    require(
        all(int(value) == identity["user"] for value in process["Uid"].split())
        and all(int(value) == identity["group"] for value in process["Gid"].split())
        and set(process["Groups"].split()) <= {str(identity["group"])}
        and int(process["CapEff"], 16) == int(process["CapBnd"], 16) == 0 and process["NoNewPrivs"] == "1",
        f"Unexpected effective privilege for service PID {pid}",
    )
    return {"pid": pid, "invocation_id": state["InvocationID"]}


def exercise(gateway, root, unit, package_root, identity):
    name = "WSL startup acceptance"
    gateway.request("/api/auth/register", body={
        "email": "startup@example.test", "password": secrets.token_urlsafe(32),
    }, status=201)
    bundle = gateway.request("/api/agent-bundles", body={
        "name": name, "runtime": "python", "profiles": [PROFILE],
    }, status=201)
    payload = gateway.request(bundle["bundle"]["download_url"], binary=True)
    unpacked = root / "zip"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(unpacked)
    launcher, = unpacked.glob("*/run.py")
    template = (launcher.parent / "remote-agent.service.example").read_bytes()
    require(hashlib.sha256(template).hexdigest() == TEMPLATE_SHA256,
            "Generated systemd template changed; review it before updating the fixture pin")
    manifest = json.loads((launcher.parent / "remote_agent/manifest.json").read_text())
    require(manifest["profiles"] == [PROFILE], "Bundle is not read-only observation only")
    for item in launcher.parent.iterdir():
        item.rename(package_root / item.name)

    state = private_dir(root / "state", identity)
    workspace = private_dir(root / "work", identity)
    config = state / "config.json"
    python = package_root / ".venv/bin/python"
    enrollment = (
        "import runpy,sys; sys.argv=sys.argv[1:]+['--token',sys.stdin.readline().strip()]; "
        "sys.path.insert(0,sys.argv[0].rsplit('/',1)[0]); runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    with child(
        [python, "-c", enrollment, package_root / "run.py", "--config", config, "--workdir", workspace,
         "--gateway", f"ws://127.0.0.1:{gateway.port}/ws/agent"], root / "enroll.log",
        env=ENV, cwd=workspace, identity=identity, stdin=subprocess.PIPE,
    ) as foreground:
        foreground.stdin.write((bundle["secret"] + "\n").encode())
        foreground.stdin.close()
        agent = wait_for(lambda: config.is_file() and gateway.device(True), "foreground enrollment")
        foreground.send_signal(signal.SIGINT)
        require(foreground.wait(timeout=8) == 0, "Foreground Agent did not exit cleanly")
    agent_id = agent["id"]
    require(agent["name"] == name and [profile["id"] for profile in agent["profile_declarations"]] == [PROFILE],
            "Unexpected enrolled device capabilities")
    config_bytes = config.read_bytes()
    require(config.stat().st_uid == identity["user"] and stat.S_IMODE(config.stat().st_mode) == 0o600,
            "Enrolled config has unexpected ownership or mode")
    wait_for(lambda: gateway.device(False, agent_id), "foreground disconnect")

    rendered = template.decode().replace("User=remote-agent", "User=nobody").replace("Group=remote-agent", "Group=nogroup")
    for before, after in (("/opt/remote-agent", package_root), ("/var/lib/remote-agent", state), ("/srv/remote-agent-work", workspace)):
        rendered = rendered.replace(before, str(after))
    with unit.open("x", encoding="utf-8") as stream:
        original = os.fstat(stream.fileno())
        stream.write(rendered)
    try:
        loaded = properties(unit)
        require(loaded.get("LoadState") == "loaded" and loaded["FragmentPath"] == str(unit)
                and loaded["UnitFileState"] == "disabled" and not loaded["DropInPaths"], f"Unexpected runtime unit: {loaded}")
        observations = []
        for action in ("start", "restart"):
            job(action, unit)
            wait_for(lambda: gateway.device(True, agent_id), f"online device after systemctl {action}")
            process = instance(unit, identity)
            health = gateway.health(agent_id)
            require(instance(unit, identity) == process and config.read_bytes() == config_bytes,
                    f"Process or enrolled credentials changed after {action}")
            observations.append({**process, **health})
        started, restarted = observations
        require(started["pid"] != restarted["pid"] and started["invocation_id"] != restarted["invocation_id"],
                "Restart did not create a distinct systemd invocation")
        job("stop", unit)
        wait_for(lambda: gateway.device(False, agent_id), "systemd Agent disconnect")
        time.sleep(5.5)
        stopped = properties(unit)
        require(stopped["ActiveState"] == "inactive" and stopped["SubState"] == "dead"
                and stopped["Result"] == "success" and stopped["MainPID"] == "0"
                and not Path(f"/proc/{restarted['pid']}").exists() and gateway.device(False, agent_id),
                f"Service did not stay stopped past RestartSec: {stopped}")
        journal = subprocess.check_output(
            ["/usr/bin/journalctl", "--no-pager", "-q", "-o", "cat", "_PID=1", f"UNIT={unit.name}"],
            env=ENV, text=True, timeout=10,
        )
        clean_stops = journal.splitlines().count(f"{unit.name}: Deactivated successfully.")
        require(clean_stops == 2, "Expected PID 1 journal to record clean deactivation on restart and stop")
        return {
            "agent_id": agent_id, "bundle_sha256": hashlib.sha256(payload).hexdigest(), "template_sha256": TEMPLATE_SHA256,
            "started": started, "restarted": restarted, "stop": {
                **{field: stopped[field] for field in ("ActiveState", "SubState", "Result", "MainPID", "ExecMainCode", "ExecMainStatus")},
                "pid1_clean_deactivations": clean_stops,
            },
        }
    finally:
        uninstall(unit, original)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-user", choices=["nobody"], required=True)
    parser.add_argument("--wheelhouse", type=lambda value: Path(value).resolve(strict=True))
    args = parser.parse_args()
    require(os.geteuid() == 0 and sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
            "Run the orchestrator as WSL root with /usr/bin/python3 -IBS; its children are unprivileged")
    require(sys.version_info[:2] == (3, 12) and "microsoft" in os.uname().release.lower()
            and Path("/proc/1/comm").read_text().strip() == "systemd", "Requires Ubuntu WSL with Python 3.12 and systemd PID 1")
    require(UNITS.resolve() == UNITS and UNITS.stat().st_uid == 0 and not UNITS.stat().st_mode & 0o022
            and shutil.rmtree.avoids_symlink_attacks, "Unsafe runtime unit directory or temporary cleanup")
    os.umask(0o022)
    user = pwd.getpwnam(args.test_user)
    require((user.pw_uid, user.pw_gid, user.pw_dir) == (65534, 65534, "/nonexistent")
            and user.pw_shell == "/usr/sbin/nologin" and os.getgrouplist(user.pw_name, user.pw_gid) == [65534],
            "The existing nobody identity is not the expected unprivileged, non-login account")
    occupancy = subprocess.run(["/usr/bin/pgrep", "-u", str(user.pw_uid)], env=ENV, capture_output=True, timeout=5)
    require(occupancy.returncode == 1, "Refusing to share the nobody identity with running processes")
    identity = {"user": user.pw_uid, "group": user.pw_gid, "extra_groups": []}
    manager = systemctl("show", "-p", "Version", "--value").stdout.strip()
    before = systemctl("is-active", "network-online.target", check=False).stdout.strip()

    root = Path(tempfile.mkdtemp(prefix="rag-startup-", dir=RUN))
    original = root.lstat()
    unit = UNITS / f"{root.name}.service"
    print(f"Fixture: {root}; runtime unit: {unit}", file=sys.stderr, flush=True)
    root.chmod(0o755)
    try:
        package_root, source = prepare(root, identity, args.wheelhouse)
        data = private_dir(root / "gateway", identity)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            gateway = Gateway(listener.getsockname()[1])
            fd = listener.fileno()
            start_gateway = (
                "import socket,sys,uvicorn; "
                "uvicorn.Server(uvicorn.Config('gateway.app:app')).run(sockets=[socket.socket(fileno=int(sys.argv[1]))])"
            )
            with child(
                [package_root / ".venv/bin/python", "-c", start_gateway, str(fd)], root / "gateway.log",
                env={**ENV, "HOME": str(data), "TMPDIR": str(data), "GATEWAY_DATA_DIR": str(data),
                     "GATEWAY_PUBLIC_URL": f"http://127.0.0.1:{gateway.port}"},
                cwd=source, identity=identity, pass_fds=(fd,),
            ) as process:
                listener.close()
                wait_for(lambda: gateway.ready(process), "loopback Gateway")
                report = exercise(gateway, root, unit, package_root, identity)
        require(process.returncode in {0, -signal.SIGTERM}, "Disposable Gateway did not exit cleanly")
        after = systemctl("is-active", "network-online.target", check=False).stdout.strip()
        require(before == after, "Existing network-online target state changed during the fixture")
    finally:
        require(not unit.exists() and same_path(root, original, RUN), f"Not cleaning unexpected or live fixture path {root}")
        shutil.rmtree(root)
        require(not root.exists(), f"Fixture directory is still present: {root}")
    print(json.dumps({
        **report, "python": sys.version.split()[0], "systemd": manager, "test_user": args.test_user, "uid": user.pw_uid,
        "loopback_port": gateway.port, "fixture_removed": str(root), "unit_removed": str(unit),
        "network_online_target": after, "gateway_exit": process.returncode,
    }, sort_keys=True))


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    main()
