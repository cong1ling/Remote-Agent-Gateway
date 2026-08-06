from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    host: str
    port: int
    public_url: str
    data_dir: Path
    database_path: Path
    session_days: int
    enrollment_minutes: int
    max_command_timeout: int
    secure_cookies: bool

    @classmethod
    def from_env(cls) -> "Settings":
        public_url = os.getenv("GATEWAY_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
        data_dir = Path(os.getenv("GATEWAY_DATA_DIR", "data")).resolve()
        database_path = Path(os.getenv("GATEWAY_DATABASE", str(data_dir / "gateway.db"))).resolve()
        parsed = urlparse(public_url)
        return cls(
            host=os.getenv("GATEWAY_HOST", "127.0.0.1"),
            port=int(os.getenv("GATEWAY_PORT", "8000")),
            public_url=public_url,
            data_dir=data_dir,
            database_path=database_path,
            session_days=int(os.getenv("GATEWAY_SESSION_DAYS", "30")),
            enrollment_minutes=int(os.getenv("GATEWAY_ENROLLMENT_MINUTES", "30")),
            max_command_timeout=int(os.getenv("GATEWAY_MAX_COMMAND_TIMEOUT", "3600")),
            secure_cookies=_env_bool("GATEWAY_SECURE_COOKIES", parsed.scheme == "https"),
        )

    @property
    def websocket_url(self) -> str:
        parsed = urlparse(self.public_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/ws/agent"

    @property
    def bundles_dir(self) -> Path:
        return self.data_dir / "bundles"
