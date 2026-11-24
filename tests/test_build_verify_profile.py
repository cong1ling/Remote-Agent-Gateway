from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
import textwrap
import unittest
import zipfile
from functools import partial
from pathlib import Path
from unittest import mock

from remote_agent.core.errors import FatalAgentError
from remote_agent.profiles.base import ProfileContext
from remote_agent.profiles.build_verify_v1 import BuildVerifyV1Profile, archive, runner
from remote_agent.profiles.build_verify_v1.actions import verify
from remote_agent.profiles.registry import build_profile_handlers


_PASSING_TEST = """\
    import os
    import sys
    import tempfile
    import unittest
    from pathlib import Path
    from example import answer

    class VerificationTest(unittest.TestCase):
        def test_source(self):
            self.assertEqual(answer(), 42)
            self.assertNotIn("RAG_VERIFY_PARENT_ONLY", os.environ)
            self.assertNotIn("PYTHONPATH", os.environ)
            self.assertEqual(Path.home(), Path.cwd().with_name("home"))
            self.assertEqual(Path(tempfile.gettempdir()), Path.cwd().with_name("temp"))
            self.assertEqual(sys.stdin.read(), "")
            print("private stdout")
            print("private stderr", file=sys.stderr)
"""

_SLEEPING_TEST = """\
    import os
    import time
    import unittest
    from pathlib import Path

    class VerificationTest(unittest.TestCase):
        def test_wait(self):
            (Path.home() / f"started-{os.getpid()}").touch()
            time.sleep(30)
"""


class BuildVerifyProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.workdir = self.root / "workspace"
        self.workdir.mkdir()
        self.snapshot = self.root / "source.zip"
        self.profile = BuildVerifyV1Profile(ProfileContext(self.workdir, build_archive=self.snapshot))

    def write_zip(self, entries: list[tuple[str | zipfile.ZipInfo, str | bytes]]) -> str:
        with zipfile.ZipFile(self.snapshot, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in entries:
                bundle.writestr(name, data)
        return hashlib.sha256(self.snapshot.read_bytes()).hexdigest()

    def write_project(self, tests: str = _PASSING_TEST, *, source: str = "def answer(): return 42") -> str:
        return self.write_zip([("example.py", source), ("tests/test_example.py", textwrap.dedent(tests))])

    async def run_verify(self, digest: str) -> dict:
        return await self.profile.execute("verify", "verify-request", {"source_sha256": digest})

    async def test_metadata_and_real_tests_in_a_fresh_minimal_environment(self) -> None:
        self.assertTrue(BuildVerifyV1Profile.plan_only)
        self.assertEqual(set(self.profile.actions), {"source", "verify"})
        self.assertEqual(self.profile.declaration(), {
            "id": "build_verify.v1",
            "runtime": {"backend": "python-unittest-zip.v1"},
        })
        digest = self.write_project()
        self.assertEqual(await self.profile.execute("source", "source-request", {}), {"source_sha256": digest})
        with mock.patch.dict(os.environ, {"RAG_VERIFY_PARENT_ONLY": "test value", "PYTHONPATH": str(self.workdir)}):
            result = await self.run_verify(digest)
        self.assertEqual(result, {
            "source_sha256": digest, "compiled": True, "tests_run": 1,
            "failures": 0, "errors": 0, "skipped": 0,
        })
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_only_provisioned_build_profile_is_registered(self) -> None:
        profile_ids = ["build_verify.v1"]
        with self.assertRaisesRegex(FatalAgentError, "--build-archive"):
            build_profile_handlers(profile_ids, self.workdir)
        with self.assertRaisesRegex(FatalAgentError, "--build-archive"):
            build_profile_handlers(profile_ids, self.workdir, build_archive=self.snapshot)
        self.write_project()
        handlers = build_profile_handlers(profile_ids, self.workdir, build_archive=self.snapshot)
        self.assertEqual(list(handlers), profile_ids)

    async def test_counts_real_failures_errors_and_skips(self) -> None:
        digest = self.write_project("""\
            import unittest

            class VerificationTest(unittest.TestCase):
                def test_failure(self):
                    self.fail("private failure")

                def test_error(self):
                    raise RuntimeError("private exception")

                @unittest.skip("private reason")
                def test_skip(self):
                    pass
        """)
        self.assertEqual(await self.run_verify(digest), {
            "source_sha256": digest, "compiled": True, "tests_run": 3,
            "failures": 1, "errors": 1, "skipped": 1,
        })

    async def test_compile_failure_is_evidence_even_for_unimported_python(self) -> None:
        digest = self.write_zip([
            ("unused.py", "def broken("),
            ("tests/test_example.py", "raise RuntimeError('must not run')"),
        ])
        self.assertEqual(await self.run_verify(digest), {
            "source_sha256": digest, "compiled": False, "tests_run": 0,
            "failures": 0, "errors": 0, "skipped": 0,
        })

    async def test_digest_mismatch_never_reaches_runner(self) -> None:
        digest = self.write_project()
        self.write_project(source="def answer(): return 99")
        with mock.patch.object(verify, "run_verification") as run:
            with self.assertRaisesRegex(ValueError, r"^build\.source_mismatch$"):
                await self.run_verify(digest)
            run.assert_not_called()

    async def test_extraction_uses_the_exact_bytes_that_were_hashed(self) -> None:
        digest = self.write_project()

        def replace_snapshot(data, project, check_active):
            self.write_project(source="raise RuntimeError('replacement must not run')")
            archive.inspect_zip(data, project, check_active)

        with mock.patch.object(verify, "inspect_zip", side_effect=replace_snapshot):
            result = await self.run_verify(digest)
        self.assertEqual((result["source_sha256"], result["tests_run"], result["errors"]), (digest, 1, 0))

    async def test_rejects_invalid_params_and_unprovisioned_or_workspace_archive(self) -> None:
        self.write_project()
        for action, params in [
            ("source", {"path": "some.zip"}),
            ("source", []),
            ("verify", {}),
            ("verify", {"source_sha256": "F" * 64}),
            ("verify", {"source_sha256": "0" * 64, "command": "anything"}),
        ]:
            with self.subTest(action=action, params=params):
                with self.assertRaisesRegex(ValueError, r"^build\.invalid_params$"):
                    await self.profile.execute(action, "bad-params", params)
        workspace_snapshot = self.workdir / "source.zip"
        workspace_snapshot.write_bytes(self.snapshot.read_bytes())
        for path in (None, workspace_snapshot):
            profile = BuildVerifyV1Profile(ProfileContext(self.workdir, build_archive=path))
            with self.assertRaisesRegex(ValueError, r"^build\.invalid_archive$"):
                await profile.execute("source", "bad-provisioning", {})

    async def test_rejects_escape_links_and_colliding_names_before_running(self) -> None:
        link = zipfile.ZipInfo("tests/link.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        reparse = zipfile.ZipInfo("tests/reparse.py")
        reparse.create_system = 0
        reparse.external_attr = stat.FILE_ATTRIBUTE_REPARSE_POINT
        backslash = zipfile.ZipInfo("tests/backslash.py")
        backslash.filename = "tests\\backslash.py"
        bad_entries = [
            "../escaped.py", "/absolute.py", "C:/absolute.py", backslash,
            "tests/aux.py", "tests/name.py:stream", "tests/TEST_EXAMPLE.py",
            "Tests/other.py", "tests", link, reparse,
        ]
        for entry in bad_entries:
            with self.subTest(entry=entry):
                digest = self.write_zip([("tests/test_example.py", ""), (entry, "../escaped.py")])
                with mock.patch.object(verify, "run_verification") as run:
                    with self.assertRaisesRegex(ValueError, r"^build\.invalid_archive$"):
                        await self.run_verify(digest)
                    run.assert_not_called()

    async def test_rejects_invalid_zip_and_enforces_each_archive_limit(self) -> None:
        malformed = b"not a zip"
        self.snapshot.write_bytes(malformed)
        with self.assertRaisesRegex(ValueError, r"^build\.invalid_archive$"):
            await self.run_verify(hashlib.sha256(malformed).hexdigest())
        digest = self.write_project()
        for limit, value in [
            ("MAX_ZIP_BYTES", self.snapshot.stat().st_size - 1),
            ("MAX_EXPANDED_BYTES", 16),
            ("MAX_ENTRIES", 1),
        ]:
            with self.subTest(limit=limit), mock.patch.object(archive, limit, value):
                with self.assertRaisesRegex(ValueError, r"^build\.invalid_archive$"):
                    await self.run_verify(digest)

    async def test_cancel_and_timeout_reap_runner_and_remove_request_state(self) -> None:
        digest = self.write_project(_SLEEPING_TEST)
        create_scratch = partial(tempfile.TemporaryDirectory, dir=self.root)
        popen = runner.subprocess.Popen
        processes = []
        started_pattern = "rag-build-*/home/started-*"

        def track_process(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            mock.patch.object(verify, "TemporaryDirectory", create_scratch),
            mock.patch.object(runner.subprocess, "Popen", side_effect=track_process),
        ):
            for cancel_through_profile in (True, False):
                with self.subTest(cancel_through_profile=cancel_through_profile):
                    task = asyncio.create_task(self.run_verify(digest))
                    try:
                        async with asyncio.timeout(8):
                            while not (started := list(self.root.glob(started_pattern))) and not task.done():
                                await asyncio.sleep(0.02)
                        self.assertFalse(task.done())
                        self.assertEqual([marker.name for marker in started], [f"started-{processes[-1].pid}"])
                        if cancel_through_profile:
                            await self.profile.cancel("verify-request")
                        else:
                            task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertEqual(list(self.root.glob("rag-build-*")), [])
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

            with mock.patch.object(runner, "RUN_TIMEOUT_SECONDS", 0.25):
                with self.assertRaisesRegex(ValueError, r"^build\.runner_failed$"):
                    await self.run_verify(digest)
        self.assertEqual(len(processes), 3)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertEqual(list(self.root.glob("rag-build-*")), [])
        self.assertEqual(self.profile.actions["verify"]._requests, {})
        self.assertEqual(self.profile._request_actions, {})

    async def test_scratch_cleanup_retries_only_sharing_violations(self) -> None:
        digest = self.write_project()
        create_scratch = partial(tempfile.TemporaryDirectory, dir=self.root)
        original_cleanup = tempfile.TemporaryDirectory.cleanup
        for winerror, retry_seconds, expected_calls in ((32, 0.5, 2), (32, 0, 1), (5, 0.5, 1)):
            with self.subTest(winerror=winerror, retry_seconds=retry_seconds):
                error = PermissionError("scratch file is temporarily locked")
                error.winerror = winerror
                with (
                    mock.patch.object(verify, "TemporaryDirectory", create_scratch),
                    mock.patch.object(verify, "_CLEANUP_RETRY_SECONDS", retry_seconds),
                    mock.patch.object(tempfile.TemporaryDirectory, "cleanup", autospec=True) as cleanup,
                    mock.patch.object(verify, "run_verification", wraps=verify.run_verification) as build,
                ):
                    def release_lock(scratch):
                        if cleanup.call_count == 1:
                            raise error
                        original_cleanup(scratch)

                    cleanup.side_effect = release_lock
                    if expected_calls == 2:
                        result = await self.run_verify(digest)
                        self.assertEqual(result["tests_run"], 1)
                        self.assertEqual(list(self.root.glob("rag-build-*")), [])
                    else:
                        with self.assertRaisesRegex(ValueError, r"^build\.runner_failed$"):
                            await self.run_verify(digest)
                    self.assertEqual(cleanup.call_count, expected_calls)
                    build.assert_called_once()


if __name__ == "__main__":
    unittest.main()
