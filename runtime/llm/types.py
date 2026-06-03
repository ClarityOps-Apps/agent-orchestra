"""Shared types for the M4 LLM provider layer.

Intentionally minimal in task 4.1. Task 4.4 (message-passing protocol) extends
this with a structured envelope (`id`, `session_id`, `sender`, `target`,
`message_type`, `content`, `action_surface`, `parent_id`, `metadata`,
`created_at`) per the M4 architecture scope (comment 1215386979487630).
The shapes here cover only what the OpenAI provider ping needs in 4.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


@dataclass(frozen=True)
class Message:
    """A single chat-style message handed to a provider."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in VALID_ROLES:
            raise ValueError(
                f"Unknown message role: {self.role!r}. Expected one of {sorted(VALID_ROLES)}."
            )
        if not isinstance(self.content, str):
            raise TypeError("Message.content must be a string.")


@dataclass(frozen=True)
class AgentResponse:
    """The response envelope returned by a provider's `send()`.

    `content` is the text the agent emitted; the runtime is expected to
    validate that it carries a signature when it reaches the agent-visible
    output layer (the identity-signing hook enforces this).
    """

    agent: str
    model: str
    content: str
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
