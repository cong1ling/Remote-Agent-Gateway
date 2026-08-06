from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import FatalAgentError


class AgentConfig:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        with self.path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise FatalAgentError(f"设备配置格式无效: {self.path}")
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

