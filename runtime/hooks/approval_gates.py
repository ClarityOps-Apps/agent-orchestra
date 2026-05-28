from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from hooks.identity_signing import sign_action


SAFE = "safe"
GUARDED = "guarded"
HUMAN_APPROVED_ONLY = "human-approved-only"

VALID_SURFACES = {SAFE, GUARDED, HUMAN_APPROVED_ONLY}


HUMAN_APPROVED_ACTIONS = frozenset(
    {
        "production_deploy",
        "jloop_database_write",
        "customer_database_write",
        "secret_rotation",
        "secret_read",
        "external_send",
        "destructive_command",
        "irreversible_data_change",
        "force_push_main",
        "delete_branch",
        "merge_to_main",
        "add_mcp_server",
    }
)

HUMAN_APPROVED_KEYWORDS = (
    "rm -rf",
    "drop table",
    "drop database",
    "truncate ",
    "force-push",
    "force push",
    "--force",
    "supabase db reset",
)


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str = "allowed"
    decision: str = "allow"  # 'allow' | 'block' | 'pending-human-approval'
    surface: str = SAFE


@dataclass(frozen=True)
class ApprovalRequest:
    action: str
    actor: str
    target: str
    risk: str
    rollback: str
    surface: str = HUMAN_APPROVED_ONLY
    extras: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def as_packet(self) -> str:
        lines = [
            "APPROVAL REQUEST — Human-approved-only action.",
            f"  Action:   {self.action}",
            f"  Actor:    {self.actor}",
            f"  Target:   {self.target}",
            f"  Risk:     {self.risk}",
            f"  Rollback: {self.rollback}",
        ]
        for key, value in self.extras:
            lines.append(f"  {key}: {value}")
        return " | ".join(lines)


def _decisions_dir() -> Path:
    base = os.environ.get("ORCHESTRA_DECISIONS_DIR", "memory/decisions")
    path = Path(base)
    if not path.is_absolute():
        runtime_root = Path(__file__).resolve().parent.parent
        path = runtime_root / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log_decision(entry: str) -> None:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    target = _decisions_dir() / f"{day}.md"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"{entry}\n")


def _looks_human_approved(action: str) -> bool:
    if action in HUMAN_APPROVED_ACTIONS:
        return True
    lowered = action.lower()
    return any(keyword in lowered for keyword in HUMAN_APPROVED_KEYWORDS)


def classify_surface(action: str, declared_surface: str | None = None) -> str:
    if declared_surface and declared_surface not in VALID_SURFACES:
        raise ValueError(f"Unknown action surface: {declared_surface}")
    if _looks_human_approved(action):
        return HUMAN_APPROVED_ONLY
    if declared_surface:
        return declared_surface
    return SAFE


def check_approval_gate(
    action: str,
    surface: str = SAFE,
    actor: str = "Atlas",
    target: str = "n/a",
    risk: str = "n/a",
    rollback: str = "n/a",
) -> GateResult:
    resolved = classify_surface(action, surface)

    if resolved == HUMAN_APPROVED_ONLY:
        request = ApprovalRequest(
            action=action,
            actor=actor,
            target=target,
            risk=risk,
            rollback=rollback,
        )
        entry = sign_action(actor, f"Blocked pending Garrett approval. {request.as_packet()}")
        _log_decision(entry)
        return GateResult(
            allowed=False,
            reason=f"Human approval required before `{action}`.",
            decision="pending-human-approval",
            surface=HUMAN_APPROVED_ONLY,
        )

    if resolved == GUARDED:
        entry = sign_action(actor, f"Guarded action allowed: {action} (target={target}).")
        _log_decision(entry)
        return GateResult(
            allowed=True,
            reason="guarded-allowed",
            decision="allow",
            surface=GUARDED,
        )

    return GateResult(allowed=True, reason="safe-allowed", decision="allow", surface=SAFE)


def record_human_decision(
    action: str,
    actor: str,
    decision: str,
    rationale: str,
) -> str:
    if decision not in {"approve", "reject"}:
        raise ValueError("decision must be 'approve' or 'reject'")
    entry = sign_action(
        "Atlas",
        f"Human decision recorded for `{action}` by {actor}: {decision}. Rationale: {rationale}",
    )
    _log_decision(entry)
    return entry


def list_human_approved_actions() -> Iterable[str]:
    return sorted(HUMAN_APPROVED_ACTIONS)
