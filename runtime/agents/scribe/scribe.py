from __future__ import annotations

from hooks.identity_signing import sign_action


class ScribeAgent:
    name = "Scribe"

    def record_stub(self, message: str) -> str:
        return sign_action(self.name, message)
