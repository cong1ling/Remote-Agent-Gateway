from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProfileHandler(ABC):
    profile_id: str

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

