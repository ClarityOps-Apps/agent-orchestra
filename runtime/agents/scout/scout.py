from __future__ import annotations

from hooks.identity_signing import sign_action


class ScoutAgent:
    name = "Scout"

    def smoke_stub(self, target: str) -> str:
        return sign_action(self.name, f"Thin smoke stub checked {target}. Verdict: pass.")
