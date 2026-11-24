from __future__ import annotations

import json
import os
import re
import uuid
import zipfile
from pathlib import Path

import remote_agent
from remote_agent.core.version import AGENT_VERSION, CORE_VERSION
from remote_agent.profiles.common.managed_contracts import BUILD_PROFILE, SERVICE_PROFILE

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

SYSTEMD_TEMPLATE = """[Unit]
Description=Remote Agent Gateway device agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=remote-agent
Group=remote-agent
WorkingDirectory=/srv/remote-agent-work
ExecStart=/opt/remote-agent/.venv/bin/python /opt/remote-agent/run.py --config /var/lib/remote-agent/config.json --workdir /srv/remote-agent-work
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=/var/lib/remote-agent /srv/remote-agent-work

[Install]
WantedBy=multi-user.target
"""

WINDOWS_TASK_TEMPLATE = r"""<?xml version="1.0"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Remote Agent Gateway example for the current standard user</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
      <UserId>REPLACE_WITH_CURRENT_USER_SID</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="CurrentUser">
      <UserId>REPLACE_WITH_CURRENT_USER_SID</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="CurrentUser">
    <Exec>
      <Command>C:\Users\REPLACE_WITH_ACCOUNT\Remote Agent\agent\.venv\Scripts\python.exe</Command>
      <Arguments>"C:\Users\REPLACE_WITH_ACCOUNT\Remote Agent\agent\run.py" --config "C:\Users\REPLACE_WITH_ACCOUNT\Remote Agent\state\config.json" --workdir "C:\Users\REPLACE_WITH_ACCOUNT\Remote Agent\workspace"</Arguments>
      <WorkingDirectory>C:\Users\REPLACE_WITH_ACCOUNT\Remote Agent\workspace</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

MACOS_LAUNCH_AGENT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.remote-agent.gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/REPLACE_WITH_ACCOUNT/Remote Agent/agent/.venv/bin/python</string>
    <string>/Users/REPLACE_WITH_ACCOUNT/Remote Agent/agent/run.py</string>
    <string>--config</string>
    <string>/Users/REPLACE_WITH_ACCOUNT/Remote Agent/state/config.json</string>
    <string>--workdir</string>
    <string>/Users/REPLACE_WITH_ACCOUNT/Remote Agent/workspace</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/REPLACE_WITH_ACCOUNT/Remote Agent/workspace</string>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
"""

AGENT_DOCKERFILE_TEMPLATE = """FROM python:3.12-slim

RUN groupadd --gid 10001 remote-agent \\
    && useradd --uid 10001 --gid remote-agent --create-home remote-agent
WORKDIR /opt/remote-agent
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY --chown=remote-agent:remote-agent . ./
USER 10001:10001
ENTRYPOINT [\"python\", \"run.py\"]
CMD [\"--config\", \"/var/lib/remote-agent/config.json\", \"--workdir\", \"/workspace\"]
"""

