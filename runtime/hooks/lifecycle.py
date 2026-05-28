from __future__ import annotations

from hooks.identity_signing import sign_action


VALID_EVENTS = {"start", "stop"}


def lifecycle_event(agent: str, event: str) -> str:
    if event not in VALID_EVENTS:
        raise ValueError(f"Unsupported lifecycle event: {event}")
    return sign_action(agent, f"Lifecycle event: {event}.")
