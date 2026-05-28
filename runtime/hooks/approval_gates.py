from __future__ import annotations

from dataclasses import dataclass


HUMAN_APPROVED_ONLY = {
    "production_deploy",
    "jloop_database_write",
    "secret_rotation",
    "external_send",
    "destructive_command",
    "irreversible_data_change",
}


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str = "allowed"


def check_approval_gate(action: str, surface: str = "safe") -> GateResult:
    if surface == "human-approved-only" or action in HUMAN_APPROVED_ONLY:
        return GateResult(False, f"Human approval required before `{action}`.")
    return GateResult(True)
