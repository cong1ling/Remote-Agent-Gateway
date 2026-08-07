from __future__ import annotations

import json
import os
import re
import uuid
import zipfile
from pathlib import Path

import remote_agent
from remote_agent.core.version import AGENT_VERSION, CORE_VERSION

from .profiles import PROFILE_REGISTRY, validate_profiles


class AgentBuildError(Exception):
    pass


GITHUB_ACTIONS_TEMPLATE = """name: Remote Agent Session

on:
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: remote-agent-${{ github.repository }}
  cancel-in-progress: true

jobs:
  remote-agent:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    env:
      REMOTE_AGENT_DIR: __AGENT_DIR__
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Agent dependency
        run: python -m pip install -r "$REMOTE_AGENT_DIR/requirements.txt"

      - name: Keep remote Agent online
        env:
          REMOTE_AGENT_CONFIG_B64: ${{ secrets.REMOTE_AGENT_CONFIG_B64 }}
        run: >-
          python "$REMOTE_AGENT_DIR/run.py"
          --config-env REMOTE_AGENT_CONFIG_B64
          --workdir "$GITHUB_WORKSPACE"
"""


class PythonAgentBuilder:
    runtime = "python"

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.package_root = Path(remote_agent.__file__).resolve().parent

    @staticmethod
    def _safe_slug(name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
        return slug[:48] or "remote-agent"

    def build(
        self,
        *,
        bundle_id: str,
        name: str,
        description: str,
        profiles: list[str],
    ) -> tuple[Path, str]:
        selected = validate_profiles(profiles)
        for profile_id in selected:
            if self.runtime not in PROFILE_REGISTRY[profile_id]["implementations"]:
                raise AgentBuildError(f"Profile {profile_id} 没有 Python 实现")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"remote-agent-{self._safe_slug(name)}-{bundle_id[:8]}.zip"
        destination = self.output_dir / filename
        temporary = destination.with_suffix(".zip.tmp")
        archive_root = filename.removesuffix(".zip")
        manifest = {
            "runtime": self.runtime,
            "agent_version": AGENT_VERSION,
            "core_version": CORE_VERSION,
            "build_id": bundle_id,
            "profiles": selected,
            "agent": {"name": name, "description": description},
        }
        launcher = (
            "from pathlib import Path\n"
            "from remote_agent.cli import main\n\n"
            "if __name__ == '__main__':\n"
            "    main(default_config=Path(__file__).resolve().parent / '.agent' / 'config.json')\n"
        )
        readme = (
            "Remote Agent Gateway - Python Agent\n"
            "===================================\n\n"
            "1. Install dependency: python -m pip install -r requirements.txt\n"
            "2. Run the one-time enrollment command shown by the gateway.\n"
            "3. Later restarts only need: python run.py\n\n"
            "Device credentials default to .agent/config.json. The Agent checks that this\n"
            "location is writable before consuming the enrollment token. For services,\n"
            "read-only directories, or containers, use --config with a persistent path.\n"
            "\n"
            "GitHub Actions / CI\n"
            "-------------------\n"
            "1. Register this dedicated Agent once with the normal enrollment command.\n"
            "2. Stop it, then encode .agent/config.json as a single-line Base64 value:\n"
            "   python -c \"import base64,pathlib; print(base64.b64encode(pathlib.Path('.agent/config.json').read_bytes()).decode())\"\n"
            "3. Save the output as the repository secret REMOTE_AGENT_CONFIG_B64.\n"
            "4. Keep this Agent directory at the repository root and copy\n"
            "   github-actions.yml.example to .github/workflows/remote-agent.yml.\n"
            "5. Start 'Remote Agent Session' manually from the Actions page.\n"
            "\n"
            "The CI secret is consumed and removed from the Agent process environment\n"
            "before shell commands can run. Never commit .agent/config.json, and do not\n"
            "run the same device credential in two places at the same time.\n"
        )
        github_actions = GITHUB_ACTIONS_TEMPLATE.replace("__AGENT_DIR__", archive_root)

        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(f"{archive_root}/run.py", launcher)
                archive.writestr(f"{archive_root}/requirements.txt", "websockets>=14,<18\n")
                archive.writestr(f"{archive_root}/README.txt", readme)
                archive.writestr(
                    f"{archive_root}/.gitignore",
                    ".agent/\n__pycache__/\n*.py[cod]\n",
                )
                archive.writestr(
                    f"{archive_root}/github-actions.yml.example",
                    github_actions,
                )
                archive.writestr(
                    f"{archive_root}/remote_agent/manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                )
                self._write_file(archive, self.package_root / "__init__.py", archive_root)
                self._write_file(archive, self.package_root / "__main__.py", archive_root)
                self._write_file(archive, self.package_root / "cli.py", archive_root)
                for source in sorted((self.package_root / "core").glob("*.py")):
                    self._write_file(archive, source, archive_root)
                self._write_file(archive, self.package_root / "profiles" / "__init__.py", archive_root)
                self._write_file(archive, self.package_root / "profiles" / "base.py", archive_root)
                self._write_file(archive, self.package_root / "profiles" / "registry.py", archive_root)
                self._write_tree(archive, self.package_root / "profiles" / "common", archive_root)
                for profile_id in selected:
                    source_name = PROFILE_REGISTRY[profile_id]["implementations"][self.runtime]["source"]
                    source = self.package_root / "profiles" / source_name
                    if source.is_dir():
                        self._write_tree(archive, source, archive_root)
                    else:
                        self._write_file(archive, source, archive_root)
            os.replace(temporary, destination)
        except OSError as exc:
            if temporary.exists():
                temporary.unlink()
            raise AgentBuildError(f"生成 Agent 包失败: {exc}") from exc
        return destination, filename

    def _write_file(self, archive: zipfile.ZipFile, source: Path, archive_root: str) -> None:
        if not source.is_file():
            raise AgentBuildError(f"Agent 源文件不存在: {source.name}")
        relative = source.relative_to(self.package_root)
        archive.write(source, f"{archive_root}/remote_agent/{relative.as_posix()}")

    def _write_tree(self, archive: zipfile.ZipFile, source: Path, archive_root: str) -> None:
        if not source.is_dir():
            raise AgentBuildError(f"Agent 源目录不存在: {source.name}")
        for child in sorted(source.rglob("*")):
            if child.is_file():
                self._write_file(archive, child, archive_root)


def new_bundle_id() -> str:
    return str(uuid.uuid4())