AGENT_COMPOSE_TEMPLATE = """services:
  remote-agent:
    build:
      context: .
      dockerfile: Dockerfile.agent.example
    restart: unless-stopped
    user: \"10001:10001\"
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    tmpfs:
      - /tmp:size=64m,mode=1777
    volumes:
      - ./state:/var/lib/remote-agent
      - ./workspace:/workspace
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
            "\n"
            "Least-privilege production deployment\n"
            "-------------------------------------\n"
            "shell.v1 always inherits the operating-system account that starts this\n"
            "process. --workdir limits workspace.v1 paths but is NOT a shell sandbox.\n"
            "Never run a production shell Agent as root or Administrator.\n"
            "\n"
            "Host startup templates are examples, not installers. Review all paths and\n"
            "replace sample values with absolute paths for Python, run.py, config, and\n"
            "the workspace. Keep Python, the launcher, and private config outside the workspace.\n"
            "As the account that will run the Agent, enroll once in the foreground using\n"
            "the gateway enrollment command with the template's --config and --workdir.\n"
            "After the config is saved, stop the foreground Agent before enabling startup.\n"
            "Later starts use the existing local config only. Never put an enrollment\n"
            "token, account password, or the contents of config.json into a startup template.\n"
            "\n"
            "Linux: create an unprivileged remote-agent account with no sudo access,\n"
            "make /opt/remote-agent read-only to it, and grant it access only to\n"
            "/var/lib/remote-agent plus the intended workspace. Adapt and install\n"
            "remote-agent.service.example after the first enrollment.\n"
            "Example preparation commands (run as root):\n"
            "   useradd --system --home /var/lib/remote-agent --shell /usr/sbin/nologin remote-agent\n"
            "   install -d -o remote-agent -g remote-agent -m 0700 /var/lib/remote-agent\n"
            "   install -d -o remote-agent -g remote-agent -m 0750 /srv/remote-agent-work\n"
            "   chown -R root:root /opt/remote-agent && chmod -R go-w /opt/remote-agent\n"
            "Run the enrollment command once with 'sudo -u remote-agent' and the\n"
            "service template's --config/--workdir paths, then enable the service.\n"
            "\n"
            "Windows (current Standard local user): review remote-agent.task.xml.example.\n"
            "Do not import as an administrator. Replace both UserId values with that user's\n"
            "SID (shown by whoami /user). Keep the quotes around paths in Arguments.\n"
            "After foreground enrollment, save a reviewed .xml copy\n"
            "and import it in Task Scheduler if local policy permits. Keep 'Run only when\n"
            "user is logged on' and leave 'Run with highest privileges' unchecked.\n"
            "\n"
            "macOS: review com.remote-agent.gateway.plist.example as the current standard\n"
            "user. ProgramArguments are separate values, not a shell command. After\n"
            "foreground enrollment, save the reviewed copy as a .plist matching its\n"
            "Label under ~/Library/LaunchAgents to start it at that user's login.\n"
            "Do not use sudo or install it as a system LaunchDaemon.\n"
            "\n"
            "Container: use Dockerfile.agent.example and compose.agent.yml.example.\n"
            "They run as UID 10001 with a read-only root filesystem, all capabilities\n"
            "dropped, and only state/workspace mounted writable. Before first start,\n"
            "create state/workspace and make them writable by UID/GID 10001.\n"
            "Enroll once with: docker compose -f compose.agent.yml.example run --rm\n"
            "   remote-agent --config /var/lib/remote-agent/config.json --workdir /workspace\n"
            "   --gateway wss://gateway.example/ws/agent --token <enrollment-token>\n"
        )
        if SERVICE_PROFILE in selected:
            readme += (
                "\nRegistered service profile\n"
                "Pass --service-config with the device-local supervisor config outside --workdir,\n"
                "both at enrollment and on later starts. Use a dedicated unprivileged account.\n"
                "This profile accepts managed plans only, not direct MCP or shell requests.\n"
            )
        if BUILD_PROFILE in selected:
            readme += (
                "\nTrusted Python build verification profile\n"
                "Pass --build-archive with the absolute path to your trusted, pre-provisioned source ZIP\n"
                "outside --workdir at enrollment and on later starts. The runner is not an OS sandbox.\n"
                "Run it using a dedicated low-privilege, network-isolated account or container.\n"
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
                    f"{archive_root}/remote-agent.service.example",
                    SYSTEMD_TEMPLATE,
                )
                archive.writestr(
                    f"{archive_root}/remote-agent.task.xml.example",
                    WINDOWS_TASK_TEMPLATE,
                )
                archive.writestr(
                    f"{archive_root}/com.remote-agent.gateway.plist.example",
                    MACOS_LAUNCH_AGENT_TEMPLATE,
                )
                archive.writestr(
                    f"{archive_root}/Dockerfile.agent.example",
                    AGENT_DOCKERFILE_TEMPLATE,
                )
                archive.writestr(
                    f"{archive_root}/compose.agent.yml.example",
                    AGENT_COMPOSE_TEMPLATE,
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
                self._write_tree(archive, self.package_root / "runtimes", archive_root)
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
            if child.is_file() and child.suffix == ".py":
                self._write_file(archive, child, archive_root)


def new_bundle_id() -> str:
    return str(uuid.uuid4())
