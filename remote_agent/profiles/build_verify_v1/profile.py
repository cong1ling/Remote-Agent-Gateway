from __future__ import annotations

from remote_agent.profiles.base import ProfileContext, ProfileHandler

from .actions.source import SourceAction
from .actions.verify import VerifyAction


class BuildVerifyV1Profile(ProfileHandler):
    profile_id = "build_verify.v1"
    plan_only = True

    def __init__(self, context: ProfileContext):
        super().__init__(
            context,
            {
                "source": SourceAction(context),
                "verify": VerifyAction(context),
            },
        )

    def runtime_metadata(self) -> dict[str, str]:
        return {"backend": "python-unittest-zip.v1"}
