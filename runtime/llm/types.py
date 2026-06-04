"""Shared types for the M4 LLM provider + message-passing layer.

Originally minimal in task 4.1. Task 4.4 (message-passing protocol) extends
this with the structured `MessageEnvelope` (`id`, `session_id`, `sender`,
`target`, `message_type`, `content`, `action_surface`, `parent_id`,
`metadata`, `created_at`) per the M4 architecture scope (comment
1215386979487630). Task 4.6 (SQLite persistence) will serialize envelopes;
nothing here owns persistence itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


# Message types for the inter-agent envelope (task 4.4).
MESSAGE_TYPE_DIRECTIVE = "directive"
MESSAGE_TYPE_RESPONSE = "response"
MESSAGE_TYPE_BLOCKER = "blocker"
MESSAGE_TYPE_AGENT_MESSAGE = "agent_message"

VALID_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        MESSAGE_TYPE_DIRECTIVE,
        MESSAGE_TYPE_RESPONSE,
        MESSAGE_TYPE_BLOCKER,
        MESSAGE_TYPE_AGENT_MESSAGE,
    }
)


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


@dataclass(frozen=True)
class MessageEnvelope:
    """Structured envelope for inter-agent messages (M4 task 4.4).

    Fields per the M4 architecture scope (comment 1215386979487630,
    architecture decision 4):

    - `id`: UUID4 string, runtime-generated per envelope.
    - `session_id`: UUID4 string. Threaded across related sends by the
      caller (4.5 supervisor or CLI). Auto-generated if not provided.
    - `sender`: canonical agent name (Atlas / Cody / Scribe / Scout).
    - `target`: canonical agent name (Atlas / Cody / Scribe / Scout).
    - `message_type`: one of the `MESSAGE_TYPE_*` constants.
    - `content`: the human-readable text. For responses the runtime
      wraps this in `[<target> · <real-UTC>] ...` via `sign_action`;
      the inner content may itself contain a model-emitted placeholder
      signature, which stays narrative under the outer runtime sig.
    - `action_surface`: one of `safe` / `guarded` / `human-approved-only`
      from `hooks/approval_gates.py`. Unknown surfaces fail closed.
    - `parent_id`: nullable UUID of the parent envelope (used to thread
      responses back to their directives).
    - `metadata`: free-form dict for provider model id, finish reason,
      usage stats, redacted raw response, etc. Bounded; no secrets.
    - `created_at`: real UTC `datetime` stamped by the runtime at envelope
      creation. This is the deferred timestamp-truth decision from task
      4.1/4.2 — model-emitted timestamps remain narrative content.
    """

    id: str
    session_id: str
    sender: str
    target: str
    message_type: str
    content: str
    action_surface: str
    parent_id: str | None
    metadata: dict[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("MessageEnvelope.id must be a non-empty string.")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("MessageEnvelope.session_id must be a non-empty string.")
        if not isinstance(self.sender, str) or not self.sender:
            raise ValueError("MessageEnvelope.sender must be a non-empty string.")
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("MessageEnvelope.target must be a non-empty string.")
        if not isinstance(self.message_type, str) or not self.message_type:
            raise ValueError("MessageEnvelope.message_type must be a non-empty string.")
        if not isinstance(self.content, str):
            raise TypeError("MessageEnvelope.content must be a string.")
        if not isinstance(self.action_surface, str) or not self.action_surface:
            raise ValueError("MessageEnvelope.action_surface must be a non-empty string.")
        if self.parent_id is not None and (
            not isinstance(self.parent_id, str) or not self.parent_id
        ):
            raise ValueError("MessageEnvelope.parent_id must be a non-empty string or None.")
        if not isinstance(self.metadata, dict):
            raise TypeError("MessageEnvelope.metadata must be a dict.")
        if not isinstance(self.created_at, datetime):
            raise TypeError("MessageEnvelope.created_at must be a datetime.")
