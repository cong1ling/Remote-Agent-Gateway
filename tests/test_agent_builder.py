from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from gateway.agent_builder import PythonAgentBuilder


class AgentBuilderTests(unittest.TestCase):
    def test_python_bundle_contains_core_selected_profile_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder = PythonAgentBuilder(Path(directory))
            path, filename = builder.build(
                bundle_id="12345678-1234-1234-1234-123456789012",
                name="GPU Notebook",
                description="模型训练环境",
                profiles=["shell.v1"],
            )
            self.assertEqual(path.name, filename)
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                root = filename.removesuffix(".zip")
                self.assertIn(f"{root}/run.py", names)
                self.assertIn(f"{root}/remote_agent/core/runtime.py", names)
                self.assertIn(f"{root}/remote_agent/core/version.py", names)
                self.assertIn(f"{root}/remote_agent/profiles/shell_v1.py", names)
                manifest = json.loads(
                    archive.read(f"{root}/remote_agent/manifest.json").decode("utf-8")
                )
        self.assertEqual(manifest["profiles"], ["shell.v1"])
        self.assertEqual(manifest["agent_version"], "0.1.2")
        self.assertEqual(manifest["core_version"], 3)
        self.assertEqual(manifest["build_id"], "12345678-1234-1234-1234-123456789012")
        self.assertEqual(manifest["agent"]["name"], "GPU Notebook")
        self.assertEqual(manifest["agent"]["description"], "模型训练环境")

    def test_bundle_never_contains_enrollment_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder = PythonAgentBuilder(Path(directory))
            path, _ = builder.build(
                bundle_id="12345678-1234-1234-1234-123456789012",
                name="Safe Agent",
                description="",
                profiles=["shell.v1"],
            )
            with zipfile.ZipFile(path) as archive:
                text = "\n".join(
                    archive.read(name).decode("utf-8", "ignore") for name in archive.namelist()
                )
        self.assertNotIn("enroll_", text)

    def test_bundle_documents_persistent_config_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder = PythonAgentBuilder(Path(directory))
            path, filename = builder.build(
                bundle_id="12345678-1234-1234-1234-123456789012",
                name="Persistent Agent",
                description="",
                profiles=["shell.v1"],
            )
            with zipfile.ZipFile(path) as archive:
                root = filename.removesuffix(".zip")
                readme = archive.read(f"{root}/README.txt").decode("utf-8")
        self.assertIn(".agent/config.json", readme)
        self.assertIn("--config", readme)

    def test_downloaded_bundle_launcher_is_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder = PythonAgentBuilder(root)
            path, _ = builder.build(
                bundle_id="12345678-1234-1234-1234-123456789012",
                name="Runnable Agent",
                description="",
                profiles=["shell.v1"],
            )
            with zipfile.ZipFile(path) as archive:
                archive.extractall(root / "extracted")
            launcher = next((root / "extracted").rglob("run.py"))
            result = subprocess.run(
                [sys.executable, str(launcher), "--help"],
                cwd=launcher.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--manifest", result.stdout)


if __name__ == "__main__":
    unittest.main()
