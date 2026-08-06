from __future__ import annotations

import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path

from remote_agent.profiles.shell_v1 import (
    MAX_CAPTURE_BYTES,
    TRUNCATION_MARKER,
    ShellExecutor,
    ShellRuntime,
    detect_shell_runtime,
)


def python_command(runtime: ShellRuntime, code: str) -> str:
    executable = str(sys.executable)
    if runtime.dialect == "powershell":
        escaped_executable = executable.replace("'", "''")
        escaped_code = code.replace("'", "''")
        return f"& '{escaped_executable}' -c '{escaped_code}'"
    if runtime.dialect == "cmd":
        return f'"{executable}" -c "{code}"'
    return f"{shlex.quote(executable)} -c {shlex.quote(code)}"


class ShellExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_returns_stdout_stderr_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = ShellExecutor(Path(directory))
            command = python_command(
                executor.runtime,
                "import sys;print(123);print(456,file=sys.stderr);sys.exit(7)",
            )
            result = await executor.execute("request-1", {"command": command, "timeout": 10})
        self.assertEqual(result["stdout"].strip(), "123")
        self.assertEqual(result["stderr"].strip(), "456")
        self.assertEqual(result["exit_code"], 7)

    async def test_exec_enforces_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = ShellExecutor(Path(directory))
            command = python_command(executor.runtime, "import time;time.sleep(2)")
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                await executor.execute("request-2", {"command": command, "timeout": 0.1})
            self.assertLess(time.monotonic() - started, 1.5)

    async def test_exec_caps_output_without_blocking_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = ShellExecutor(Path(directory))
            command = python_command(
                executor.runtime,
                f"print(chr(120)*{MAX_CAPTURE_BYTES + 1024})",
            )
            result = await executor.execute("request-3", {"command": command, "timeout": 10})
        encoded = result["stdout"].encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_CAPTURE_BYTES + len(TRUNCATION_MARKER))
        self.assertIn("output truncated by remote agent", result["stdout"])

    def test_windows_detection_prefers_powershell_then_cmd(self) -> None:
        executables = {
            "pwsh": "C:\\Program Files\\PowerShell\\7\\pwsh.exe",
            "powershell.exe": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
            "cmd.exe": "C:\\Windows\\System32\\cmd.exe",
        }

        runtime = detect_shell_runtime(
            "Windows",
            which=executables.get,
            is_file=lambda _: True,
            environment={},
        )
        self.assertEqual(runtime.dialect, "powershell")
        self.assertEqual(runtime.public_metadata()["executable"], "pwsh.exe")

        runtime = detect_shell_runtime(
            "Windows",
            which=lambda name: executables.get(name) if name in {"cmd.exe", "cmd"} else None,
            is_file=lambda _: False,
            environment={},
        )
        self.assertEqual(runtime.dialect, "cmd")

    def test_posix_detection_uses_bin_sh(self) -> None:
        runtime = detect_shell_runtime("Linux", is_file=lambda path: path == "/bin/sh")
        self.assertEqual(
            runtime.public_metadata(),
            {"os": "linux", "dialect": "posix-sh", "executable": "sh"},
        )


if __name__ == "__main__":
    unittest.main()
