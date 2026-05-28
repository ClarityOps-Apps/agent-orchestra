from __future__ import annotations

from hooks.identity_signing import sign_action


class AtlasAgent:
    name = "Atlas"

    def receive_directive(self, directive: str) -> str:
        return sign_action(
            self.name,
            f'Received Garrett directive: "{directive}". Delegating Day 1 no-op stub to Cody.',
        )
