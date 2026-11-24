from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import nullcontext, suppress
from pathlib import Path
from threading import Event

if os.name == "nt":
    from .windows_job import WindowsJob

RUN_TIMEOUT_SECONDS = 20
_POLL_SECONDS = 0.02
_MAX_RESULT_BYTES = 256
_MAX_COUNT = 100_000
_COUNTS = {"tests_run", "failures", "errors", "skipped"}
_RUNNER_FAILED = "build.runner_failed"
_RECIPE_PATH = Path(__file__).resolve().with_name("_recipe.py")


def _environment(scratch: Path) -> dict[str, str]:
    home = scratch / "home"
    temporary = scratch / "temp"
    home.mkdir()
    temporary.mkdir()
    env = {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
    }
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
        if system_root := os.environ.get("SystemRoot"):
            env["SystemRoot"] = system_root
    return env


def _read_summary(path: Path) -> dict[str, bool | int]:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or info.st_size > _MAX_RESULT_BYTES
    ):
        raise ValueError(_RUNNER_FAILED)
    with path.open("rb") as file:
        data = file.read(_MAX_RESULT_BYTES + 1)
    if len(data) > _MAX_RESULT_BYTES:
        raise ValueError(_RUNNER_FAILED)
    summary = json.loads(data)
    if (
        not isinstance(summary, dict)
        or summary.keys() != _COUNTS | {"compiled"}
        or type(summary["compiled"]) is not bool
        or any(type(summary[field]) is not int or not 0 <= summary[field] <= _MAX_COUNT for field in _COUNTS)
        or summary["skipped"] > summary["tests_run"]
        or (not summary["compiled"] and any(summary[field] for field in _COUNTS))
    ):
        raise ValueError(_RUNNER_FAILED)
    return summary


def run_verification(project: Path, cancelled: Event) -> dict[str, bool | int]:
    try:
        executable = Path(sys._base_executable if os.name == "nt" else sys.executable)
        if not executable.is_absolute():
            raise ValueError(_RUNNER_FAILED)
        scratch = project.parent
        env = _environment(scratch)
        if cancelled.is_set():
            raise asyncio.CancelledError
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with (WindowsJob() if os.name == "nt" else nullcontext()) as job:
            deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
            process = subprocess.Popen(
                [str(executable), "-I", "-S", "-B", str(_RECIPE_PATH)],
                cwd=project,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **options,
            )
            try:
                if job is not None:
                    job.assign(process.pid)
                    (scratch / "start").touch(exist_ok=False)
                while True:
                    if cancelled.is_set():
                        raise asyncio.CancelledError
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError(_RUNNER_FAILED)
                    if process.poll() is not None:
                        break
                    cancelled.wait(min(_POLL_SECONDS, remaining))
            finally:
                try:
                    if job is not None:
                        job.terminate()
                    else:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                finally:
                    with suppress(ProcessLookupError):
                        if process.poll() is None:
                            process.kill()
                    process.wait(timeout=2)
        if process.returncode != 0:
            raise ValueError(_RUNNER_FAILED)
        return _read_summary(scratch / "result.json")
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        raise ValueError(_RUNNER_FAILED) from None
