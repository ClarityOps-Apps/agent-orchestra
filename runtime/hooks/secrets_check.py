from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SecretCheckResult:
    allowed: bool
    reason: str = "allowed"


def check_for_secrets(payload: str) -> SecretCheckResult:
    # Day 1 stub. Real pattern checks land on Day 2.
    return SecretCheckResult(True)
