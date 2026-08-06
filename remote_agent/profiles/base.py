from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProfileHandler(ABC):
    profile_id: str

    @abstractmethod
    def runtime_metadata(self) -> dict[str, Any]:
        """Describe the concrete runtime selected for this Profile."""

    def declaration(self) -> dict[str, Any]:
        return {"id": self.profile_id, "runtime": self.runtime_metadata()}

    @abstractmethod
    async def execute(
        self,
        action: str,
        request_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute one action defined by this standard Profile."""

    async def cancel(self, request_id: str) -> None:
        """Cancel an active request when the Profile supports cancellation."""
