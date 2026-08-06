from __future__ import annotations

import json
import os
import re
import uuid
import zipfile
from pathlib import Path

import remote_agent

from .profiles import PROFILE_REGISTRY, validate_profiles


class AgentBuildError(Exception):
    pass


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
            "core_version": 1,
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
            "3. Later restarts only need: python run.py\n"
        )

        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(f"{archive_root}/run.py", launcher)
                archive.writestr(f"{archive_root}/requirements.txt", "websockets>=14,<18\n")
                archive.writestr(f"{archive_root}/README.txt", readme)
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
                for profile_id in selected:
                    source_name = PROFILE_REGISTRY[profile_id]["implementations"][self.runtime]["source"]
                    self._write_file(
                        archive,
                        self.package_root / "profiles" / source_name,
                        archive_root,
                    )
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


def new_bundle_id() -> str:
    return str(uuid.uuid4())
