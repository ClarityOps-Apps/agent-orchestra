from __future__ import annotations

from hooks.identity_signing import sign_action


class CodyAgent:
    name = "Cody"

    def run_stub_task(self, objective: str) -> str:
        return sign_action(self.name, f"{objective} Result: ok.")
