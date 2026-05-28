from __future__ import annotations

import re
from datetime import UTC, datetime


AGENT_NAMES = (
    "Atlas",
    "Cody",
    "Scribe",
    "Scout",
    "Sentinel",
    "UX Reviewer",
    "Release Captain",
)

SIGNATURE_RE = re.compile(
    r"^\[(?P<agent>Atlas|Cody|Scribe|Scout|Sentinel|UX Reviewer|Release Captain)"
    r" · (?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z)\] .+"
)


class SignatureError(ValueError):
    pass


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")


def sign_action(agent: str, message: str) -> str:
    if agent not in AGENT_NAMES:
        raise SignatureError(f"Unknown agent name: {agent}")
    clean_message = " ".join(message.split())
    if not clean_message:
        raise SignatureError("Cannot sign an empty action message.")
    return f"[{agent} · {utc_timestamp()}] {clean_message}"


def enforce_signed(entry: str) -> str:
    if not SIGNATURE_RE.match(entry):
        raise SignatureError(
            "Unsigned action blocked. Expected `[Agent · YYYY-MM-DDTHH:MMZ] message`."
        )
    return entry
