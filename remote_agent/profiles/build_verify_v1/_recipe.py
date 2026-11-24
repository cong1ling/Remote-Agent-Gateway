from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path


class _Result(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.skipped_cases = 0
        self._in_test = False
        self._last_skipped = 0

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._in_test = True

    def stopTest(self, test: unittest.TestCase) -> None:
        self._in_test = False
        super().stopTest(test)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        if self._in_test and self._last_skipped != self.testsRun:
            self.skipped_cases += 1
            self._last_skipped = self.testsRun
        super().addSkip(test, reason)


def _summarize(project: Path) -> dict[str, bool | int]:
    summary = {"compiled": False, "tests_run": 0, "failures": 0, "errors": 0, "skipped": 0}
    try:
        for path in project.rglob("*.py"):
            if path.is_file():
                compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    except SyntaxError:
        return summary

    sys.path.insert(0, str(project))
    suite = unittest.TestLoader().discover(str(project / "tests"))
    result = _Result()
    suite.run(result)
    return {
        "compiled": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures) + len(result.unexpectedSuccesses),
        "errors": len(result.errors),
        "skipped": result.skipped_cases,
    }


def _main() -> None:
    project = Path.cwd()
    if os.name == "nt":
        start = project.with_name("start")
        deadline = time.monotonic() + 20
        while not start.is_file():
            if time.monotonic() >= deadline:
                raise SystemExit(1)
            time.sleep(0.01)
    summary_path = project.with_name("result.json")
    summary_path.write_text(json.dumps(_summarize(project)), encoding="ascii")


if __name__ == "__main__":
    _main()
