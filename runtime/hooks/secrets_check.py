from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hooks.identity_signing import sign_action


# Named patterns. Each captures a distinct credential shape so block reasons
# can name the kind of secret without echoing the value.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[posur]_[A-Za-z0-9]{30,}\b")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)\baws(.{0,20})?(secret|access)?(.{0,20})?key[\"' :=]+([A-Za-z0-9/+=]{40})\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("asana_pat", re.compile(r"\b[12]/\d{10,}:[A-Fa-f0-9]{30,}\b")),
    ("supabase_service_role", re.compile(r"\bsbp_[A-Za-z0-9]{32,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("ssh_private_key", re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|bearer)\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9_/+\-=\.]{12,})[\"']?"
        ),
    ),
)


@dataclass(frozen=True)
class SecretCheckResult:
    allowed: bool
    reason: str = "allowed"
    matches: tuple[str, ...] = field(default_factory=tuple)


# Hook-emitted audit trail. Per the M4 architecture decision (Asana comment
# 1215434597282433): runtime audit logs live under `runtime/memory/audit/`,
# separate from `runtime/memory/decisions/` which holds curated, human-authored
# ADR-style receipts. The audit/ directory is gitignored and regenerated per
# session; nothing here writes into decisions/ anymore.
#
# Env var resolution prefers ORCHESTRA_AUDIT_DIR. ORCHESTRA_DECISIONS_DIR is a
# DEPRECATED alias kept for one compatibility window: it is honored only when
# ORCHESTRA_AUDIT_DIR is unset. Remove the legacy fallback once no caller sets
# it.
def _audit_dir() -> Path:
    base = (
        os.environ.get("ORCHESTRA_AUDIT_DIR")
        or os.environ.get("ORCHESTRA_DECISIONS_DIR")  # deprecated alias
        or "memory/audit"
    )
    path = Path(base)
    if not path.is_absolute():
        runtime_root = Path(__file__).resolve().parent.parent
        path = runtime_root / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log_block(actor: str, kinds: tuple[str, ...]) -> None:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    target = _audit_dir() / f"{day}.md"
    entry = sign_action(
        actor,
        f"Secrets-check block: refused to pass payload containing {', '.join(kinds)}. Payload redacted.",
    )
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"{entry}\n")


def redact(payload: str) -> str:
    redacted = payload
    for label, pattern in SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted


def find_secrets(payload: str) -> tuple[str, ...]:
    if not payload:
        return ()
    hits: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(payload):
            hits.append(label)
    # de-duplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for label in hits:
        if label not in seen:
            seen.add(label)
            unique.append(label)
    return tuple(unique)


def check_for_secrets(payload: str, actor: str = "Atlas") -> SecretCheckResult:
    matches = find_secrets(payload or "")
    if not matches:
        return SecretCheckResult(allowed=True)
    _log_block(actor, matches)
    return SecretCheckResult(
        allowed=False,
        reason=f"Refused payload: contains {', '.join(matches)}. Payload not logged.",
        matches=matches,
    )
