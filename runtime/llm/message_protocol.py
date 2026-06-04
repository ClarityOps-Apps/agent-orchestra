"""Message-passing protocol for Agent Orchestra (M4 task 4.4).

Implements `send_message(sender, target, message, ...)`, the first real
inter-agent routing layer. Routes target construction through the 4.3
factory (`create_agent`), wraps the provider call with the hooks layer
(secrets check → approval gates → identity signing), and returns a
structured `MessageEnvelope` (the response).

This module does NOT implement:
- the supervisor loop (task 4.5)
- SQLite session persistence (task 4.6)
- MCP tool access (task 4.7)
- REST API endpoints (task 4.11)

Per the M4 architecture scope (Asana comment 1215386979487630):
- Provider adapter pattern preserved (sends via `Agent.provider.send`).
- Runtime-stamped `created_at` and `id` are authoritative.
- Hook order is explicit and fails closed.
- `human-approved-only` action surfaces return a signed blocker without
  calling any provider.

Run from `runtime/`:

  cd runtime
  uv run python -m llm.message_protocol --dry-run
  uv run python -m llm.message_protocol --send Atlas Cody "hello cody"

Required env (when --send is used): the 4.1 / 4.2 keys for the target
agent's provider — e.g. `OPENAI_API_KEY` + `ATLAS_MODEL` for an Atlas
target, `ANTHROPIC_API_KEY` + `<NAME>_MODEL` for a Cody/Scribe/Scout
target. Sender does NOT need credentials because its provider is not
called by 4.4 (only the target's is).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from config import MissingEnvError, load_env_file
from hooks.approval_gates import (
    GUARDED,
    HUMAN_APPROVED_ONLY,
    SAFE,
    VALID_SURFACES,
    check_approval_gate,
)
from hooks.identity_signing import sign_action
from hooks.secrets_check import check_for_secrets, find_secrets, redact
from llm.agent_factory import UnknownAgentError, create_agent, get_spec
from llm.types import (
    MESSAGE_TYPE_AGENT_MESSAGE,
    MESSAGE_TYPE_BLOCKER,
    MESSAGE_TYPE_RESPONSE,
    Message,
    MessageEnvelope,
    VALID_MESSAGE_TYPES,
)


# Bound the metadata raw-response capture so a chatty model can't blow up the
# envelope dict. 4.6 persistence will define its own column width.
MAX_RAW_CONTENT_METADATA_CHARS = 4000


class ProtocolError(ValueError):
    """Raised when a message-protocol invariant is violated."""


def _new_id() -> str:
    return str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_message_type(message_type: str) -> None:
    if message_type not in VALID_MESSAGE_TYPES:
        raise ProtocolError(
            f"Unknown message_type {message_type!r}. "
            f"Valid: {', '.join(sorted(VALID_MESSAGE_TYPES))}."
        )


def _validate_action_surface(action_surface: str) -> None:
    if action_surface not in VALID_SURFACES:
        raise ProtocolError(
            f"Unknown action_surface {action_surface!r}. "
            f"Valid: {', '.join(sorted(VALID_SURFACES))}."
        )


def _canonicalize_agent(name: str) -> str:
    """Resolve a name through the 4.3 factory; raise on unknown."""
    return get_spec(name).name


def _build_outbound(
    sender: str,
    target: str,
    message: str,
    *,
    session_id: str,
    message_type: str,
    action_surface: str,
    parent_id: str | None,
    metadata: dict[str, Any],
) -> MessageEnvelope:
    return MessageEnvelope(
        id=_new_id(),
        session_id=session_id,
        sender=sender,
        target=target,
        message_type=message_type,
        content=message,
        action_surface=action_surface,
        parent_id=parent_id,
        metadata=dict(metadata),
        created_at=_utc_now(),
    )


def _build_blocker(
    *,
    sender: str,
    target: str,
    session_id: str,
    parent_id: str,
    reason: str,
    extra_metadata: dict[str, Any] | None = None,
) -> MessageEnvelope:
    """Build a signed blocker envelope.

    The blocker conceptually replaces the response that would have come
    from the target, so `sender` of the blocker = the target agent (under
    which it is signed). `target` = the original requester.
    """
    body = sign_action(target, f"Blocked: {reason}")
    meta: dict[str, Any] = {"blocker_reason": reason}
    if extra_metadata:
        meta.update(extra_metadata)
    return MessageEnvelope(
        id=_new_id(),
        session_id=session_id,
        sender=target,
        target=sender,
        message_type=MESSAGE_TYPE_BLOCKER,
        content=body,
        action_surface=SAFE,
        parent_id=parent_id,
        metadata=meta,
        created_at=_utc_now(),
    )


def _build_response(
    *,
    sender: str,
    target: str,
    session_id: str,
    parent_id: str,
    raw_content: str,
    model: str,
    provider_family: str,
    finish_reason: str | None,
    usage: dict[str, Any],
) -> MessageEnvelope:
    """Wrap a provider response in a signed envelope.

    `sender` here is the *target* agent (it's now sending its response back),
    `target` is the original requester. The runtime applies the authoritative
    signature in real UTC; any model-emitted `[Name · placeholder]` prefix in
    `raw_content` becomes narrative under our outer signature.
    """
    trimmed = raw_content.strip()
    if not trimmed:
        trimmed = "(empty response from provider)"
    signed_content = sign_action(sender, trimmed)

    # Defense-in-depth: scan inbound content for secret patterns and store a
    # redacted copy in metadata. We don't block delivery on inbound — the
    # model already produced the content — but we don't propagate raw secrets
    # to the metadata persistence layer either.
    inbound_hits = find_secrets(raw_content)
    redacted_raw = redact(raw_content) if inbound_hits else raw_content
    if len(redacted_raw) > MAX_RAW_CONTENT_METADATA_CHARS:
        redacted_raw = redacted_raw[:MAX_RAW_CONTENT_METADATA_CHARS] + "…[truncated]"

    metadata: dict[str, Any] = {
        "model": model,
        "provider_family": provider_family,
        "raw_content_redacted": redacted_raw,
        "raw_content_inbound_secret_hits": list(inbound_hits),
        "finish_reason": finish_reason,
        "usage": usage,
    }

    return MessageEnvelope(
        id=_new_id(),
        session_id=session_id,
        sender=sender,
        target=target,
        message_type=MESSAGE_TYPE_RESPONSE,
        content=signed_content,
        action_surface=SAFE,
        parent_id=parent_id,
        metadata=metadata,
        created_at=_utc_now(),
    )


def send_message(
    sender: str,
    target: str,
    message: str,
    *,
    session_id: str | None = None,
    message_type: str = MESSAGE_TYPE_AGENT_MESSAGE,
    action_surface: str = SAFE,
    parent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    _skip_provider: bool = False,
) -> MessageEnvelope:
    """Send a message from `sender` to `target` and return the response envelope.

    Hook order (per Atlas's 4.4 directive step 7):
      1. secrets_check on outbound `message` content — fail closed.
      2. approval_gates on `action_surface` and the `message_type`/action —
         `human-approved-only` returns a signed blocker without a provider
         call; `guarded` requires gate allow; `safe` proceeds.
      3. provider call via the 4.3 factory's `create_agent(target).provider.send(...)`.
      4. identity-signing applied to the response content using real UTC
         (`sign_action(target, ...)`). Inner model-emitted signatures become
         narrative content under the outer runtime signature.

    `_skip_provider` is a private dry-run hook: when True the provider call
    is bypassed and a synthetic "skipped (dry-run)" response envelope is
    returned. This lets the CLI's `--dry-run` exercise hook short-circuits
    without spending tokens or requiring credentials.

    Raises `UnknownAgentError` for unknown sender/target names. Raises
    `ProtocolError` for unknown `message_type` or `action_surface`.
    Provider construction may raise `MissingEnvError` from the underlying
    factory if env keys are not present.
    """
    # Validate types early so frozen dataclass constructors don't surface
    # confusing errors.
    if not isinstance(message, str):
        raise ProtocolError("send_message: message must be a string.")
    sender_canonical = _canonicalize_agent(sender)
    target_canonical = _canonicalize_agent(target)
    _validate_message_type(message_type)
    _validate_action_surface(action_surface)

    session_id_final = session_id or _new_id()
    metadata_in = dict(metadata or {})

    outbound = _build_outbound(
        sender=sender_canonical,
        target=target_canonical,
        message=message,
        session_id=session_id_final,
        message_type=message_type,
        action_surface=action_surface,
        parent_id=parent_id,
        metadata=metadata_in,
    )

    # --- Hook 1: secrets check on outbound content ----------------------------
    secrets_result = check_for_secrets(message, actor=sender_canonical)
    if not secrets_result.allowed:
        return _build_blocker(
            sender=sender_canonical,
            target=target_canonical,
            session_id=session_id_final,
            parent_id=outbound.id,
            reason=secrets_result.reason,
            extra_metadata={
                "blocker_phase": "secrets_check",
                "secret_kinds": list(secrets_result.matches),
            },
        )

    # --- Hook 2: approval gate on the action_surface --------------------------
    gate_action = message_type or MESSAGE_TYPE_AGENT_MESSAGE
    gate_result = check_approval_gate(
        action=gate_action,
        surface=action_surface,
        actor=sender_canonical,
        target=f"agent:{target_canonical}",
        risk=f"inter-agent message ({message_type})",
        rollback="n/a — message-only",
    )
    if not gate_result.allowed:
        return _build_blocker(
            sender=sender_canonical,
            target=target_canonical,
            session_id=session_id_final,
            parent_id=outbound.id,
            reason=gate_result.reason,
            extra_metadata={
                "blocker_phase": "approval_gates",
                "decision": gate_result.decision,
                "surface": gate_result.surface,
            },
        )

    # --- Provider call --------------------------------------------------------
    if _skip_provider:
        # Dry-run path: skip the provider but return a synthetic envelope so
        # the caller can assert hook short-circuits without API spend.
        return MessageEnvelope(
            id=_new_id(),
            session_id=session_id_final,
            sender=target_canonical,
            target=sender_canonical,
            message_type=MESSAGE_TYPE_RESPONSE,
            content=sign_action(
                target_canonical, "(dry-run: provider call skipped)"
            ),
            action_surface=SAFE,
            parent_id=outbound.id,
            metadata={"dry_run": True, "outbound_id": outbound.id},
            created_at=_utc_now(),
        )

    target_agent = create_agent(target_canonical)
    provider_messages = [
        Message(
            role="user",
            content=f"[Message from {sender_canonical}]\n\n{message}",
        )
    ]
    agent_response = target_agent.provider.send(provider_messages)

    # --- Hook 3: identity-signing on the response (handled inside _build_response).
    return _build_response(
        sender=target_canonical,
        target=sender_canonical,
        session_id=session_id_final,
        parent_id=outbound.id,
        raw_content=agent_response.content,
        model=agent_response.model,
        provider_family=target_agent.provider_family,
        finish_reason=agent_response.finish_reason,
        usage=agent_response.usage,
    )


def _format_envelope(env: MessageEnvelope) -> str:
    """Render an envelope as a human-readable block for CLI output."""
    lines = [
        f"  id           : {env.id}",
        f"  session_id   : {env.session_id}",
        f"  sender       : {env.sender}",
        f"  target       : {env.target}",
        f"  message_type : {env.message_type}",
        f"  action_surface: {env.action_surface}",
        f"  parent_id    : {env.parent_id or '-'}",
        f"  created_at   : {env.created_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "  metadata     :",
    ]
    for key, value in env.metadata.items():
        rendered = repr(value)
        if len(rendered) > 200:
            rendered = rendered[:200] + "…"
        lines.append(f"    {key}: {rendered}")
    lines.append("  content      :")
    for line in env.content.splitlines() or [""]:
        lines.append(f"    {line}")
    return "\n".join(lines)


def dry_run() -> int:
    """Exercise the message protocol without making any provider API call.

    Validates envelope construction, hook short-circuits (secrets, gates),
    canonicalization, and the safe-path skip-provider mode. Prints one
    signed Cody line per scenario. Exit 0 if every scenario behaves as
    expected; exit 1 otherwise.
    """
    load_env_file()
    passes: list[str] = []
    failures: list[str] = []

    def record(case: str, ok: bool, detail: str) -> None:
        if ok:
            passes.append(f"{case}: {detail}")
        else:
            failures.append(f"{case}: {detail}")

    # Scenario 1: safe message, skip provider — should return a synthetic
    # response envelope from the target with the right shape.
    try:
        env = send_message(
            "Atlas", "Cody", "Test safe message body.", _skip_provider=True
        )
        ok = (
            env.message_type == MESSAGE_TYPE_RESPONSE
            and env.sender == "Cody"
            and env.target == "Atlas"
            and env.action_surface == SAFE
            and env.parent_id is not None
            and env.metadata.get("dry_run") is True
            and env.content.startswith("[Cody · ")
        )
        record("safe-skip", ok, "synthetic response envelope OK" if ok else f"unexpected envelope: {env}")
    except Exception as exc:  # noqa: BLE001
        record("safe-skip", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 2: human-approved-only — must return a signed blocker without
    # calling the provider, regardless of _skip_provider.
    try:
        env = send_message(
            "Atlas",
            "Cody",
            "Test human-approved-only path.",
            action_surface=HUMAN_APPROVED_ONLY,
        )
        ok = (
            env.message_type == MESSAGE_TYPE_BLOCKER
            and env.sender == "Cody"
            and env.target == "Atlas"
            and env.metadata.get("blocker_phase") == "approval_gates"
            and env.metadata.get("decision") == "pending-human-approval"
            and env.content.startswith("[Cody · ")
            and "Blocked:" in env.content
        )
        record("human-approved-only", ok, "blocker envelope OK" if ok else f"unexpected envelope: {env}")
    except Exception as exc:  # noqa: BLE001
        record("human-approved-only", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 3: secrets in payload — must be blocked by secrets_check
    # before any gate evaluation, with the kinds listed in metadata.
    try:
        env = send_message(
            "Atlas",
            "Cody",
            "Please don't leak: sk-proj-AAAAAAAAAAAAAAAAAAAA",
            _skip_provider=True,
        )
        ok = (
            env.message_type == MESSAGE_TYPE_BLOCKER
            and env.metadata.get("blocker_phase") == "secrets_check"
            and "openai_api_key" in env.metadata.get("secret_kinds", [])
        )
        record("secrets-block", ok, "secrets blocker OK" if ok else f"unexpected envelope: {env}")
    except Exception as exc:  # noqa: BLE001
        record("secrets-block", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 4: unknown action surface — should raise ProtocolError.
    try:
        send_message(
            "Atlas",
            "Cody",
            "Test unknown surface.",
            action_surface="invalid-surface",
            _skip_provider=True,
        )
        record("unknown-surface", False, "did NOT raise ProtocolError")
    except ProtocolError:
        record("unknown-surface", True, "raised ProtocolError as expected")
    except Exception as exc:  # noqa: BLE001
        record("unknown-surface", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 5: unknown agent name (target) — should raise UnknownAgentError.
    try:
        send_message("Atlas", "Sentinel", "Test unknown target.", _skip_provider=True)
        record("unknown-target", False, "did NOT raise UnknownAgentError")
    except UnknownAgentError:
        record("unknown-target", True, "raised UnknownAgentError as expected")
    except Exception as exc:  # noqa: BLE001
        record("unknown-target", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 6: case-insensitive canonicalization through 4.3 factory.
    try:
        env = send_message("atlas", "CODY", "Case test.", _skip_provider=True)
        ok = env.sender == "Cody" and env.target == "Atlas"
        record(
            "canonicalize",
            ok,
            "Atlas/Cody resolved case-insensitively" if ok else f"unexpected names: {env.sender}/{env.target}",
        )
    except Exception as exc:  # noqa: BLE001
        record("canonicalize", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 7: guarded surface with safe action — gate allows; should
    # short-circuit at _skip_provider with a synthetic response envelope.
    try:
        env = send_message(
            "Atlas",
            "Cody",
            "Test guarded surface body.",
            action_surface=GUARDED,
            _skip_provider=True,
        )
        ok = (
            env.message_type == MESSAGE_TYPE_RESPONSE
            and env.metadata.get("dry_run") is True
        )
        record("guarded-allow", ok, "guarded surface allows skip-provider OK" if ok else f"unexpected envelope: {env}")
    except Exception as exc:  # noqa: BLE001
        record("guarded-allow", False, f"raised {type(exc).__name__}: {exc}")

    for case in passes:
        print(sign_action("Cody", f"dry-run pass — {case}"))
    for case in failures:
        print(sign_action("Cody", f"dry-run FAIL — {case}"))

    return 0 if not failures else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm.message_protocol",
        description="Message-passing protocol for Agent Orchestra (M4 task 4.4).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run hook + envelope scenarios without any provider API call.",
    )
    parser.add_argument(
        "--send",
        nargs=3,
        metavar=("SENDER", "TARGET", "MESSAGE"),
        help="Make one real round-trip from SENDER to TARGET with MESSAGE.",
    )
    parser.add_argument(
        "--surface",
        default=SAFE,
        choices=sorted(VALID_SURFACES),
        help="Action surface for the --send call (default: safe).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run()
    if args.send:
        sender, target, message = args.send
        try:
            envelope = send_message(
                sender, target, message, action_surface=args.surface
            )
        except UnknownAgentError as exc:
            print(sign_action("Cody", f"send blocked — {exc}"))
            return 2
        except ProtocolError as exc:
            print(sign_action("Cody", f"send blocked — {exc}"))
            return 2
        except MissingEnvError as exc:
            print(sign_action("Cody", f"send blocked — {exc}"))
            return 2
        except ImportError as exc:
            print(sign_action("Cody", f"send blocked — SDK not installed: {exc}"))
            return 2
        print(sign_action("Cody", f"send returned envelope {envelope.id}:"))
        print(_format_envelope(envelope))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
