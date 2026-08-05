from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent import MAX_CAPTURE_BYTES, TRUNCATION_MARKER, ShellExecutor


class ShellExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_returns_stdout_stderr_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = ShellExecutor(Path(directory))
            command = f'"{sys.executable}" -c "import sys; print(123); print(456, file=sys.stderr)"'
            result = await executor.execute("request-1", {"command": command, "timeout": 10})
        self.assertEqual(result["stdout"].strip(), "123")
        self.assertEqual(result["stderr"].strip(), "456")
        self.assertEqual(result["exit_code"], 0)

    async def test_exec_enforces_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = ShellExecutor(Path(directory))
            command = f'"{sys.executable}" -c "import time; time.sleep(2)"'
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                await executor.execute("request-2", {"command": command, "timeout": 0.1})
            self.assertLess(time.monotonic() - started, 1.5)

    async def test_exec_caps_output_without_blocking_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = ShellExecutor(Path(directory))
            command = f'"{sys.executable}" -c "print(\'x\' * {MAX_CAPTURE_BYTES + 1024})"'
            result = await executor.execute("request-3", {"command": command, "timeout": 10})
        encoded = result["stdout"].encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_CAPTURE_BYTES + len(TRUNCATION_MARKER))
        self.assertIn("output truncated by remote agent", result["stdout"])


if __name__ == "__main__":
    unittest.main()
