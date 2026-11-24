from __future__ import annotations

import json
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree

from gateway.agent_builder import MACOS_LAUNCH_AGENT_TEMPLATE, PythonAgentBuilder
from tests.macos_startup_acceptance import _render as render_temporary_launch_agent


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
                self.assertIn(f"{root}/remote_agent/profiles/registry.py", names)
                self.assertIn(f"{root}/remote_agent/profiles/common/filesystem.py", names)
                self.assertIn(f"{root}/remote_agent/profiles/common/planned_action.py", names)
                self.assertIn(f"{root}/remote_agent/profiles/shell_v1/__init__.py", names)
                self.assertIn(f"{root}/remote_agent/profiles/shell_v1/actions/exec.py", names)
                self.assertFalse(any("__pycache__" in name for name in names))
                self.assertFalse(any(name.endswith(".pyc") for name in names))
                self.assertIn(f"{root}/.gitignore", names)
                self.assertIn(f"{root}/github-actions.yml.example", names)
                self.assertIn(f"{root}/remote-agent.service.example", names)
                self.assertIn(f"{root}/Dockerfile.agent.example", names)
                self.assertIn(f"{root}/compose.agent.yml.example", names)
                manifest = json.loads(
                    archive.read(f"{root}/remote_agent/manifest.json").decode("utf-8")
                )
        self.assertEqual(manifest["profiles"], ["shell.v1"])
        self.assertEqual(manifest["agent_version"], "0.2.3")
        self.assertEqual(manifest["core_version"], 6)
        self.assertEqual(manifest["build_id"], "12345678-1234-1234-1234-123456789012")
        self.assertEqual(manifest["agent"]["name"], "GPU Notebook")
        self.assertEqual(manifest["agent"]["description"], "模型训练环境")

    def test_python_bundle_contains_workspace_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder = PythonAgentBuilder(Path(directory))
            path, filename = builder.build(
                bundle_id="12345678-1234-1234-1234-123456789012",
                name="Workspace Agent",
                description="文本工作区",
                profiles=["workspace.v1"],
            )
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                root = filename.removesuffix(".zip")
        self.assertIn(f"{root}/remote_agent/profiles/workspace_v1/profile.py", names)
        self.assertIn(f"{root}/remote_agent/profiles/workspace_v1/actions/read.py", names)
        self.assertIn(f"{root}/remote_agent/profiles/workspace_v1/actions/write.py", names)
        self.assertIn(f"{root}/remote_agent/profiles/workspace_v1/actions/edit.py", names)

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
        credential = re.search(r"(?<![A-Za-z0-9_-])enroll_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", text)
        self.assertFalse(bool(credential), "ZIP must not contain an enrollment token")

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
        self.assertIn("REMOTE_AGENT_CONFIG_B64", readme)
        self.assertIn("github-actions.yml.example", readme)
        self.assertIn("--workdir limits workspace.v1", readme)
        self.assertIn("NOT a shell sandbox", readme)
        self.assertIn("Standard local user", readme)
        self.assertIn("remote-agent.task.xml.example", readme)
        self.assertIn("com.remote-agent.gateway.plist.example", readme)
        self.assertIn("~/Library/LaunchAgents", readme)
        self.assertIn("enroll once in the foreground", readme)
        self.assertIn("Later starts use the existing local config only", readme)
        self.assertIn("useradd --system", readme)
        self.assertIn("sudo -u remote-agent", readme)
        self.assertIn("docker compose -f compose.agent.yml.example run --rm", readme)
        self.assertIn(
            "--config /var/lib/remote-agent/config.json --workdir /workspace",
            readme,
        )

    def test_bundle_includes_guarded_github_actions_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder = PythonAgentBuilder(Path(directory))
            path, filename = builder.build(
                bundle_id="12345678-1234-1234-1234-123456789012",
                name="CI Runner",
                description="",
                profiles=["shell.v1"],
            )
            with zipfile.ZipFile(path) as archive:
                root = filename.removesuffix(".zip")
                workflow = archive.read(
                    f"{root}/github-actions.yml.example"
                ).decode("utf-8")
                gitignore = archive.read(f"{root}/.gitignore").decode("utf-8")

        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("timeout-minutes: 60", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertIn("secrets.REMOTE_AGENT_CONFIG_B64", workflow)
        self.assertIn("--config-env REMOTE_AGENT_CONFIG_B64", workflow)
        self.assertIn(root, workflow)
        self.assertIn(".agent/", gitignore)

    def test_bundle_includes_least_privilege_service_and_container_templates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder = PythonAgentBuilder(Path(directory))
            path, filename = builder.build(
                bundle_id="12345678-1234-1234-1234-123456789012",
                name="Production Agent",
                description="",
                profiles=["shell.v1"],
            )
            with zipfile.ZipFile(path) as archive:
                root = filename.removesuffix(".zip")
                systemd = archive.read(f"{root}/remote-agent.service.example").decode()
                dockerfile = archive.read(f"{root}/Dockerfile.agent.example").decode()
                compose = archive.read(f"{root}/compose.agent.yml.example").decode()

        self.assertIn("User=remote-agent", systemd)
        self.assertIn("NoNewPrivileges=true", systemd)
        self.assertIn("CapabilityBoundingSet=", systemd)
        self.assertIn("ReadWritePaths=/var/lib/remote-agent /srv/remote-agent-work", systemd)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("read_only: true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn("no-new-privileges:true", compose)

    def test_desktop_startup_templates_use_the_current_user_and_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder = PythonAgentBuilder(Path(directory))
            path, filename = builder.build(
                bundle_id="12345678-1234-1234-1234-123456789012",
                name="Desktop Agent",
                description="",
                profiles=["workspace.v1"],
            )
            root = filename.removesuffix(".zip")
            with zipfile.ZipFile(path) as archive:
                windows = ElementTree.fromstring(archive.read(f"{root}/remote-agent.task.xml.example"))
                macos_name = "com.remote-agent.gateway.plist"
                macos = plistlib.loads(archive.read(f"{root}/{macos_name}.example"))

        namespaces = {"": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        principal = windows.find("Principals/Principal", namespaces)
        actions = windows.find("Actions", namespaces)
        self.assertEqual(actions.get("Context"), principal.get("id"))
        current_user = "REPLACE_WITH_CURRENT_USER_SID"
        self.assertEqual(principal.findtext("UserId", namespaces=namespaces), current_user)
        self.assertEqual(windows.findtext("Triggers/LogonTrigger/UserId", namespaces=namespaces), current_user)
        self.assertEqual(principal.findtext("LogonType", namespaces=namespaces), "InteractiveToken")
        self.assertEqual(principal.findtext("RunLevel", namespaces=namespaces), "LeastPrivilege")
        self.assertIsNone(principal.find("GroupId", namespaces))
        self.assertEqual(len(windows.findall("Triggers/*", namespaces)), 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(windows.findtext("Settings/MultipleInstancesPolicy", namespaces=namespaces), "IgnoreNew")
        windows_arguments = actions.findtext("Exec/Arguments", namespaces=namespaces)
        match = re.fullmatch(r'"([^"]+)" --config "([^"]+)" --workdir "([^"]+)"', windows_arguments)
        self.assertIsNotNone(match)
        windows_launcher, windows_config, windows_workdir = match.groups()
        self.assertEqual(actions.findtext("Exec/WorkingDirectory", namespaces=namespaces), windows_workdir)
        windows_python = actions.findtext("Exec/Command", namespaces=namespaces)

        self.assertEqual(macos["Label"] + ".plist", macos_name)
        self.assertIs(macos["RunAtLoad"], True)
        self.assertEqual(set(macos), {"Label", "RunAtLoad", "WorkingDirectory", "ProgramArguments"})
        macos_python, macos_launcher, config_flag, macos_config, workdir_flag, macos_workdir = macos["ProgramArguments"]
        self.assertEqual((config_flag, workdir_flag), ("--config", "--workdir"))
        self.assertEqual(macos["WorkingDirectory"], macos_workdir)
        for path_type, interpreter, launcher, config, workdir in [
            (PureWindowsPath, windows_python, windows_launcher, windows_config, windows_workdir),
            (PurePosixPath, macos_python, macos_launcher, macos_config, macos_workdir),
        ]:
            with self.subTest(platform=path_type.__name__):
                self.assertTrue(all(path_type(value).is_absolute() for value in (interpreter, launcher, config, workdir)))
                self.assertIn(path_type(interpreter).name, {"python", "python.exe"})
                self.assertEqual(path_type(launcher).name, "run.py")
                self.assertEqual(path_type(config).name, "config.json")
                self.assertFalse(path_type(interpreter).is_relative_to(workdir))
                self.assertFalse(path_type(launcher).is_relative_to(workdir))
                self.assertFalse(path_type(config).is_relative_to(workdir))

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
        self.assertIn("--config-env", result.stdout)

    def test_temporary_launch_agent_uses_only_the_explicit_config_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag macos fixture ") as directory:
            root = Path(directory).resolve()
            label = f"com.remote-agent.gateway.acceptance-{'a' * 32}"
            executable, launcher = root / "agent env" / "bin/python", root / "bundle" / "run.py"
            config, workdir = root / "state" / "config.json", root / "isolated workspace"
            template = MACOS_LAUNCH_AGENT_TEMPLATE.encode()
            rendered = plistlib.loads(render_temporary_launch_agent(template, label, executable, launcher, config, workdir))
            self.assertEqual(rendered, {
                "Label": label, "RunAtLoad": True, "WorkingDirectory": str(workdir),
                "ProgramArguments": [str(executable), str(launcher), "--config", str(config), "--workdir", str(workdir)],
            })
            with self.assertRaises(ValueError):
                render_temporary_launch_agent(template, "com.remote-agent.gateway", executable, launcher, config, workdir)
            with self.assertRaises(ValueError):
                render_temporary_launch_agent(template, label, executable, launcher, workdir / "config.json", workdir)
            modified_template = plistlib.loads(template)
            modified_template["KeepAlive"] = True
            with self.assertRaises(ValueError):
                render_temporary_launch_agent(plistlib.dumps(modified_template), label, executable, launcher, config, workdir)


if __name__ == "__main__":
    unittest.main()
