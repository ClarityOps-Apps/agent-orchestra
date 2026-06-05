"""Supervisor loop for Agent Orchestra (M4 task 4.5).

Top-level orchestration service:

  1. Garrett's directive arrives.
  2. Pre-flight: secrets check + env presence.
  3. Atlas *planner* turn — Atlas decomposes the directive into a JSON plan
     of subagent steps (Cody / Scribe / Scout).
  4. Sequential step execution — each step routes through the 4.4 message
     protocol (`send_message`) so 4.2/4.3 providers, 4.4 envelope, hook
     chain, and identity-signing all apply uniformly.
  5. Atlas *finalizer* turn — Atlas synthesizes the result from the
     transcript and emits a signed summary for Garrett.

This module is a **library** — 4.8 owns the polished public CLI
(`python orchestra.py --directive "..."`). The CLI exposed here
(`uv run python -m supervisor --dry-run | --directive | --resume`) is
validation-only.

SQLite session persistence (task 4.6) is integrated as an opt-in
parameter (`store=SessionStore(...)`) on `run_supervisor()` and is the
sole mechanism `resume_supervisor()` uses to continue a crashed or
in-flight run **in place** under the original `run_id`. Default behavior
when no store is provided is unchanged from 4.5 — the run is in-memory
only and the legacy 4.5 surface is preserved byte-for-byte.

This module does NOT implement:
- MCP tool wiring inside the loop (task 4.7).
- The polished public CLI on `orchestra.py` (task 4.8).
- REST API endpoints (task 4.11).

Per the M4 architecture scope (Asana comment 1215386979487630): hooks wrap
every action; providers are reached only through the factory + protocol
layer; turn-taking is sequential; fail-closed across plan parse, target
canonicalization, action-surface validation, max-step overflow, missing
env, provider errors, and pending-human-approval gates.

Run from `runtime/`:

  cd runtime
  uv run python -m supervisor --dry-run
  uv run python -m supervisor --directive "Ask Cody to acknowledge supervisor loop live."
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import MissingEnvError, load_env_file
from hooks.identity_signing import sign_action
from hooks.secrets_check import check_for_secrets, find_secrets, redact
from llm.agent_factory import CANONICAL_AGENT_NAMES, UnknownAgentError, get_spec
from llm.message_protocol import ProtocolError, send_message
from llm.types import (
    ACTION_SURFACE_HUMAN_APPROVED_ONLY,
    ACTION_SURFACE_SAFE,
    MESSAGE_TYPE_BLOCKER,
    MESSAGE_TYPE_DIRECTIVE,
    MESSAGE_TYPE_RESPONSE,
    MessageEnvelope,
    VALID_ACTION_SURFACES,
)
from session_store import (
    DECISION_KIND_BLOCKER,
    DECISION_KIND_ERROR,
    PHASE_BLOCKER,
    PHASE_FINALIZER,
    PHASE_PLANNER,
    PHASE_STEP,
    SessionStore,
)
from tool_registry import (
    TOOL_REGISTRY,
    TOOL_STATUS_BLOCKED,
    TOOL_STATUS_ERRORED,
    TOOL_STATUS_OK,
    TOOL_STATUS_PENDING_HUMAN_APPROVAL,
    ToolCallResult,
    UnknownToolError,
    allowed_tools_for,
    execute_tool,
    is_agent_allowed,
)


# --- Constants ---------------------------------------------------------------

DEFAULT_MAX_STEPS = 5
HARD_CAP_MAX_STEPS = 10
DIRECTIVE_SUMMARY_LIMIT = 200
PLAN_BLOCK_RE = re.compile(
    r"<orchestra_plan>\s*(\{.*?\})\s*</orchestra_plan>", re.DOTALL
)

# Subagent targets the planner is allowed to invoke. Atlas is the planner
# and finalizer; subagent steps that target Atlas would re-enter the
# planner loop, which 4.5 does not support.
VALID_SUBAGENT_TARGETS: frozenset[str] = frozenset({"Cody", "Scribe", "Scout"})

SUPERVISOR_STATUS_PLANNING = "planning"
SUPERVISOR_STATUS_EXECUTING = "executing"
SUPERVISOR_STATUS_FINALIZING = "finalizing"
SUPERVISOR_STATUS_COMPLETE = "complete"
SUPERVISOR_STATUS_BLOCKED = "blocked"
SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL = "pending_human_approval"
SUPERVISOR_STATUS_ERRORED = "errored"

VALID_SUPERVISOR_STATUSES: frozenset[str] = frozenset(
    {
        SUPERVISOR_STATUS_PLANNING,
        SUPERVISOR_STATUS_EXECUTING,
        SUPERVISOR_STATUS_FINALIZING,
        SUPERVISOR_STATUS_COMPLETE,
        SUPERVISOR_STATUS_BLOCKED,
        SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
        SUPERVISOR_STATUS_ERRORED,
    }
)

STEP_STATUS_PLANNED = "planned"
STEP_STATUS_RESPONDED = "responded"
STEP_STATUS_BLOCKED = "blocked"
STEP_STATUS_ERRORED = "errored"
STEP_STATUS_SKIPPED = "skipped"


class PlanError(ValueError):
    """Raised when the planner output cannot be parsed or validated."""


# --- Transient provider-error catch list (M4 task 4.7 narrow step) ----------
#
# Provider SDKs raise their own HTTP-tier exceptions (HTTP 429 rate limit,
# HTTP 529 Anthropic overload, generic API status, connection errors).
# 4.6's closure flag noted that `anthropic.OverloadedError` surfaced as a
# raw traceback because `_execute_step` only caught the runtime's own
# named exceptions. 4.7 step 1 wraps those transient errors at every
# planner/step/finalizer call site so they become signed Atlas error
# decisions rather than tracebacks.
#
# Imported defensively: missing classes in either SDK simply omit that
# entry from the catch tuple. The runtime never imports a class it can't
# find; the dry-run scenarios remain safe even if a future SDK rename
# removes one of these names.
def _collect_transient_provider_errors() -> tuple[type[BaseException], ...]:
    """Return the catch tuple of known provider transient/API errors.

    Always returns at least `()`; never raises on missing SDKs or
    renamed classes. Callers should add the result to their
    `except (...)` tuple alongside the runtime's own exception types.

    Probes both the top-level SDK module (where the public/canonical
    classes typically live) and `<sdk>._exceptions` (where SDK internals
    keep specialised subclasses). The anthropic SDK exposes
    `APIStatusError`/`RateLimitError`/etc. at the top level but keeps
    HTTP-status-specific subclasses like `OverloadedError`,
    `ServiceUnavailableError`, `InternalServerError`,
    `DeadlineExceededError`, and `RequestTooLargeError` only under
    `anthropic._exceptions`. We want the catch tuple to include both
    layers so the signed error decision can name the specific class.
    """
    found: list[type[BaseException]] = []
    candidates = (
        # Top-level public classes (canonical for the runtime catch).
        ("anthropic", "OverloadedError"),
        ("anthropic", "RateLimitError"),
        ("anthropic", "APIStatusError"),
        ("anthropic", "APIConnectionError"),
        ("anthropic", "APITimeoutError"),
        ("openai", "RateLimitError"),
        ("openai", "APIStatusError"),
        ("openai", "APIConnectionError"),
        ("openai", "APITimeoutError"),
        # Internal _exceptions: HTTP-status-specific subclasses some SDKs
        # raise rather than the parent APIStatusError. Probed defensively
        # so an SDK that doesn't expose these still works fine.
        ("anthropic._exceptions", "OverloadedError"),
        ("anthropic._exceptions", "ServiceUnavailableError"),
        ("anthropic._exceptions", "InternalServerError"),
        ("anthropic._exceptions", "DeadlineExceededError"),
        ("anthropic._exceptions", "RequestTooLargeError"),
        ("openai._exceptions", "InternalServerError"),
    )
    for module_name, class_name in candidates:
        try:
            module = __import__(module_name, fromlist=[class_name])
        except ImportError:
            continue
        cls = getattr(module, class_name, None)
        if (
            cls is not None
            and isinstance(cls, type)
            and issubclass(cls, BaseException)
            and cls not in found
        ):
            found.append(cls)
    return tuple(found)


TRANSIENT_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    _collect_transient_provider_errors()
)


# --- Dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorStep:
    """In-memory record of one subagent turn.

    `error_info` is populated when `status == STEP_STATUS_ERRORED`. It
    carries a small dict with the exception class name, message, and a
    `kind` tag (`transient_provider` vs `runtime`) so the run-level
    error-decision record can surface a useful signed message rather
    than the generic "send_message raised" line.
    """

    id: int
    target: str
    message: str
    action_surface: str
    reason: str
    status: str
    response_envelope: MessageEnvelope | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_info: dict[str, Any] | None = None


@dataclass(frozen=True)
class SupervisorRun:
    """In-memory record of one supervisor invocation.

    When the run was driven through `run_supervisor(..., store=...)`,
    the same record is also persisted to SQLite via the 4.6 session
    store at every phase boundary; this dataclass is then a snapshot
    of the rehydrated state. Without a store, the run lives only in
    the returning caller's address space.
    """

    run_id: str
    session_id: str
    directive_summary: str
    status: str
    created_at: datetime
    completed_at: datetime | None
    planner_envelope: MessageEnvelope | None
    plan: dict[str, Any] | None
    steps: tuple[SupervisorStep, ...]
    finalizer_envelope: MessageEnvelope | None
    blockers: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]
    max_steps: int
    dry_run: bool


# --- Helpers -----------------------------------------------------------------


def _new_id() -> str:
    return str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _blocker_record(*, phase: str, reason: str, **extra: Any) -> dict[str, Any]:
    """Build a signed blocker record for `SupervisorRun.blockers`.

    Every supervisor-owned halt point goes through this helper so that 4.6
    persistence has an agent-signed human-readable artifact alongside the
    structured metadata. The `signed_message` field is produced via
    `sign_action('Atlas', ...)` with real UTC and matches the hooks-layer
    signature format used by the rest of the runtime.

    Args:
        phase: short identifier for the halt site (e.g.
            `preflight_secrets_check`, `plan_parse_or_validate`,
            `step_1_blocker`, `finalizer_blocker`).
        reason: human-readable reason. Should not contain secrets — the
            caller is responsible for redacting before calling.
        **extra: any additional structured fields the caller wants to
            persist (e.g. `secret_kinds`, `envelope_id`, `step_id`,
            `target`, `decision`, `blocker_phase`).

    Returns:
        dict with `phase`, `reason`, all `extra` fields, and a
        `signed_message` Atlas signature line.
    """
    record: dict[str, Any] = {"phase": phase, "reason": reason}
    record.update(extra)
    record["signed_message"] = sign_action(
        "Atlas", f"Supervisor blocker [{phase}]: {reason}"
    )
    return record


def _error_record(*, phase: str, error: str, **extra: Any) -> dict[str, Any]:
    """Build a signed error record for `SupervisorRun.errors`.

    Same pattern as `_blocker_record` but for run-level errors (provider
    exceptions, missing env, SDK import failures, last-resort catches).
    The `signed_message` is produced via `sign_action('Atlas', ...)` so
    4.6 persistence and any operator-facing surface (4.11 REST, future
    flight deck) read a signed Atlas line per error.
    """
    record: dict[str, Any] = {"phase": phase, "error": error}
    record.update(extra)
    record["signed_message"] = sign_action(
        "Atlas", f"Supervisor error [{phase}]: {error}"
    )
    return record


def _summarize_directive(directive: str) -> str:
    """Produce a bounded, redacted directive summary for receipts/blockers.

    The summary never carries token-shaped strings (they'd already have
    been caught by the pre-flight secrets check anyway, but defense in
    depth keeps any later receipt logging safe).
    """
    summary = redact(directive) if find_secrets(directive) else directive
    summary = summary.replace("\n", " ").strip()
    if len(summary) > DIRECTIVE_SUMMARY_LIMIT:
        summary = summary[:DIRECTIVE_SUMMARY_LIMIT] + "…"
    return summary


def _normalize_max_steps(max_steps: int) -> int:
    """Cap max_steps to the hard ceiling and floor to 1."""
    return max(1, min(int(max_steps), HARD_CAP_MAX_STEPS))


def _planner_prompt(directive: str, max_steps: int) -> str:
    """Build the planner instruction passed to Atlas through send_message."""
    targets = ", ".join(sorted(VALID_SUBAGENT_TARGETS))
    surfaces = ", ".join(sorted(VALID_ACTION_SURFACES))
    return (
        "You are operating in supervisor-planner mode for Agent Orchestra.\n"
        "Garrett issued the following directive to the team:\n\n"
        f"---\n{directive}\n---\n\n"
        "Decompose the work into a sequence of subagent steps. Emit a strict, "
        "machine-readable JSON plan inside exactly one "
        "<orchestra_plan>...</orchestra_plan> block. Do not include any prose "
        "outside the block.\n\n"
        "Required JSON shape:\n"
        "{\n"
        '  "summary": "one-sentence summary of what you are doing",\n'
        '  "steps": [\n'
        '    {"id": 1, "target": "Cody", "message": "...", '
        '"action_surface": "safe", "reason": "..."}\n'
        "  ],\n"
        '  "final_response_instruction": "How Atlas should synthesize the '
        "final response after all steps run.\"\n"
        "}\n\n"
        f"Valid targets for subagent steps: {targets}. "
        "Atlas self-calls are reserved for planner and finalizer turns and "
        "must not appear as a step.\n"
        f"Valid action_surface values: {surfaces}.\n"
        f"Maximum {max_steps} steps. step.id is a 1-based integer.\n"
        "Planning guidance:\n"
        "- If the work can proceed, emit at least one valid step.\n"
        "- If the next action requires Garrett's explicit approval before "
        "any subagent runs, mark that step with "
        'action_surface="human-approved-only". The runtime will halt at the '
        "gate without calling the target provider and surface the pending "
        "decision to Garrett.\n"
        "- If you cannot plan a safe path at all, explain the blocker in "
        "`summary` and omit risky steps. The runtime fails closed on a "
        "zero-step plan, so this produces a recorded supervisor blocker "
        "for Garrett rather than running anything unsafe."
    )


def _finalizer_prompt(
    plan: dict[str, Any],
    steps: tuple[SupervisorStep, ...],
) -> str:
    """Build the finalizer instruction with a redacted transcript."""
    instruction = plan.get(
        "final_response_instruction",
        "Synthesize a brief, signed summary of the work for Garrett.",
    )
    lines = [
        "You are operating in supervisor-finalizer mode for Agent Orchestra.",
        "All planned steps have completed without a blocker. Synthesize a "
        "single signed response for Garrett based on the transcript below.",
        "",
        f"Finalizer instruction from the planner: {instruction}",
        "",
        "Transcript:",
    ]
    for step in steps:
        response = step.response_envelope
        # `response.content` is already redacted by the 4.4 protocol; safe
        # to embed in the transcript that goes to Atlas.
        snippet = response.content if response is not None else "(no response)"
        snippet = snippet.replace("\n", " ")
        if len(snippet) > 1000:
            snippet = snippet[:1000] + "…[truncated]"
        lines.append(
            f"- step {step.id} → {step.target} ({step.action_surface}): {snippet}"
        )
    lines.append("")
    lines.append(
        "Emit your final response as a single signed Atlas message for "
        "Garrett. Do not include the transcript verbatim in your output."
    )
    return "\n".join(lines)


# --- Plan parsing + validation -----------------------------------------------


def _parse_plan(planner_content: str) -> dict[str, Any]:
    """Extract and JSON-parse the <orchestra_plan>...</orchestra_plan> block.

    Raises `PlanError` on missing block, malformed JSON, or non-object root.
    """
    match = PLAN_BLOCK_RE.search(planner_content)
    if not match:
        raise PlanError(
            "Planner output missing <orchestra_plan>{...}</orchestra_plan> block."
        )
    body = match.group(1)
    try:
        plan = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PlanError(f"Planner JSON parse failed: {exc.msg} at pos {exc.pos}") from exc
    if not isinstance(plan, dict):
        raise PlanError(
            f"Planner JSON root must be an object, got {type(plan).__name__}."
        )
    return plan


def _validate_plan(plan: dict[str, Any], max_steps: int) -> list[dict[str, Any]]:
    """Validate the parsed plan and return the normalized steps list.

    Raises `PlanError` on any structural problem. Caller is expected to
    wrap the error in a signed Atlas blocker envelope.
    """
    if "summary" not in plan or not isinstance(plan["summary"], str):
        raise PlanError("Plan missing string `summary`.")
    if "steps" not in plan or not isinstance(plan["steps"], list):
        raise PlanError("Plan missing list `steps`.")
    raw_steps = plan["steps"]
    if len(raw_steps) == 0:
        raise PlanError("Plan has zero steps — nothing to execute.")
    if len(raw_steps) > max_steps:
        raise PlanError(
            f"Plan has {len(raw_steps)} steps, exceeds max_steps={max_steps}."
        )

    normalized: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for idx, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise PlanError(f"Step #{idx} must be a JSON object.")
        required = ("id", "target", "message", "action_surface", "reason")
        missing = [k for k in required if k not in raw]
        if missing:
            raise PlanError(
                f"Step #{idx} missing required fields: {', '.join(missing)}."
            )
        step_id = raw["id"]
        if not isinstance(step_id, int) or step_id < 1:
            raise PlanError(f"Step #{idx} id must be a positive int.")
        if step_id in seen_ids:
            raise PlanError(f"Step id {step_id} is duplicated in the plan.")
        seen_ids.add(step_id)
        target = raw["target"]
        if not isinstance(target, str) or not target:
            raise PlanError(f"Step {step_id} target must be a non-empty string.")
        # Canonicalize early to catch unknown agents before any provider call.
        try:
            spec = get_spec(target)
        except UnknownAgentError as exc:
            raise PlanError(f"Step {step_id} target unknown: {exc}") from exc
        canonical_target = spec.name
        if canonical_target not in VALID_SUBAGENT_TARGETS:
            raise PlanError(
                f"Step {step_id} target {canonical_target!r} not allowed; "
                f"valid subagent targets are {sorted(VALID_SUBAGENT_TARGETS)}."
            )
        message = raw["message"]
        if not isinstance(message, str) or not message.strip():
            raise PlanError(f"Step {step_id} message must be a non-empty string.")
        action_surface = raw["action_surface"]
        if action_surface not in VALID_ACTION_SURFACES:
            raise PlanError(
                f"Step {step_id} action_surface {action_surface!r} invalid; "
                f"valid: {sorted(VALID_ACTION_SURFACES)}."
            )
        reason = raw["reason"]
        if not isinstance(reason, str):
            raise PlanError(f"Step {step_id} reason must be a string.")
        # 4.7 main scope: optional `tool_calls` on a step. Each entry must
        # be `{tool_name: str, args: object}`. The tool name must be in
        # the registry; the agent (canonical_target) must be permitted to
        # use the tool. Validation here keeps the supervisor's policy
        # surface fail-closed before any provider/executor call.
        tool_calls_raw = raw.get("tool_calls", [])
        if not isinstance(tool_calls_raw, list):
            raise PlanError(
                f"Step {step_id} tool_calls must be a list of objects."
            )
        validated_tool_calls: list[dict[str, Any]] = []
        for tc_idx, tc in enumerate(tool_calls_raw, start=1):
            if not isinstance(tc, dict):
                raise PlanError(
                    f"Step {step_id} tool_calls[{tc_idx}] must be a JSON object."
                )
            tc_tool = tc.get("tool_name")
            if not isinstance(tc_tool, str) or not tc_tool:
                raise PlanError(
                    f"Step {step_id} tool_calls[{tc_idx}] missing string `tool_name`."
                )
            if tc_tool not in TOOL_REGISTRY:
                raise PlanError(
                    f"Step {step_id} tool_calls[{tc_idx}] unknown tool {tc_tool!r}; "
                    f"registered: {sorted(TOOL_REGISTRY)}."
                )
            tc_args = tc.get("args", {})
            if not isinstance(tc_args, dict):
                raise PlanError(
                    f"Step {step_id} tool_calls[{tc_idx}] args must be a JSON object."
                )
            if not is_agent_allowed(canonical_target, tc_tool):
                raise PlanError(
                    f"Step {step_id} tool_calls[{tc_idx}]: agent "
                    f"{canonical_target} is not authorized for {tc_tool}."
                )
            validated_tool_calls.append({"tool_name": tc_tool, "args": tc_args})
        normalized.append(
            {
                "id": step_id,
                "target": canonical_target,
                "message": message,
                "action_surface": action_surface,
                "reason": reason,
                "tool_calls": validated_tool_calls,
            }
        )
    return normalized


# --- Provider-call wrappers (planner / step / finalizer) ---------------------


DRY_RUN_DEFAULT_PLAN: dict[str, Any] = {
    "summary": "Dry-run synthetic plan: one safe Cody acknowledgement step.",
    "steps": [
        {
            "id": 1,
            "target": "Cody",
            "message": "Acknowledge the dry-run supervisor request.",
            "action_surface": ACTION_SURFACE_SAFE,
            "reason": "Supervisor dry-run smoke step.",
        }
    ],
    "final_response_instruction": (
        "Summarize Cody's acknowledgement for Garrett."
    ),
}


def _planner_envelope_for_dry_run(
    session_id: str,
    parent_id: str | None,
    content_override: str | None = None,
) -> MessageEnvelope:
    """Synthetic planner envelope used by dry-run.

    By default the envelope contains a valid one-step plan. When
    `content_override` is provided, that string becomes the planner's
    pre-signature payload — allowing dry-run scenarios to inject
    malformed-JSON / unknown-target / invalid-surface / max-step-overflow
    planner outputs and verify the supervisor halt paths produce signed
    Atlas blockers via the full `run_supervisor()` flow.
    """
    if content_override is None:
        payload = f"<orchestra_plan>{json.dumps(DRY_RUN_DEFAULT_PLAN)}</orchestra_plan>"
    else:
        payload = content_override
    return MessageEnvelope(
        id=_new_id(),
        session_id=session_id,
        sender="Atlas",
        target="Atlas",
        message_type=MESSAGE_TYPE_RESPONSE,
        content=sign_action("Atlas", payload),
        action_surface=ACTION_SURFACE_SAFE,
        parent_id=parent_id,
        metadata={"dry_run": True, "synthetic_planner": True},
        created_at=_utc_now(),
    )


def _plan_turn(
    directive: str,
    session_id: str,
    max_steps: int,
    dry_run: bool,
    planner_content_override: str | None = None,
) -> MessageEnvelope:
    """Run the planner turn. Returns the planner's response envelope.

    The envelope flows through the same 4.4 protocol used for subagent
    sends, so the secrets check, identity-sign, and runtime UTC stamping
    rules all apply uniformly.

    `planner_content_override` is dry-run only; live runs ignore it and
    invoke the real Atlas planner through `send_message`.
    """
    if dry_run:
        return _planner_envelope_for_dry_run(
            session_id, parent_id=None, content_override=planner_content_override
        )
    return send_message(
        "Atlas",
        "Atlas",
        _planner_prompt(directive, max_steps),
        session_id=session_id,
        message_type=MESSAGE_TYPE_DIRECTIVE,
        action_surface=ACTION_SURFACE_SAFE,
        parent_id=None,
        metadata={"phase": "planner"},
    )


def _execute_step(
    step_spec: dict[str, Any],
    session_id: str,
    parent_id: str,
    dry_run: bool,
    *,
    _force_exception: BaseException | None = None,
) -> SupervisorStep:
    """Run one subagent step and return its SupervisorStep record.

    Provider-transient errors from `send_message` (e.g. Anthropic 529
    overload, OpenAI rate-limit) are caught alongside the runtime's own
    named errors and surfaced as `status=STEP_STATUS_ERRORED` with a
    populated `error_info` dict. The run-level error decision then
    carries a signed Atlas message that names the exception class
    instead of leaking a raw traceback. Task 4.7 step 1; resolves the
    4.6 closure flag where a 529 surfaced raw.

    `_force_exception` is a dry-run-only test hook. When provided, the
    given exception is raised in place of the real `send_message` call
    so the supervisor's transient-error wrapping is regression-covered
    without forcing a real upstream failure.
    """
    started_at = _utc_now()
    try:
        if _force_exception is not None:
            raise _force_exception
        response = send_message(
            "Atlas",
            step_spec["target"],
            step_spec["message"],
            session_id=session_id,
            message_type="agent_message",
            action_surface=step_spec["action_surface"],
            parent_id=parent_id,
            metadata={
                "phase": "step",
                "step_id": step_spec["id"],
                "step_reason": step_spec["reason"],
            },
            _skip_provider=dry_run,
        )
    except (UnknownAgentError, ProtocolError, MissingEnvError) as exc:
        return SupervisorStep(
            id=step_spec["id"],
            target=step_spec["target"],
            message=step_spec["message"],
            action_surface=step_spec["action_surface"],
            reason=step_spec["reason"],
            status=STEP_STATUS_ERRORED,
            response_envelope=None,
            started_at=started_at,
            completed_at=_utc_now(),
            error_info={
                "kind": "runtime",
                "exception_class": type(exc).__name__,
                "message": str(exc),
            },
        )
    except TRANSIENT_PROVIDER_ERRORS as exc:
        return SupervisorStep(
            id=step_spec["id"],
            target=step_spec["target"],
            message=step_spec["message"],
            action_surface=step_spec["action_surface"],
            reason=step_spec["reason"],
            status=STEP_STATUS_ERRORED,
            response_envelope=None,
            started_at=started_at,
            completed_at=_utc_now(),
            error_info={
                "kind": "transient_provider",
                "exception_class": type(exc).__name__,
                "message": str(exc),
            },
        )
    if response.message_type == MESSAGE_TYPE_BLOCKER:
        return SupervisorStep(
            id=step_spec["id"],
            target=step_spec["target"],
            message=step_spec["message"],
            action_surface=step_spec["action_surface"],
            reason=step_spec["reason"],
            status=STEP_STATUS_BLOCKED,
            response_envelope=response,
            started_at=started_at,
            completed_at=_utc_now(),
        )
    return SupervisorStep(
        id=step_spec["id"],
        target=step_spec["target"],
        message=step_spec["message"],
        action_surface=step_spec["action_surface"],
        reason=step_spec["reason"],
        status=STEP_STATUS_RESPONDED,
        response_envelope=response,
        started_at=started_at,
        completed_at=_utc_now(),
    )


def _finalize_turn(
    plan: dict[str, Any],
    steps: tuple[SupervisorStep, ...],
    session_id: str,
    parent_id: str,
    dry_run: bool,
) -> MessageEnvelope:
    """Run the Atlas finalizer turn over the redacted transcript."""
    prompt = _finalizer_prompt(plan, steps)
    if dry_run:
        # Use the protocol's own dry-run skip path so the response envelope
        # is shaped exactly the same as a real one — just without an API call.
        return send_message(
            "Atlas",
            "Atlas",
            prompt,
            session_id=session_id,
            message_type="agent_message",
            action_surface=ACTION_SURFACE_SAFE,
            parent_id=parent_id,
            metadata={"phase": "finalizer", "dry_run": True},
            _skip_provider=True,
        )
    return send_message(
        "Atlas",
        "Atlas",
        prompt,
        session_id=session_id,
        message_type="agent_message",
        action_surface=ACTION_SURFACE_SAFE,
        parent_id=parent_id,
        metadata={"phase": "finalizer"},
    )


# --- Public entry point ------------------------------------------------------


def _build_run(
    *,
    run_id: str,
    session_id: str,
    directive_summary: str,
    status: str,
    created_at: datetime,
    completed_at: datetime | None,
    planner_envelope: MessageEnvelope | None,
    plan: dict[str, Any] | None,
    steps: list[SupervisorStep],
    finalizer_envelope: MessageEnvelope | None,
    blockers: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    max_steps: int,
    dry_run: bool,
) -> SupervisorRun:
    if status not in VALID_SUPERVISOR_STATUSES:
        raise ValueError(f"Internal invariant: unknown supervisor status {status!r}.")
    return SupervisorRun(
        run_id=run_id,
        session_id=session_id,
        directive_summary=directive_summary,
        status=status,
        created_at=created_at,
        completed_at=completed_at,
        planner_envelope=planner_envelope,
        plan=plan,
        steps=tuple(steps),
        finalizer_envelope=finalizer_envelope,
        blockers=tuple(blockers),
        errors=tuple(errors),
        max_steps=max_steps,
        dry_run=dry_run,
    )


def run_supervisor(
    directive: str,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    dry_run: bool = False,
    store: SessionStore | None = None,
    _dry_run_planner_content: str | None = None,
    _stop_after: str | None = None,
    _force_step_exception: BaseException | None = None,
    _force_step_target_id: int | None = None,
) -> SupervisorRun:
    """Run one supervisor invocation and return a `SupervisorRun` record.

    See module docstring for the turn structure. This function never raises
    on planner/step/finalizer failures — every halt path returns a
    `SupervisorRun` with the relevant status and a signed Atlas
    blocker/error record. Internal invariant violations (e.g. status enum
    drift) are the only paths that raise.

    Args:
        directive: Garrett's directive text.
        max_steps: maximum subagent steps the planner may emit. Clamped to
            [1, HARD_CAP_MAX_STEPS].
        dry_run: when True, no provider API calls are made. The planner
            turn returns a canned valid plan (or `_dry_run_planner_content`
            when provided); subagent and finalizer calls use the 4.4
            protocol's `_skip_provider=True` path. Used by the module
            CLI's --dry-run and by CI/local checks without secrets.
        store: optional `SessionStore` to persist the run incrementally.
            Each phase boundary writes inside its own transaction so a
            crash mid-run leaves a coherent prefix that can be resumed
            via `resume_supervisor()`. When `None` (the default), the
            run is in-memory only — preserving the existing 4.5 surface
            unchanged. Task 4.6 owns this hook.
        _dry_run_planner_content: private dry-run-only hook. When `dry_run`
            is True and a string is provided, this becomes the synthetic
            planner envelope's pre-signature payload. Dry-run scenarios
            use this to inject malformed plans, unknown targets, invalid
            surfaces, etc. — verifying the supervisor's halt paths
            produce signed Atlas blockers end-to-end. Ignored on live runs.
        _stop_after: private crash-simulation hook (4.6 dry-run only).
            When set to one of `"planner"`, `"step:1"`, `"step:2"`, etc.,
            the loop returns immediately after persisting the named phase
            without continuing. Used by crash/resume tests to prove
            resume completes the remaining work without duplication.

    Returns:
        SupervisorRun: in-memory record of the run (also persisted to
        `store` when one is provided).
    """
    if not isinstance(directive, str):
        raise TypeError("run_supervisor: directive must be a string.")

    # 4.7 step 1 addendum: enforce dry-run-only on the forced-exception
    # test hooks. The receipt promised live runs ignore them; the code
    # now makes that strictly true — passing the kwargs on a live run is
    # rejected at the entry point rather than silently respected.
    # Atlas review 1215456686905326.
    if not dry_run and (
        _force_step_exception is not None or _force_step_target_id is not None
    ):
        raise ValueError(
            "run_supervisor: `_force_step_exception` and "
            "`_force_step_target_id` are dry-run-only test hooks. "
            "Set `dry_run=True` to use them; live runs must not force "
            "synthetic exceptions."
        )

    load_env_file()
    run_id = _new_id()
    session_id = _new_id()
    created_at = _utc_now()
    directive_summary = _summarize_directive(directive)
    max_steps_norm = _normalize_max_steps(max_steps)

    blockers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    steps: list[SupervisorStep] = []
    planner_envelope: MessageEnvelope | None = None
    plan: dict[str, Any] | None = None
    finalizer_envelope: MessageEnvelope | None = None

    # --- Persistence helpers (no-op when store is None) -------------------
    # Each helper isolates the "if store:" guard so the loop body reads
    # cleanly. Failures from the persistence layer surface as runtime
    # errors (not silent drops) — 4.6's contract is that a write either
    # succeeded or the process knew it failed.
    if store is not None:
        store.ensure_schema()
        store.record_session(
            run_id=run_id,
            session_id=session_id,
            directive_summary=directive_summary,
            status=SUPERVISOR_STATUS_PLANNING,
            max_steps=max_steps_norm,
            dry_run=dry_run,
            created_at=created_at,
        )

    def _persist_envelope(envelope: MessageEnvelope, *, phase: str) -> None:
        if store is None:
            return
        store.record_message(
            envelope_id=envelope.id,
            run_id=run_id,
            session_id=session_id,
            parent_id=envelope.parent_id,
            sender=envelope.sender,
            target=envelope.target,
            message_type=envelope.message_type,
            action_surface=envelope.action_surface,
            content=envelope.content,
            metadata=dict(envelope.metadata),
            created_at=envelope.created_at,
            phase=phase,
        )

    def _persist_planned_actions(validated: list[dict[str, Any]]) -> None:
        if store is None:
            return
        for spec in validated:
            store.record_action(
                run_id=run_id,
                session_id=session_id,
                step_id=int(spec["id"]),
                target=spec["target"],
                action_surface=spec["action_surface"],
                message=spec["message"],
                reason=spec["reason"],
                status=STEP_STATUS_PLANNED,
            )

    def _persist_step_result(step: SupervisorStep) -> None:
        if store is None:
            return
        # Persist the response envelope FIRST so the FK in actions.
        # response_envelope_id resolves at update time.
        if step.response_envelope is not None:
            _persist_envelope(step.response_envelope, phase=PHASE_STEP)
        store.update_action(
            run_id,
            step.id,
            status=step.status,
            response_envelope_id=(
                step.response_envelope.id
                if step.response_envelope is not None
                else None
            ),
            completed_at=step.completed_at,
        )

    def _persist_decision(record: dict[str, Any], *, kind: str) -> None:
        if store is None:
            return
        phase = str(record.get("phase", "unknown"))
        signed_message = record.get("signed_message")
        if not isinstance(signed_message, str) or not signed_message:
            return  # the addendum guarantees this; defensive guard only.
        text = record.get("reason") if kind == DECISION_KIND_BLOCKER else record.get("error")
        meta = {k: v for k, v in record.items()
                if k not in {"phase", "reason", "error", "signed_message"}}
        store.record_decision(
            run_id=run_id,
            session_id=session_id,
            kind=kind,
            phase=phase,
            signed_message=signed_message,
            reason_or_error=str(text) if text is not None else "",
            metadata=meta,
            created_at=_utc_now(),
        )

    def _persist_gate_if_pending(record: dict[str, Any]) -> None:
        if store is None:
            return
        if record.get("decision") != "pending-human-approval":
            return
        signed_message = record.get("signed_message")
        if not isinstance(signed_message, str) or not signed_message:
            return
        store.record_gate(
            run_id=run_id,
            session_id=session_id,
            step_id=record.get("step_id"),
            target=str(record.get("target", "unknown")),
            action_surface=ACTION_SURFACE_HUMAN_APPROVED_ONLY,
            signed_message=signed_message,
            metadata={k: v for k, v in record.items()
                      if k not in {"signed_message"}},
            created_at=_utc_now(),
        )

    # Statuses that represent a finished run lifecycle; non-terminal
    # statuses (planning/executing/finalizing) describe an in-flight or
    # crashed run and must NOT carry a completed_at timestamp — Atlas's
    # 4.6 review (comment 1215454084467171) flagged this as muddying the
    # operator semantics 4.8/4.11 depend on.
    _TERMINAL_STATUSES = frozenset(
        {
            SUPERVISOR_STATUS_COMPLETE,
            SUPERVISOR_STATUS_BLOCKED,
            SUPERVISOR_STATUS_ERRORED,
            SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
        }
    )

    def finalize(status: str) -> SupervisorRun:
        is_terminal = status in _TERMINAL_STATUSES
        completed_at = _utc_now() if is_terminal else None
        if store is not None:
            store.update_session(
                run_id,
                status=status,
                planner_envelope_id=(
                    planner_envelope.id if planner_envelope is not None else None
                ),
                finalizer_envelope_id=(
                    finalizer_envelope.id if finalizer_envelope is not None else None
                ),
                plan=plan,
                # Only stamp completed_at when the run truly finished.
                completed_at=completed_at if is_terminal else None,
                error_count=len(errors),
                blocker_count=len(blockers),
            )
        return _build_run(
            run_id=run_id,
            session_id=session_id,
            directive_summary=directive_summary,
            status=status,
            created_at=created_at,
            completed_at=completed_at,
            planner_envelope=planner_envelope,
            plan=plan,
            steps=steps,
            finalizer_envelope=finalizer_envelope,
            blockers=blockers,
            errors=errors,
            max_steps=max_steps_norm,
            dry_run=dry_run,
        )

    # --- Pre-flight: secrets check on the directive ---------------------------
    secrets_result = check_for_secrets(directive, actor="Atlas")
    if not secrets_result.allowed:
        record = _blocker_record(
            phase="preflight_secrets_check",
            reason=secrets_result.reason,
            secret_kinds=list(secrets_result.matches),
        )
        blockers.append(record)
        _persist_decision(record, kind=DECISION_KIND_BLOCKER)
        return finalize(SUPERVISOR_STATUS_BLOCKED)

    # --- Planner turn ---------------------------------------------------------
    try:
        planner_envelope = _plan_turn(
            directive,
            session_id,
            max_steps_norm,
            dry_run,
            planner_content_override=_dry_run_planner_content if dry_run else None,
        )
    except (UnknownAgentError, ProtocolError, MissingEnvError) as exc:
        record = _error_record(phase="planner_call", error=str(exc))
        errors.append(record)
        _persist_decision(record, kind=DECISION_KIND_ERROR)
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except ImportError as exc:
        record = _error_record(phase="planner_sdk", error=str(exc))
        errors.append(record)
        _persist_decision(record, kind=DECISION_KIND_ERROR)
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except TRANSIENT_PROVIDER_ERRORS as exc:
        # 4.7 step 1: wrap provider transient errors so a 529 or 429 never
        # surfaces as a raw traceback.
        record = _error_record(
            phase="planner_provider_transient",
            error=f"{type(exc).__name__}: {exc}",
            exception_class=type(exc).__name__,
            kind="transient_provider",
        )
        errors.append(record)
        _persist_decision(record, kind=DECISION_KIND_ERROR)
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except Exception as exc:  # noqa: BLE001 - last-resort: don't surface raw traceback
        record = _error_record(
            phase="planner_call", error=f"{type(exc).__name__}: {exc}"
        )
        errors.append(record)
        _persist_decision(record, kind=DECISION_KIND_ERROR)
        return finalize(SUPERVISOR_STATUS_ERRORED)

    _persist_envelope(planner_envelope, phase=PHASE_PLANNER)

    if planner_envelope.message_type == MESSAGE_TYPE_BLOCKER:
        record = _blocker_record(
            phase="planner_blocker",
            reason="planner returned a blocker envelope",
            envelope_id=planner_envelope.id,
            planner_envelope_metadata=dict(planner_envelope.metadata),
        )
        blockers.append(record)
        _persist_decision(record, kind=DECISION_KIND_BLOCKER)
        return finalize(SUPERVISOR_STATUS_BLOCKED)

    # --- Plan parse + validate -----------------------------------------------
    try:
        plan = _parse_plan(planner_envelope.content)
        validated_steps = _validate_plan(plan, max_steps_norm)
    except PlanError as exc:
        record = _blocker_record(
            phase="plan_parse_or_validate",
            reason=str(exc),
            planner_envelope_id=planner_envelope.id,
        )
        blockers.append(record)
        _persist_decision(record, kind=DECISION_KIND_BLOCKER)
        return finalize(SUPERVISOR_STATUS_BLOCKED)

    _persist_planned_actions(validated_steps)
    if _stop_after == "planner":
        # Crash simulation: pretend the process died right after the
        # planner phase persisted. Status remains `planning` so resume
        # picks up from the first un-executed step.
        if store is not None:
            store.update_session(
                run_id,
                planner_envelope_id=planner_envelope.id,
                plan=plan,
            )
        return finalize(SUPERVISOR_STATUS_PLANNING)

    # --- Step execution -------------------------------------------------------
    for step_spec in validated_steps:
        # 4.7 main scope: run any pre-step tool calls through the registry
        # before dispatching the subagent message. Each tool call is
        # already validated against agent/tool permissions in
        # `_validate_plan`; the registry re-checks at execute_tool() as
        # defense-in-depth and applies the full hook chain
        # (secrets → approval → execute → sign → persist).
        tool_summaries: list[str] = []
        tool_halt = False
        for tc in step_spec.get("tool_calls", []):
            tool_result = execute_tool(
                step_spec["target"],
                tc["tool_name"],
                tc["args"],
                run_id=run_id,
                session_id=session_id,
                step_id=int(step_spec["id"]),
                store=store,
                dry_run=dry_run,
            )
            tool_summaries.append(
                f"  - {tool_result.tool_name} ({tool_result.action_surface}) "
                f"→ {tool_result.status}: {tool_result.result_summary[:200]}"
            )
            if tool_result.status == TOOL_STATUS_OK:
                continue
            # Any non-ok outcome halts the supervisor with a signed
            # blocker/error decision. The tool_call row itself is already
            # persisted by execute_tool(); we add a matching decision
            # (and gate row when pending-human-approval) so the existing
            # decisions/gates surface 4.8/4.11 consumes stays uniform.
            if tool_result.status == TOOL_STATUS_PENDING_HUMAN_APPROVAL:
                record = _blocker_record(
                    phase=f"step_{step_spec['id']}_tool_pending_human_approval",
                    reason=tool_result.result_summary or "tool gated for human approval",
                    step_id=int(step_spec["id"]),
                    target=step_spec["target"],
                    tool_name=tool_result.tool_name,
                    tool_call_id=tool_result.tool_call_id,
                    decision="pending-human-approval",
                    signed_message=tool_result.signed_message,
                )
                blockers.append(record)
                _persist_decision(record, kind=DECISION_KIND_BLOCKER)
                _persist_gate_if_pending(record)
                tool_halt = True
                final_status = SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL
                break
            if tool_result.status == TOOL_STATUS_BLOCKED:
                record = _blocker_record(
                    phase=f"step_{step_spec['id']}_tool_blocked",
                    reason=tool_result.result_summary or "tool blocked by hook chain",
                    step_id=int(step_spec["id"]),
                    target=step_spec["target"],
                    tool_name=tool_result.tool_name,
                    tool_call_id=tool_result.tool_call_id,
                    signed_message=tool_result.signed_message,
                )
                blockers.append(record)
                _persist_decision(record, kind=DECISION_KIND_BLOCKER)
                tool_halt = True
                final_status = SUPERVISOR_STATUS_BLOCKED
                break
            # TOOL_STATUS_ERRORED
            record = _error_record(
                phase=f"step_{step_spec['id']}_tool_errored",
                error=tool_result.error or tool_result.result_summary
                or "tool executor errored",
                step_id=int(step_spec["id"]),
                target=step_spec["target"],
                tool_name=tool_result.tool_name,
                tool_call_id=tool_result.tool_call_id,
                signed_message=tool_result.signed_message,
            )
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            tool_halt = True
            final_status = SUPERVISOR_STATUS_ERRORED
            break

        if tool_halt:
            return finalize(final_status)

        # If any tool calls succeeded, fold a redacted signed-summary block
        # into the subagent's message so the LLM sees the tool outputs in
        # context. The summaries are already redacted by the registry.
        if tool_summaries and step_spec.get("tool_calls"):
            tool_block = "\n".join(
                [
                    "<orchestra_tool_results>",
                    *tool_summaries,
                    "</orchestra_tool_results>",
                ]
            )
            step_spec = dict(step_spec)
            step_spec["message"] = step_spec["message"] + "\n\n" + tool_block

        step = _execute_step(
            step_spec=step_spec,
            session_id=session_id,
            parent_id=planner_envelope.id,
            dry_run=dry_run,
            _force_exception=(
                _force_step_exception
                if _force_step_exception is not None
                and (
                    _force_step_target_id is None
                    or _force_step_target_id == step_spec["id"]
                )
                else None
            ),
        )
        steps.append(step)
        _persist_step_result(step)
        if step.status == STEP_STATUS_BLOCKED:
            decision = (
                step.response_envelope.metadata.get("decision")
                if step.response_envelope is not None
                else None
            )
            inner_phase = (
                step.response_envelope.metadata.get("blocker_phase")
                if step.response_envelope is not None
                else None
            )
            step_reason = (
                step.response_envelope.metadata.get("blocker_reason")
                if step.response_envelope is not None
                else "step blocked"
            )
            record = _blocker_record(
                phase=f"step_{step.id}_blocker",
                reason=step_reason or "step blocked",
                step_id=step.id,
                target=step.target,
                envelope_id=(
                    step.response_envelope.id
                    if step.response_envelope is not None
                    else None
                ),
                blocker_phase=inner_phase,
                decision=decision,
            )
            blockers.append(record)
            _persist_decision(record, kind=DECISION_KIND_BLOCKER)
            _persist_gate_if_pending(record)
            if decision == "pending-human-approval":
                return finalize(SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL)
            return finalize(SUPERVISOR_STATUS_BLOCKED)
        if step.status == STEP_STATUS_ERRORED:
            # Use the step's error_info when populated (4.7 step 1) to
            # name the actual exception class. Falls back to the legacy
            # generic message when error_info is absent (older code
            # paths or rehydrated steps).
            info = step.error_info or {}
            kind = str(info.get("kind", "runtime"))
            exc_class = info.get("exception_class")
            exc_message = info.get("message")
            if exc_class:
                error_text = (
                    f"step {step.id} {kind} error from {step.target} "
                    f"({exc_class}): {exc_message}"
                )
                phase = (
                    f"step_{step.id}_provider_transient"
                    if kind == "transient_provider"
                    else f"step_{step.id}_error"
                )
            else:
                error_text = "send_message raised during step execution"
                phase = f"step_{step.id}_error"
            record = _error_record(
                phase=phase,
                error=error_text,
                step_id=step.id,
                target=step.target,
                exception_class=exc_class,
                error_kind=kind,
            )
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return finalize(SUPERVISOR_STATUS_ERRORED)
        if _stop_after == f"step:{step.id}":
            # Crash simulation: process died after persisting this step's
            # response. Status remains `executing` so resume picks up from
            # the next step.
            if store is not None:
                store.update_session(
                    run_id,
                    planner_envelope_id=planner_envelope.id,
                    plan=plan,
                )
            return finalize(SUPERVISOR_STATUS_EXECUTING)

    # --- Finalizer turn ------------------------------------------------------
    try:
        finalizer_envelope = _finalize_turn(
            plan, tuple(steps), session_id, planner_envelope.id, dry_run
        )
    except (UnknownAgentError, ProtocolError, MissingEnvError) as exc:
        record = _error_record(phase="finalizer_call", error=str(exc))
        errors.append(record)
        _persist_decision(record, kind=DECISION_KIND_ERROR)
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except ImportError as exc:
        record = _error_record(phase="finalizer_sdk", error=str(exc))
        errors.append(record)
        _persist_decision(record, kind=DECISION_KIND_ERROR)
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except TRANSIENT_PROVIDER_ERRORS as exc:
        # 4.7 step 1: wrap provider transient errors for the finalizer too.
        record = _error_record(
            phase="finalizer_provider_transient",
            error=f"{type(exc).__name__}: {exc}",
            exception_class=type(exc).__name__,
            kind="transient_provider",
        )
        errors.append(record)
        _persist_decision(record, kind=DECISION_KIND_ERROR)
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except Exception as exc:  # noqa: BLE001
        record = _error_record(
            phase="finalizer_call", error=f"{type(exc).__name__}: {exc}"
        )
        errors.append(record)
        _persist_decision(record, kind=DECISION_KIND_ERROR)
        return finalize(SUPERVISOR_STATUS_ERRORED)

    _persist_envelope(finalizer_envelope, phase=PHASE_FINALIZER)

    if finalizer_envelope.message_type == MESSAGE_TYPE_BLOCKER:
        record = _blocker_record(
            phase="finalizer_blocker",
            reason="finalizer returned a blocker envelope",
            envelope_id=finalizer_envelope.id,
            finalizer_envelope_metadata=dict(finalizer_envelope.metadata),
        )
        blockers.append(record)
        _persist_decision(record, kind=DECISION_KIND_BLOCKER)
        return finalize(SUPERVISOR_STATUS_BLOCKED)

    return finalize(SUPERVISOR_STATUS_COMPLETE)


# --- Resume -----------------------------------------------------------------


def resume_supervisor(
    identifier: str,
    *,
    store: SessionStore,
    directive: str | None = None,
    dry_run: bool = False,
    _force_step_exception: BaseException | None = None,
    _force_step_target_id: int | None = None,
) -> SupervisorRun:
    """Resume a persisted supervisor run **in place** — same run_id.

    Implements the 4.6 directive §11 semantics that the original commit
    `9640802` missed (Atlas's review at Asana comment 1215454084467171):

    - **Terminal** (complete / blocked / errored / pending_human_approval):
      rehydrate via `load_supervisor_run` and return. No provider call.
    - **planning with no planner envelope**:
      - directive supplied: rerun the planner for the SAME run_id +
        session_id, persist the planner envelope and plan into the same
        session, and continue into step execution.
      - directive missing: persist a signed `resume_unsafe` blocker
        decision into the same session and return blocked.
    - **planning / executing with planner envelope + plan + some
      completed steps**: rehydrate the planned action rows, skip any
      step already `status=responded` with a response envelope, and
      execute the remaining steps in order into the same run_id.
    - **all steps responded, finalizer missing**: run only the
      finalizer turn into the same run_id, persist it, mark the
      session complete.
    - **pending human gate**: do not auto-continue. (The corresponding
      session state is `pending_human_approval`, which is terminal
      here; the gate row remains pending until 4.11's resolution API
      lands.)

    M4 is sequential-only; parallel resume is out of scope.

    Args:
        identifier: a stored `run_id` or `session_id`.
        store: the SessionStore the run was persisted to.
        directive: original directive text. Required to re-plan a
            session whose planner envelope is missing. Optional for
            terminal-state rehydration and step/finalizer continuation
            (the plan and step messages already on disk are the source
            of truth there).
        dry_run: when True, no provider calls are made — continuation
            uses the same synthetic-envelope path as
            `run_supervisor(dry_run=True)`.

    Returns:
        SupervisorRun: the resumed run's final state, persisted in
        place against the original run_id.

    Raises:
        SessionStoreError: when the identifier does not match a stored
            session.
    """
    from llm.types import MessageEnvelope  # local: avoid top-level cycle
    from session_store import SessionStoreError  # local: kept lightweight

    # 4.7 step 1 addendum: same dry-run-only enforcement as run_supervisor.
    if not dry_run and (
        _force_step_exception is not None or _force_step_target_id is not None
    ):
        raise ValueError(
            "resume_supervisor: `_force_step_exception` and "
            "`_force_step_target_id` are dry-run-only test hooks. "
            "Set `dry_run=True` to use them; live runs must not force "
            "synthetic exceptions."
        )

    load_env_file()
    persisted = store.load_session(identifier)
    if persisted is None:
        raise SessionStoreError(
            f"resume_supervisor: no session matching {identifier!r}"
        )

    _terminal = {
        SUPERVISOR_STATUS_COMPLETE,
        SUPERVISOR_STATUS_BLOCKED,
        SUPERVISOR_STATUS_ERRORED,
        SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
    }
    if persisted.status in _terminal:
        run = store.load_supervisor_run(identifier)
        if run is None:  # pragma: no cover
            raise SessionStoreError(
                f"resume_supervisor: load_supervisor_run({identifier!r}) failed"
            )
        return run

    # --- Non-terminal: in-place continuation ----------------------------
    run_id = persisted.run_id
    session_id = persisted.session_id
    max_steps_norm = persisted.max_steps

    blockers: list[dict[str, Any]] = []  # newly produced this resume
    errors: list[dict[str, Any]] = []

    # Rehydrate any envelopes already on disk.
    msg_rows = store.load_messages(run_id)
    msg_by_id = {m["id"]: m for m in msg_rows}

    def _env_from_row(row: dict[str, Any]) -> MessageEnvelope:
        return MessageEnvelope(
            id=row["id"],
            session_id=row["session_id"],
            sender=row["sender"],
            target=row["target"],
            message_type=row["message_type"],
            content=row["content"],
            action_surface=row["action_surface"],
            parent_id=row["parent_id"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=datetime.fromisoformat(
                row["created_at"].rstrip("Z")
            ).replace(tzinfo=UTC),
        )

    planner_envelope: MessageEnvelope | None = None
    if persisted.planner_envelope_id and persisted.planner_envelope_id in msg_by_id:
        planner_envelope = _env_from_row(msg_by_id[persisted.planner_envelope_id])
    finalizer_envelope: MessageEnvelope | None = None
    if persisted.finalizer_envelope_id and persisted.finalizer_envelope_id in msg_by_id:
        finalizer_envelope = _env_from_row(msg_by_id[persisted.finalizer_envelope_id])
    plan = persisted.plan

    # --- Persistence helpers (in-place; share run_id/session_id) -------
    def _persist_envelope(env: MessageEnvelope, *, phase: str) -> None:
        store.record_message(
            envelope_id=env.id,
            run_id=run_id,
            session_id=session_id,
            parent_id=env.parent_id,
            sender=env.sender,
            target=env.target,
            message_type=env.message_type,
            action_surface=env.action_surface,
            content=env.content,
            metadata=dict(env.metadata),
            created_at=env.created_at,
            phase=phase,
        )

    def _persist_decision(record: dict[str, Any], *, kind: str) -> None:
        phase = str(record.get("phase", "unknown"))
        signed_message = record.get("signed_message")
        if not isinstance(signed_message, str) or not signed_message:
            return
        text = record.get("reason") if kind == DECISION_KIND_BLOCKER else record.get("error")
        meta = {k: v for k, v in record.items()
                if k not in {"phase", "reason", "error", "signed_message"}}
        store.record_decision(
            run_id=run_id,
            session_id=session_id,
            kind=kind,
            phase=phase,
            signed_message=signed_message,
            reason_or_error=str(text) if text is not None else "",
            metadata=meta,
            created_at=_utc_now(),
        )

    def _persist_gate_if_pending(record: dict[str, Any]) -> None:
        if record.get("decision") != "pending-human-approval":
            return
        signed_message = record.get("signed_message")
        if not isinstance(signed_message, str) or not signed_message:
            return
        store.record_gate(
            run_id=run_id,
            session_id=session_id,
            step_id=record.get("step_id"),
            target=str(record.get("target", "unknown")),
            action_surface=ACTION_SURFACE_HUMAN_APPROVED_ONLY,
            signed_message=signed_message,
            metadata={k: v for k, v in record.items() if k != "signed_message"},
            created_at=_utc_now(),
        )

    def _finalize_in_place(status: str) -> SupervisorRun:
        is_terminal = status in _terminal
        completed_at = _utc_now() if is_terminal else None
        store.update_session(
            run_id,
            status=status,
            planner_envelope_id=(
                planner_envelope.id if planner_envelope is not None else None
            ),
            finalizer_envelope_id=(
                finalizer_envelope.id if finalizer_envelope is not None else None
            ),
            plan=plan,
            completed_at=completed_at if is_terminal else None,
            error_count=persisted.error_count + len(errors),
            blocker_count=persisted.blocker_count + len(blockers),
        )
        rehydrated = store.load_supervisor_run(identifier)
        if rehydrated is None:  # pragma: no cover
            raise SessionStoreError(
                f"resume_supervisor: re-load failed for {identifier!r}"
            )
        return rehydrated

    # --- Branch: planner envelope or plan missing ----------------------
    if planner_envelope is None or plan is None:
        if directive is None:
            signed = sign_action(
                "Atlas",
                f"Supervisor blocker [resume_unsafe]: cannot resume run "
                f"{run_id} (status={persisted.status}) without the original "
                "directive; redacted summary is insufficient to safely re-plan.",
            )
            record = {
                "phase": "resume_unsafe",
                "reason": "missing original directive",
                "signed_message": signed,
                "prior_status": persisted.status,
            }
            blockers.append(record)
            _persist_decision(record, kind=DECISION_KIND_BLOCKER)
            return _finalize_in_place(SUPERVISOR_STATUS_BLOCKED)

        # Re-plan into the same run_id/session_id.
        secrets_result = check_for_secrets(directive, actor="Atlas")
        if not secrets_result.allowed:
            record = _blocker_record(
                phase="preflight_secrets_check",
                reason=secrets_result.reason,
                secret_kinds=list(secrets_result.matches),
            )
            blockers.append(record)
            _persist_decision(record, kind=DECISION_KIND_BLOCKER)
            return _finalize_in_place(SUPERVISOR_STATUS_BLOCKED)

        try:
            planner_envelope = _plan_turn(
                directive, session_id, max_steps_norm, dry_run
            )
        except (UnknownAgentError, ProtocolError, MissingEnvError) as exc:
            record = _error_record(phase="planner_call", error=str(exc))
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)
        except ImportError as exc:
            record = _error_record(phase="planner_sdk", error=str(exc))
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)
        except TRANSIENT_PROVIDER_ERRORS as exc:
            record = _error_record(
                phase="planner_provider_transient",
                error=f"{type(exc).__name__}: {exc}",
                exception_class=type(exc).__name__,
                kind="transient_provider",
            )
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)
        except Exception as exc:  # noqa: BLE001
            record = _error_record(
                phase="planner_call", error=f"{type(exc).__name__}: {exc}"
            )
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)

        _persist_envelope(planner_envelope, phase=PHASE_PLANNER)

        if planner_envelope.message_type == MESSAGE_TYPE_BLOCKER:
            record = _blocker_record(
                phase="planner_blocker",
                reason="planner returned a blocker envelope",
                envelope_id=planner_envelope.id,
                planner_envelope_metadata=dict(planner_envelope.metadata),
            )
            blockers.append(record)
            _persist_decision(record, kind=DECISION_KIND_BLOCKER)
            return _finalize_in_place(SUPERVISOR_STATUS_BLOCKED)

        try:
            plan = _parse_plan(planner_envelope.content)
            validated_steps = _validate_plan(plan, max_steps_norm)
        except PlanError as exc:
            record = _blocker_record(
                phase="plan_parse_or_validate",
                reason=str(exc),
                planner_envelope_id=planner_envelope.id,
            )
            blockers.append(record)
            _persist_decision(record, kind=DECISION_KIND_BLOCKER)
            return _finalize_in_place(SUPERVISOR_STATUS_BLOCKED)

        # Persist planned actions for the same run_id.
        for spec in validated_steps:
            store.record_action(
                run_id=run_id,
                session_id=session_id,
                step_id=int(spec["id"]),
                target=spec["target"],
                action_surface=spec["action_surface"],
                message=spec["message"],
                reason=spec["reason"],
                status=STEP_STATUS_PLANNED,
            )

    # --- At this point: planner_envelope + plan exist (rehydrated or fresh) ---
    # Load the canonical action rows (rehydrated state from disk, then any
    # planned actions just inserted).
    action_rows = store.load_actions(run_id)
    # Build step specs ordered by step_id.
    step_specs = sorted(action_rows, key=lambda a: int(a["step_id"]))

    # Build a step_id → plan_step lookup so we can rehydrate tool_calls
    # for any not-yet-responded step. The plan JSON is the authoritative
    # source; `actions` rows don't carry tool_calls.
    plan_steps_by_id: dict[int, dict[str, Any]] = {}
    if isinstance(plan, dict):
        for s in plan.get("steps", []) or []:
            try:
                plan_steps_by_id[int(s["id"])] = s
            except (KeyError, TypeError, ValueError):
                continue

    # Execute steps whose status is not already `responded`.
    for action in step_specs:
        step_status = action["status"]
        if step_status == STEP_STATUS_RESPONDED:
            continue  # already done — skip; do not re-call provider
        spec = {
            "id": int(action["step_id"]),
            "target": action["target"],
            "message": action["message"],
            "action_surface": action["action_surface"],
            "reason": action["reason"],
        }
        plan_step = plan_steps_by_id.get(int(action["step_id"]))
        rehydrated_tool_calls: list[dict[str, Any]] = []
        if isinstance(plan_step, dict):
            for tc in plan_step.get("tool_calls", []) or []:
                if (
                    isinstance(tc, dict)
                    and isinstance(tc.get("tool_name"), str)
                    and tc["tool_name"] in TOOL_REGISTRY
                    and isinstance(tc.get("args", {}), dict)
                    and is_agent_allowed(spec["target"], tc["tool_name"])
                ):
                    rehydrated_tool_calls.append(
                        {"tool_name": tc["tool_name"], "args": tc.get("args", {})}
                    )
        # 4.7 main scope: replay tool calls on resume too. Same hook chain.
        tool_summaries: list[str] = []
        tool_halt = False
        for tc in rehydrated_tool_calls:
            tool_result = execute_tool(
                spec["target"],
                tc["tool_name"],
                tc["args"],
                run_id=run_id,
                session_id=session_id,
                step_id=int(spec["id"]),
                store=store,
                dry_run=dry_run,
            )
            tool_summaries.append(
                f"  - {tool_result.tool_name} ({tool_result.action_surface}) "
                f"→ {tool_result.status}: {tool_result.result_summary[:200]}"
            )
            if tool_result.status == TOOL_STATUS_OK:
                continue
            if tool_result.status == TOOL_STATUS_PENDING_HUMAN_APPROVAL:
                record = _blocker_record(
                    phase=f"step_{spec['id']}_tool_pending_human_approval",
                    reason=tool_result.result_summary
                    or "tool gated for human approval",
                    step_id=int(spec["id"]),
                    target=spec["target"],
                    tool_name=tool_result.tool_name,
                    tool_call_id=tool_result.tool_call_id,
                    decision="pending-human-approval",
                    signed_message=tool_result.signed_message,
                )
                blockers.append(record)
                _persist_decision(record, kind=DECISION_KIND_BLOCKER)
                _persist_gate_if_pending(record)
                tool_halt = True
                resume_tool_halt_status = SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL
                break
            if tool_result.status == TOOL_STATUS_BLOCKED:
                record = _blocker_record(
                    phase=f"step_{spec['id']}_tool_blocked",
                    reason=tool_result.result_summary
                    or "tool blocked by hook chain",
                    step_id=int(spec["id"]),
                    target=spec["target"],
                    tool_name=tool_result.tool_name,
                    tool_call_id=tool_result.tool_call_id,
                    signed_message=tool_result.signed_message,
                )
                blockers.append(record)
                _persist_decision(record, kind=DECISION_KIND_BLOCKER)
                tool_halt = True
                resume_tool_halt_status = SUPERVISOR_STATUS_BLOCKED
                break
            record = _error_record(
                phase=f"step_{spec['id']}_tool_errored",
                error=tool_result.error
                or tool_result.result_summary
                or "tool executor errored",
                step_id=int(spec["id"]),
                target=spec["target"],
                tool_name=tool_result.tool_name,
                tool_call_id=tool_result.tool_call_id,
                signed_message=tool_result.signed_message,
            )
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            tool_halt = True
            resume_tool_halt_status = SUPERVISOR_STATUS_ERRORED
            break
        if tool_halt:
            return _finalize_in_place(resume_tool_halt_status)
        if tool_summaries:
            tool_block = "\n".join(
                [
                    "<orchestra_tool_results>",
                    *tool_summaries,
                    "</orchestra_tool_results>",
                ]
            )
            spec["message"] = spec["message"] + "\n\n" + tool_block

        new_step = _execute_step(
            step_spec=spec,
            session_id=session_id,
            parent_id=planner_envelope.id,
            dry_run=dry_run,
            _force_exception=(
                _force_step_exception
                if _force_step_exception is not None
                and (
                    _force_step_target_id is None
                    or _force_step_target_id == int(action["step_id"])
                )
                else None
            ),
        )
        if new_step.response_envelope is not None:
            _persist_envelope(new_step.response_envelope, phase=PHASE_STEP)
        store.update_action(
            run_id,
            new_step.id,
            status=new_step.status,
            response_envelope_id=(
                new_step.response_envelope.id
                if new_step.response_envelope is not None
                else None
            ),
            completed_at=new_step.completed_at,
        )
        if new_step.status == STEP_STATUS_BLOCKED:
            decision = (
                new_step.response_envelope.metadata.get("decision")
                if new_step.response_envelope is not None
                else None
            )
            inner_phase = (
                new_step.response_envelope.metadata.get("blocker_phase")
                if new_step.response_envelope is not None
                else None
            )
            step_reason = (
                new_step.response_envelope.metadata.get("blocker_reason")
                if new_step.response_envelope is not None
                else "step blocked"
            )
            record = _blocker_record(
                phase=f"step_{new_step.id}_blocker",
                reason=step_reason or "step blocked",
                step_id=new_step.id,
                target=new_step.target,
                envelope_id=(
                    new_step.response_envelope.id
                    if new_step.response_envelope is not None
                    else None
                ),
                blocker_phase=inner_phase,
                decision=decision,
            )
            blockers.append(record)
            _persist_decision(record, kind=DECISION_KIND_BLOCKER)
            _persist_gate_if_pending(record)
            if decision == "pending-human-approval":
                return _finalize_in_place(SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL)
            return _finalize_in_place(SUPERVISOR_STATUS_BLOCKED)
        if new_step.status == STEP_STATUS_ERRORED:
            # Mirror run_supervisor()'s step-error consumption (4.7 step 1
            # addendum): preserve `new_step.error_info` so a resumed
            # step that hits a provider transient (anthropic 529 / openai
            # 429) is persisted under phase=step_{N}_provider_transient
            # with exception_class + error_kind metadata, not the generic
            # step_{N}_error line. Atlas review 1215456686905326.
            info = new_step.error_info or {}
            kind = str(info.get("kind", "runtime"))
            exc_class = info.get("exception_class")
            exc_message = info.get("message")
            if exc_class:
                error_text = (
                    f"step {new_step.id} {kind} error from {new_step.target} "
                    f"({exc_class}): {exc_message}"
                )
                phase = (
                    f"step_{new_step.id}_provider_transient"
                    if kind == "transient_provider"
                    else f"step_{new_step.id}_error"
                )
            else:
                error_text = "send_message raised during step execution"
                phase = f"step_{new_step.id}_error"
            record = _error_record(
                phase=phase,
                error=error_text,
                step_id=new_step.id,
                target=new_step.target,
                exception_class=exc_class,
                error_kind=kind,
            )
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)

    # --- Finalizer-only path ------------------------------------------
    if finalizer_envelope is None:
        # Build rehydrated step tuple for the finalizer prompt transcript.
        rehydrated_run = store.load_supervisor_run(identifier)
        rehydrated_steps = (
            rehydrated_run.steps if rehydrated_run is not None else tuple()
        )
        try:
            finalizer_envelope = _finalize_turn(
                plan, rehydrated_steps, session_id, planner_envelope.id, dry_run
            )
        except (UnknownAgentError, ProtocolError, MissingEnvError) as exc:
            record = _error_record(phase="finalizer_call", error=str(exc))
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)
        except ImportError as exc:
            record = _error_record(phase="finalizer_sdk", error=str(exc))
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)
        except TRANSIENT_PROVIDER_ERRORS as exc:
            record = _error_record(
                phase="finalizer_provider_transient",
                error=f"{type(exc).__name__}: {exc}",
                exception_class=type(exc).__name__,
                kind="transient_provider",
            )
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)
        except Exception as exc:  # noqa: BLE001
            record = _error_record(
                phase="finalizer_call", error=f"{type(exc).__name__}: {exc}"
            )
            errors.append(record)
            _persist_decision(record, kind=DECISION_KIND_ERROR)
            return _finalize_in_place(SUPERVISOR_STATUS_ERRORED)

        _persist_envelope(finalizer_envelope, phase=PHASE_FINALIZER)

        if finalizer_envelope.message_type == MESSAGE_TYPE_BLOCKER:
            record = _blocker_record(
                phase="finalizer_blocker",
                reason="finalizer returned a blocker envelope",
                envelope_id=finalizer_envelope.id,
                finalizer_envelope_metadata=dict(finalizer_envelope.metadata),
            )
            blockers.append(record)
            _persist_decision(record, kind=DECISION_KIND_BLOCKER)
            return _finalize_in_place(SUPERVISOR_STATUS_BLOCKED)

    return _finalize_in_place(SUPERVISOR_STATUS_COMPLETE)


# --- Dry-run + CLI ----------------------------------------------------------


def _supervisor_dry_run() -> int:
    """Exercise the supervisor without any provider API calls.

    Scenarios fall into two groups:

    (1) Unit-level halts (1–11): happy-path; pre-flight secrets block;
        direct `_parse_plan` / `_validate_plan` failures (no block,
        malformed JSON, unknown target, Atlas-as-subagent, invalid
        surface, max-step overflow, empty steps, missing field); single
        `_execute_step` against human-approved-only.

    (2) Signed-halt-message scenarios (12–18, added in the 4.5 addendum):
        each drives a full `run_supervisor(..., dry_run=True,
        _dry_run_planner_content=...)` through a supervisor-owned halt
        path and asserts the resulting blocker carries an Atlas-signed
        `signed_message` line starting with `[Atlas · `. These cover
        preflight-secrets, plan-no-block, plan-bad-json,
        plan-unknown-target, plan-invalid-surface,
        plan-max-step-overflow, and step-human-approved.

    Emits one signed Cody line per scenario.
    """
    passes: list[str] = []
    failures: list[str] = []

    def record(case: str, ok: bool, detail: str) -> None:
        (passes if ok else failures).append(f"{case}: {detail}")

    # Scenario 1: happy path
    try:
        run = run_supervisor("Acknowledge supervisor dry-run.", dry_run=True)
        ok = (
            run.status == SUPERVISOR_STATUS_COMPLETE
            and run.planner_envelope is not None
            and run.finalizer_envelope is not None
            and len(run.steps) == 1
            and run.steps[0].target == "Cody"
            and run.steps[0].status == STEP_STATUS_RESPONDED
            and not run.blockers
            and not run.errors
        )
        record("happy-path", ok, "complete with planner+step+finalizer envelopes" if ok else f"unexpected run: {run.status}, steps={len(run.steps)}, blockers={run.blockers}, errors={run.errors}")
    except Exception as exc:  # noqa: BLE001
        record("happy-path", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 2: secrets in directive → blocker before planner
    try:
        run = run_supervisor(
            "Use this key: sk-proj-AAAAAAAAAAAAAAAAAAAA",
            dry_run=True,
        )
        ok = (
            run.status == SUPERVISOR_STATUS_BLOCKED
            and run.planner_envelope is None
            and not run.steps
            and any(b.get("phase") == "preflight_secrets_check" for b in run.blockers)
        )
        record("secrets-in-directive", ok, "preflight blocks before planner call" if ok else f"unexpected: {run.status}, blockers={run.blockers}")
    except Exception as exc:  # noqa: BLE001
        record("secrets-in-directive", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 3: invalid planner JSON via _parse_plan
    try:
        _parse_plan("Atlas says: this is not JSON inside a <foo/> block.")
        record("invalid-plan-no-block", False, "did NOT raise PlanError")
    except PlanError:
        record("invalid-plan-no-block", True, "PlanError raised for missing block")
    except Exception as exc:  # noqa: BLE001
        record("invalid-plan-no-block", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 4: malformed JSON inside the block
    try:
        _parse_plan("<orchestra_plan>{this is not json}</orchestra_plan>")
        record("invalid-plan-bad-json", False, "did NOT raise PlanError")
    except PlanError:
        record("invalid-plan-bad-json", True, "PlanError raised for malformed JSON")
    except Exception as exc:  # noqa: BLE001
        record("invalid-plan-bad-json", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 5: unknown target in plan
    try:
        _validate_plan(
            {
                "summary": "x",
                "steps": [
                    {"id": 1, "target": "Sentinel", "message": "m", "action_surface": "safe", "reason": "r"}
                ],
            },
            5,
        )
        record("plan-unknown-target", False, "did NOT raise PlanError")
    except PlanError as exc:
        ok = "Sentinel" in str(exc) or "unknown" in str(exc).lower() or "not allowed" in str(exc).lower()
        record("plan-unknown-target", ok, "PlanError raised for unknown target" if ok else f"unexpected message: {exc}")

    # Scenario 6: Atlas as subagent target (allowed agent, but not a valid subagent target)
    try:
        _validate_plan(
            {
                "summary": "x",
                "steps": [
                    {"id": 1, "target": "Atlas", "message": "m", "action_surface": "safe", "reason": "r"}
                ],
            },
            5,
        )
        record("plan-atlas-as-subagent", False, "did NOT raise PlanError")
    except PlanError:
        record("plan-atlas-as-subagent", True, "PlanError raised — Atlas not allowed as subagent target")

    # Scenario 7: invalid action_surface
    try:
        _validate_plan(
            {
                "summary": "x",
                "steps": [
                    {"id": 1, "target": "Cody", "message": "m", "action_surface": "invalid", "reason": "r"}
                ],
            },
            5,
        )
        record("plan-invalid-surface", False, "did NOT raise PlanError")
    except PlanError:
        record("plan-invalid-surface", True, "PlanError raised for invalid action_surface")

    # Scenario 8: max-step overflow
    try:
        _validate_plan(
            {
                "summary": "x",
                "steps": [
                    {"id": i, "target": "Cody", "message": "m", "action_surface": "safe", "reason": "r"}
                    for i in range(1, 7)
                ],
            },
            5,
        )
        record("plan-max-step-overflow", False, "did NOT raise PlanError")
    except PlanError:
        record("plan-max-step-overflow", True, "PlanError raised — 6 steps > max_steps=5")

    # Scenario 9: empty steps list
    try:
        _validate_plan({"summary": "x", "steps": []}, 5)
        record("plan-empty-steps", False, "did NOT raise PlanError")
    except PlanError:
        record("plan-empty-steps", True, "PlanError raised — zero steps")

    # Scenario 10: missing required field
    try:
        _validate_plan(
            {
                "summary": "x",
                "steps": [{"id": 1, "target": "Cody", "message": "m"}],  # missing action_surface, reason
            },
            5,
        )
        record("plan-missing-field", False, "did NOT raise PlanError")
    except PlanError:
        record("plan-missing-field", True, "PlanError raised — missing required fields")

    # Scenario 11: pending-human-approval step halts the run
    try:
        spec = {
            "id": 1,
            "target": "Cody",
            "message": "Test human-approved-only step.",
            "action_surface": ACTION_SURFACE_HUMAN_APPROVED_ONLY,
            "reason": "Surface test",
        }
        step = _execute_step(
            step_spec=spec,
            session_id=_new_id(),
            parent_id=_new_id(),
            dry_run=True,
        )
        ok = (
            step.status == STEP_STATUS_BLOCKED
            and step.response_envelope is not None
            and step.response_envelope.metadata.get("decision") == "pending-human-approval"
        )
        record("step-human-approved-only", ok, "step blocked with pending-human-approval" if ok else f"unexpected step: {step}")
    except Exception as exc:  # noqa: BLE001
        record("step-human-approved-only", False, f"raised {type(exc).__name__}: {exc}")

    # --- Signed-halt-message scenarios (4.5 addendum) -----------------------
    # Each scenario drives a full run_supervisor() through a supervisor-owned
    # halt path and asserts that the resulting blocker / error record carries
    # an Atlas-signed `signed_message` line. The signed-line property is what
    # 4.6 persistence will rely on; verifying it here keeps the property
    # under regression coverage without any provider API call.

    def _signed_line_ok(record: dict[str, Any], expected_phase: str) -> tuple[bool, str]:
        if not record:
            return False, "no record"
        if record.get("phase") != expected_phase:
            return False, f"phase={record.get('phase')!r} (expected {expected_phase!r})"
        sig = record.get("signed_message", "")
        if not isinstance(sig, str) or not sig.startswith("[Atlas · "):
            return False, f"signed_message missing or wrong format: {sig!r}"
        return True, sig.split("]")[0] + "]"

    # Scenario 12: signed-preflight-secrets
    try:
        run = run_supervisor(
            "Use this key: sk-proj-AAAAAAAAAAAAAAAAAAAA",
            dry_run=True,
        )
        ok = (
            run.status == SUPERVISOR_STATUS_BLOCKED
            and len(run.blockers) == 1
        )
        if ok:
            sig_ok, sig_detail = _signed_line_ok(run.blockers[0], "preflight_secrets_check")
            ok = sig_ok
            detail = f"signed preflight blocker present ({sig_detail})" if ok else f"unsigned: {sig_detail}"
        else:
            detail = f"unexpected: status={run.status}, blockers={run.blockers}"
        record("signed-preflight-secrets", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("signed-preflight-secrets", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 13: signed-plan-no-block — planner output has no <orchestra_plan> block
    try:
        run = run_supervisor(
            "Smoke test the no-block path.",
            dry_run=True,
            _dry_run_planner_content="Atlas narrative without a plan block.",
        )
        ok = (
            run.status == SUPERVISOR_STATUS_BLOCKED
            and len(run.blockers) == 1
        )
        if ok:
            sig_ok, sig_detail = _signed_line_ok(run.blockers[0], "plan_parse_or_validate")
            ok = sig_ok
            detail = f"signed parse blocker present ({sig_detail})" if ok else f"unsigned: {sig_detail}"
        else:
            detail = f"unexpected: status={run.status}, blockers={run.blockers}"
        record("signed-plan-no-block", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("signed-plan-no-block", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 14: signed-plan-bad-json — block present but JSON malformed
    try:
        run = run_supervisor(
            "Smoke test the bad-json path.",
            dry_run=True,
            _dry_run_planner_content="<orchestra_plan>{not valid json}</orchestra_plan>",
        )
        ok = (
            run.status == SUPERVISOR_STATUS_BLOCKED
            and len(run.blockers) == 1
        )
        if ok:
            sig_ok, sig_detail = _signed_line_ok(run.blockers[0], "plan_parse_or_validate")
            ok = sig_ok
            detail = f"signed parse blocker present ({sig_detail})" if ok else f"unsigned: {sig_detail}"
        else:
            detail = f"unexpected: status={run.status}, blockers={run.blockers}"
        record("signed-plan-bad-json", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("signed-plan-bad-json", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 15: signed-plan-unknown-target — valid JSON, unknown subagent
    try:
        bad_plan = {
            "summary": "x",
            "steps": [
                {"id": 1, "target": "Sentinel", "message": "m",
                 "action_surface": ACTION_SURFACE_SAFE, "reason": "r"}
            ],
            "final_response_instruction": "n/a",
        }
        payload = f"<orchestra_plan>{json.dumps(bad_plan)}</orchestra_plan>"
        run = run_supervisor(
            "Smoke test the unknown-target path.",
            dry_run=True,
            _dry_run_planner_content=payload,
        )
        ok = (
            run.status == SUPERVISOR_STATUS_BLOCKED
            and len(run.blockers) == 1
        )
        if ok:
            sig_ok, sig_detail = _signed_line_ok(run.blockers[0], "plan_parse_or_validate")
            ok = sig_ok and "Sentinel" in run.blockers[0].get("reason", "")
            detail = f"signed unknown-target blocker present ({sig_detail})" if ok else f"unsigned or wrong reason: {run.blockers[0]}"
        else:
            detail = f"unexpected: status={run.status}, blockers={run.blockers}"
        record("signed-plan-unknown-target", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("signed-plan-unknown-target", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 16: signed-plan-invalid-surface
    try:
        bad_plan = {
            "summary": "x",
            "steps": [
                {"id": 1, "target": "Cody", "message": "m",
                 "action_surface": "not-a-real-surface", "reason": "r"}
            ],
            "final_response_instruction": "n/a",
        }
        payload = f"<orchestra_plan>{json.dumps(bad_plan)}</orchestra_plan>"
        run = run_supervisor(
            "Smoke test the invalid-surface path.",
            dry_run=True,
            _dry_run_planner_content=payload,
        )
        ok = (
            run.status == SUPERVISOR_STATUS_BLOCKED
            and len(run.blockers) == 1
        )
        if ok:
            sig_ok, sig_detail = _signed_line_ok(run.blockers[0], "plan_parse_or_validate")
            ok = sig_ok and "action_surface" in run.blockers[0].get("reason", "")
            detail = f"signed invalid-surface blocker present ({sig_detail})" if ok else f"unsigned or wrong reason: {run.blockers[0]}"
        else:
            detail = f"unexpected: status={run.status}, blockers={run.blockers}"
        record("signed-plan-invalid-surface", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("signed-plan-invalid-surface", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 17: signed-plan-max-step-overflow
    try:
        bad_plan = {
            "summary": "x",
            "steps": [
                {"id": i, "target": "Cody", "message": "m",
                 "action_surface": ACTION_SURFACE_SAFE, "reason": "r"}
                for i in range(1, 8)  # 7 steps > default max 5
            ],
            "final_response_instruction": "n/a",
        }
        payload = f"<orchestra_plan>{json.dumps(bad_plan)}</orchestra_plan>"
        run = run_supervisor(
            "Smoke test the max-step-overflow path.",
            dry_run=True,
            _dry_run_planner_content=payload,
            max_steps=5,
        )
        ok = (
            run.status == SUPERVISOR_STATUS_BLOCKED
            and len(run.blockers) == 1
        )
        if ok:
            sig_ok, sig_detail = _signed_line_ok(run.blockers[0], "plan_parse_or_validate")
            ok = sig_ok and "exceeds max_steps" in run.blockers[0].get("reason", "")
            detail = f"signed overflow blocker present ({sig_detail})" if ok else f"unsigned or wrong reason: {run.blockers[0]}"
        else:
            detail = f"unexpected: status={run.status}, blockers={run.blockers}"
        record("signed-plan-max-step-overflow", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("signed-plan-max-step-overflow", False, f"raised {type(exc).__name__}: {exc}")

    # Scenario 18: signed-step-human-approved — full run halts pending approval
    try:
        gated_plan = {
            "summary": "x",
            "steps": [
                {"id": 1, "target": "Cody", "message": "Do the gated thing.",
                 "action_surface": ACTION_SURFACE_HUMAN_APPROVED_ONLY,
                 "reason": "Surface gate test."}
            ],
            "final_response_instruction": "n/a",
        }
        payload = f"<orchestra_plan>{json.dumps(gated_plan)}</orchestra_plan>"
        run = run_supervisor(
            "Smoke test the human-approval gate path.",
            dry_run=True,
            _dry_run_planner_content=payload,
        )
        ok = (
            run.status == SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL
            and len(run.blockers) == 1
            and len(run.steps) == 1
            and run.steps[0].status == STEP_STATUS_BLOCKED
        )
        if ok:
            sig_ok, sig_detail = _signed_line_ok(run.blockers[0], "step_1_blocker")
            decision_ok = run.blockers[0].get("decision") == "pending-human-approval"
            ok = sig_ok and decision_ok
            detail = (
                f"signed step blocker + pending-human-approval ({sig_detail})"
                if ok
                else f"unsigned or missing decision: {run.blockers[0]}"
            )
        else:
            detail = f"unexpected: status={run.status}, steps={[s.status for s in run.steps]}, blockers={run.blockers}"
        record("signed-step-human-approved", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("signed-step-human-approved", False, f"raised {type(exc).__name__}: {exc}")

    # --- Persisted-mode scenarios (4.6) -------------------------------------
    # Each scenario drives a full run_supervisor(..., store=...) through a
    # fresh SQLite DB under a tempfile.TemporaryDirectory() and asserts the
    # right rows landed. These prove 4.6 persistence works end-to-end without
    # any provider API call.
    import tempfile  # noqa: PLC0415 - local: only needed for 4.6 scenarios

    with tempfile.TemporaryDirectory(prefix="orchestra-sup-persist-test-") as tmp:
        tmp_path = Path(tmp)

        def fresh_store(label: str) -> SessionStore:
            return SessionStore(tmp_path / f"{label}.db")

        # 19. persisted-happy-path
        try:
            store_h = fresh_store("happy")
            run_h = run_supervisor(
                "Persisted happy path smoke.", dry_run=True, store=store_h
            )
            sessions = store_h.list_sessions()
            messages = store_h.load_messages(run_h.run_id)
            actions = store_h.load_actions(run_h.run_id)
            decisions = store_h.load_decisions(run_h.run_id)
            ok = (
                run_h.status == SUPERVISOR_STATUS_COMPLETE
                and len(sessions) == 1
                and sessions[0].run_id == run_h.run_id
                and sessions[0].status == SUPERVISOR_STATUS_COMPLETE
                and len(messages) == 3  # planner + step + finalizer
                and len(actions) == 1
                and actions[0]["status"] == STEP_STATUS_RESPONDED
                and not decisions
            )
            detail = (
                f"sessions=1, messages={len(messages)}, actions={len(actions)}, "
                f"decisions={len(decisions)}"
                if ok
                else f"unexpected: status={run_h.status}, msgs={len(messages)}, "
                f"actions={len(actions)}, decisions={len(decisions)}"
            )
            record("persisted-happy-path", ok, detail)
        except Exception as exc:  # noqa: BLE001
            record("persisted-happy-path", False, f"raised {type(exc).__name__}: {exc}")

        # 20. persisted-secrets-block — preflight signed decision row exists
        try:
            store_s = fresh_store("secrets")
            run_s = run_supervisor(
                "Block this: sk-proj-AAAAAAAAAAAAAAAAAAAA",
                dry_run=True,
                store=store_s,
            )
            decisions = store_s.load_decisions(run_s.run_id)
            ok = (
                run_s.status == SUPERVISOR_STATUS_BLOCKED
                and len(decisions) == 1
                and decisions[0]["kind"] == DECISION_KIND_BLOCKER
                and decisions[0]["phase"] == "preflight_secrets_check"
                and decisions[0]["signed_message"].startswith("[Atlas · ")
            )
            record(
                "persisted-secrets-block",
                ok,
                "signed preflight decision row persisted"
                if ok
                else f"unexpected: status={run_s.status}, decisions={decisions}",
            )
        except Exception as exc:  # noqa: BLE001
            record("persisted-secrets-block", False, f"raised {type(exc).__name__}: {exc}")

        # 21. persisted-plan-validation-block — signed decision for invalid plan
        try:
            bad_plan = {
                "summary": "x",
                "steps": [
                    {"id": 1, "target": "Sentinel", "message": "m",
                     "action_surface": ACTION_SURFACE_SAFE, "reason": "r"}
                ],
                "final_response_instruction": "n/a",
            }
            payload = f"<orchestra_plan>{json.dumps(bad_plan)}</orchestra_plan>"
            store_v = fresh_store("validate")
            run_v = run_supervisor(
                "Persisted validation smoke.",
                dry_run=True,
                store=store_v,
                _dry_run_planner_content=payload,
            )
            decisions = store_v.load_decisions(run_v.run_id)
            messages = store_v.load_messages(run_v.run_id)
            ok = (
                run_v.status == SUPERVISOR_STATUS_BLOCKED
                and len(decisions) == 1
                and decisions[0]["phase"] == "plan_parse_or_validate"
                and decisions[0]["signed_message"].startswith("[Atlas · ")
                and len(messages) >= 1  # planner envelope was persisted
            )
            record(
                "persisted-plan-validation-block",
                ok,
                "signed plan-validate decision + planner envelope persisted"
                if ok
                else f"unexpected: status={run_v.status}, decisions={decisions}",
            )
        except Exception as exc:  # noqa: BLE001
            record("persisted-plan-validation-block", False, f"raised {type(exc).__name__}: {exc}")

        # 22. persisted-human-approved — gate row in pending status
        try:
            gated_plan = {
                "summary": "x",
                "steps": [
                    {"id": 1, "target": "Cody", "message": "Gated.",
                     "action_surface": ACTION_SURFACE_HUMAN_APPROVED_ONLY,
                     "reason": "Surface gate."}
                ],
                "final_response_instruction": "n/a",
            }
            payload = f"<orchestra_plan>{json.dumps(gated_plan)}</orchestra_plan>"
            store_g = fresh_store("gate")
            run_g = run_supervisor(
                "Persisted human-approval smoke.",
                dry_run=True,
                store=store_g,
                _dry_run_planner_content=payload,
            )
            gates = store_g.load_gates(run_g.run_id)
            decisions = store_g.load_decisions(run_g.run_id)
            ok = (
                run_g.status == SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL
                and len(gates) == 1
                and gates[0]["status"] == "pending"
                and gates[0]["signed_message"].startswith("[Atlas · ")
                and len(decisions) == 1
                and decisions[0]["phase"] == "step_1_blocker"
            )
            record(
                "persisted-human-approved",
                ok,
                "pending gate row + signed step blocker decision persisted"
                if ok
                else f"unexpected: status={run_g.status}, gates={gates}, decisions={decisions}",
            )
        except Exception as exc:  # noqa: BLE001
            record("persisted-human-approved", False, f"raised {type(exc).__name__}: {exc}")

        # 23. persisted-resume-two-step-in-place — Atlas's targeted proof:
        # crash after step 1 on a two-step plan; resume completes step 2
        # in-place under the SAME run_id, leaves step 1 untouched, runs
        # the finalizer, and marks the original session complete.
        try:
            two_step_plan = {
                "summary": "two-step in-place resume",
                "steps": [
                    {"id": 1, "target": "Cody", "message": "Step one.",
                     "action_surface": ACTION_SURFACE_SAFE, "reason": "first"},
                    {"id": 2, "target": "Cody", "message": "Step two.",
                     "action_surface": ACTION_SURFACE_SAFE, "reason": "second"},
                ],
                "final_response_instruction": "Summarize both steps.",
            }
            payload = f"<orchestra_plan>{json.dumps(two_step_plan)}</orchestra_plan>"
            store_c = fresh_store("two_step")
            run_a = run_supervisor(
                "Two-step in-place resume smoke.",
                dry_run=True,
                store=store_c,
                _dry_run_planner_content=payload,
                _stop_after="step:1",
            )
            # Crash-row semantics: non-terminal session must have completed_at IS NULL.
            crash_session = store_c.load_session(run_a.run_id)
            actions_before = store_c.load_actions(run_a.run_id)
            crash_ok = (
                run_a.status == SUPERVISOR_STATUS_EXECUTING
                and run_a.completed_at is None
                and crash_session is not None
                and crash_session.completed_at is None
                and len(actions_before) == 2
                and actions_before[0]["status"] == STEP_STATUS_RESPONDED
                and actions_before[1]["status"] == STEP_STATUS_PLANNED
            )
            # In-place resume on the SAME run_id.
            resumed = resume_supervisor(
                run_a.run_id,
                store=store_c,
                directive="Two-step in-place resume smoke.",
                dry_run=True,
            )
            after_actions = store_c.load_actions(run_a.run_id)
            decisions = store_c.load_decisions(run_a.run_id)
            same_run = resumed.run_id == run_a.run_id
            both_responded = (
                len(after_actions) == 2
                and after_actions[0]["status"] == STEP_STATUS_RESPONDED
                and after_actions[1]["status"] == STEP_STATUS_RESPONDED
            )
            # Step 1's response_envelope_id should be unchanged.
            step1_unchanged = (
                after_actions[0]["response_envelope_id"]
                == actions_before[0]["response_envelope_id"]
            )
            no_replay = not any(d["phase"] == "resume_replayed" for d in decisions)
            ok = (
                crash_ok
                and same_run
                and resumed.status == SUPERVISOR_STATUS_COMPLETE
                and both_responded
                and step1_unchanged
                and no_replay
                and resumed.finalizer_envelope is not None
            )
            record(
                "persisted-resume-two-step-in-place",
                ok,
                f"same_run={same_run}, actions={[a['status'] for a in after_actions]}, "
                f"step1_unchanged={step1_unchanged}, no_replay={no_replay}, "
                f"finalizer={resumed.finalizer_envelope.id if resumed.finalizer_envelope else None}",
            )
        except Exception as exc:  # noqa: BLE001
            record("persisted-resume-two-step-in-place", False, f"raised {type(exc).__name__}: {exc}")

        # 24. persisted-resume-finalizer-only — single-step crash after step 1
        # leaves all steps responded but finalizer missing; resume runs ONLY
        # the finalizer under the same run_id.
        try:
            store_f = fresh_store("finalizer_only")
            run_f = run_supervisor(
                "Finalizer-only resume smoke.",
                dry_run=True,
                store=store_f,
                _stop_after="step:1",
            )
            crash_session = store_f.load_session(run_f.run_id)
            actions_before = store_f.load_actions(run_f.run_id)
            msgs_before = store_f.load_messages(run_f.run_id)
            crash_ok = (
                run_f.status == SUPERVISOR_STATUS_EXECUTING
                and crash_session is not None
                and crash_session.completed_at is None  # non-terminal: completed_at must be None
                and crash_session.finalizer_envelope_id is None
                and len(actions_before) == 1
                and actions_before[0]["status"] == STEP_STATUS_RESPONDED
                and len(msgs_before) == 2  # planner + step 1
            )
            resumed = resume_supervisor(
                run_f.run_id,
                store=store_f,
                directive="Finalizer-only resume smoke.",
                dry_run=True,
            )
            after_session = store_f.load_session(run_f.run_id)
            after_actions = store_f.load_actions(run_f.run_id)
            after_msgs = store_f.load_messages(run_f.run_id)
            step1_unchanged = (
                len(after_actions) == 1
                and after_actions[0]["response_envelope_id"]
                == actions_before[0]["response_envelope_id"]
            )
            same_run = resumed.run_id == run_f.run_id
            ok = (
                crash_ok
                and same_run
                and resumed.status == SUPERVISOR_STATUS_COMPLETE
                and after_session is not None
                and after_session.finalizer_envelope_id is not None
                and after_session.completed_at is not None
                and step1_unchanged
                and len(after_msgs) == 3  # planner + step 1 + finalizer (no duplication)
            )
            record(
                "persisted-resume-finalizer-only",
                ok,
                f"same_run={same_run}, finalizer_env={after_session.finalizer_envelope_id if after_session else None}, "
                f"msgs={len(after_msgs)}, step1_unchanged={step1_unchanged}"
                if ok
                else f"unexpected: crash_ok={crash_ok}, resumed={resumed.status}, "
                f"after_actions={[(a['step_id'], a['status']) for a in after_actions]}, "
                f"after_session.completed_at={after_session.completed_at if after_session else None}, "
                f"after_session.finalizer={after_session.finalizer_envelope_id if after_session else None}",
            )
        except Exception as exc:  # noqa: BLE001
            record("persisted-resume-finalizer-only", False, f"raised {type(exc).__name__}: {exc}")

        # 25. persisted-resume-unsafe-no-directive — crash with no planner
        # envelope and no directive on resume produces a signed
        # `resume_unsafe` blocker and no provider call.
        try:
            store_u = fresh_store("unsafe")
            run_u = run_supervisor(
                "Unsafe resume smoke.",
                dry_run=True,
                store=store_u,
                _stop_after="planner",
            )
            # After planner-stage crash: status=planning, planner_envelope_id IS set,
            # plan IS persisted. To prove the missing-directive unsafe path,
            # synthetically clear the planner envelope id + plan on the row so the
            # resume hits the no-planner branch.
            with store_u.transaction() as conn:
                conn.execute(
                    "UPDATE sessions SET planner_envelope_id = NULL, plan_json = NULL "
                    "WHERE run_id = ?",
                    (run_u.run_id,),
                )
            crash_session = store_u.load_session(run_u.run_id)
            crash_ok = (
                crash_session is not None
                and crash_session.completed_at is None
                and crash_session.planner_envelope_id is None
                and crash_session.plan is None
            )
            resumed = resume_supervisor(
                run_u.run_id,
                store=store_u,
                directive=None,  # the key property under test
                dry_run=True,
            )
            decisions = store_u.load_decisions(run_u.run_id)
            unsafe_decisions = [d for d in decisions if d["phase"] == "resume_unsafe"]
            ok = (
                crash_ok
                and resumed.run_id == run_u.run_id  # same run_id
                and resumed.status == SUPERVISOR_STATUS_BLOCKED
                and len(unsafe_decisions) == 1
                and unsafe_decisions[0]["signed_message"].startswith("[Atlas · ")
            )
            record(
                "persisted-resume-unsafe-no-directive",
                ok,
                "signed resume_unsafe blocker persisted on same run_id; no provider call"
                if ok
                else f"unexpected: crash_ok={crash_ok}, resumed.status={resumed.status}, "
                f"unsafe_decisions={unsafe_decisions}",
            )
        except Exception as exc:  # noqa: BLE001
            record("persisted-resume-unsafe-no-directive", False, f"raised {type(exc).__name__}: {exc}")

        # 25. persisted-redaction — synthetic token doesn't survive in rows
        try:
            store_r = fresh_store("redact")
            # The directive itself would be blocked by pre-flight; instead we
            # use a benign directive and check that signed_message + plan_json
            # come back free of token content. The supervisor's own
            # directive_summary path runs `redact()`; the decision metadata
            # path runs `redact()`. To prove it, inject a token through the
            # crash hook's `_dry_run_planner_content`, which lands inside
            # planner_envelope.content (already redacted by 4.4 inbound) and
            # also bleeds into the plan_parse error message.
            tainted = "<orchestra_plan>{not json: sk-proj-BBBBBBBBBBBBBBBBBBBB}</orchestra_plan>"
            run_r = run_supervisor(
                "Persisted redaction smoke.",
                dry_run=True,
                store=store_r,
                _dry_run_planner_content=tainted,
            )
            decisions = store_r.load_decisions(run_r.run_id)
            messages = store_r.load_messages(run_r.run_id)
            blob = "".join(
                [d["reason_or_error"] + " " + d["metadata_json"] for d in decisions]
            ) + "".join(m["content"] + " " + m["metadata_json"] for m in messages)
            ok = (
                run_r.status == SUPERVISOR_STATUS_BLOCKED
                and "sk-proj-BBBBBBBBBBBBBBBBBBBB" not in blob
            )
            record(
                "persisted-redaction",
                ok,
                "synthetic token did not survive in any persisted row"
                if ok
                else f"raw token leaked into persisted state: {blob[:200]}",
            )
        except Exception as exc:  # noqa: BLE001
            record("persisted-redaction", False, f"raised {type(exc).__name__}: {exc}")

    # 27. persisted-provider-transient-step — 4.7 step 1: prove a synthetic
    # provider transient error during a subagent step becomes a signed
    # Atlas error decision (phase=step_N_provider_transient) instead of
    # surfacing as a raw traceback. This regression-covers the 4.6
    # closure flag without forcing a real upstream failure.
    try:
        import anthropic  # noqa: PLC0415

        overload_cls = getattr(anthropic, "OverloadedError", None)
        if overload_cls is None:
            # SDK 0.x exposes OverloadedError only under _exceptions.
            try:
                from anthropic import _exceptions as _anth_exc  # noqa: PLC0415
            except ImportError:
                _anth_exc = None  # type: ignore[assignment]
            if _anth_exc is not None:
                overload_cls = getattr(_anth_exc, "OverloadedError", None)
        if overload_cls is None:
            record(
                "persisted-provider-transient-step",
                False,
                "anthropic.OverloadedError not importable from top-level or _exceptions; skipping",
            )
        else:
            # Construct without calling __init__ to avoid SDK constructor
            # signature drift breaking the test.
            synthetic = overload_cls.__new__(overload_cls)
            synthetic.args = ("simulated 529 overload",)
            store_t = SessionStore(tmp_path / "transient.db")
            run_t = run_supervisor(
                "Provider-transient smoke.",
                dry_run=True,
                store=store_t,
                _force_step_exception=synthetic,
                _force_step_target_id=1,
            )
            decisions = store_t.load_decisions(run_t.run_id)
            transient_decisions = [
                d
                for d in decisions
                if d["phase"] == "step_1_provider_transient"
            ]
            try:
                meta = (
                    json.loads(transient_decisions[0]["metadata_json"])
                    if transient_decisions
                    else {}
                )
            except (json.JSONDecodeError, KeyError):
                meta = {}
            ok = (
                run_t.status == SUPERVISOR_STATUS_ERRORED
                and len(transient_decisions) == 1
                and transient_decisions[0]["signed_message"].startswith("[Atlas · ")
                and meta.get("exception_class") == "OverloadedError"
                and meta.get("error_kind") == "transient_provider"
            )
            record(
                "persisted-provider-transient-step",
                ok,
                "signed step_1_provider_transient decision persisted with "
                "exception_class=OverloadedError, kind=transient_provider"
                if ok
                else f"unexpected: status={run_t.status}, "
                f"transient_decisions={transient_decisions}, meta={meta}",
            )
    except ImportError:
        record(
            "persisted-provider-transient-step",
            False,
            "anthropic SDK not installed; skipping",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "persisted-provider-transient-step",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    # 28. persisted-resume-provider-transient-step — 4.7 step 1 addendum:
    # prove the SAME provider-transient classification works in the resume
    # path (Atlas review 1215456686905326). Two-step plan, crash after
    # step 1, resume with forced OverloadedError on step 2. Asserts:
    #   - status=errored
    #   - signed decision row phase=step_2_provider_transient (same run_id)
    #   - exception_class=OverloadedError, error_kind=transient_provider
    #   - resume preserves same_run = True
    try:
        import anthropic  # noqa: PLC0415

        overload_cls = getattr(anthropic, "OverloadedError", None)
        if overload_cls is None:
            try:
                from anthropic import _exceptions as _anth_exc  # noqa: PLC0415
            except ImportError:
                _anth_exc = None  # type: ignore[assignment]
            if _anth_exc is not None:
                overload_cls = getattr(_anth_exc, "OverloadedError", None)
        if overload_cls is None:
            record(
                "persisted-resume-provider-transient-step",
                False,
                "anthropic.OverloadedError not importable from top-level or _exceptions; skipping",
            )
        else:
            two_step_plan = {
                "summary": "resume transient",
                "steps": [
                    {"id": 1, "target": "Cody", "message": "ok step",
                     "action_surface": ACTION_SURFACE_SAFE, "reason": "first"},
                    {"id": 2, "target": "Cody", "message": "transient step",
                     "action_surface": ACTION_SURFACE_SAFE, "reason": "second"},
                ],
                "final_response_instruction": "Summarize.",
            }
            payload = (
                f"<orchestra_plan>{json.dumps(two_step_plan)}</orchestra_plan>"
            )
            store_rt = SessionStore(tmp_path / "resume_transient.db")
            crashed = run_supervisor(
                "Resume transient smoke.",
                dry_run=True,
                store=store_rt,
                _dry_run_planner_content=payload,
                _stop_after="step:1",
            )
            synthetic = overload_cls.__new__(overload_cls)
            synthetic.args = ("simulated 529 during resumed step 2",)
            resumed = resume_supervisor(
                crashed.run_id,
                store=store_rt,
                directive="Resume transient smoke.",
                dry_run=True,
                _force_step_exception=synthetic,
                _force_step_target_id=2,
            )
            decisions = store_rt.load_decisions(crashed.run_id)
            transient_decisions = [
                d
                for d in decisions
                if d["phase"] == "step_2_provider_transient"
            ]
            try:
                meta = (
                    json.loads(transient_decisions[0]["metadata_json"])
                    if transient_decisions
                    else {}
                )
            except (json.JSONDecodeError, KeyError):
                meta = {}
            same_run = resumed.run_id == crashed.run_id
            ok = (
                same_run
                and resumed.status == SUPERVISOR_STATUS_ERRORED
                and len(transient_decisions) == 1
                and transient_decisions[0]["signed_message"].startswith("[Atlas · ")
                and meta.get("exception_class") == "OverloadedError"
                and meta.get("error_kind") == "transient_provider"
            )
            record(
                "persisted-resume-provider-transient-step",
                ok,
                f"same_run={same_run}, status={resumed.status}, "
                f"signed step_2_provider_transient decision with "
                f"exception_class=OverloadedError, kind=transient_provider"
                if ok
                else f"unexpected: same_run={same_run}, status={resumed.status}, "
                f"transient_decisions={transient_decisions}, meta={meta}",
            )
    except ImportError:
        record(
            "persisted-resume-provider-transient-step",
            False,
            "anthropic SDK not installed; skipping",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "persisted-resume-provider-transient-step",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    # 29. force-hook-rejected-on-live — 4.7 step 1 addendum: prove the
    # private forced-exception hook is rejected on live runs. Verifies
    # that even if a caller passes the private kwarg, dry_run=False
    # makes the call fail closed before any provider call.
    try:
        synthetic = Exception("simulated")
        run_supervisor(
            "Live forced-hook smoke.",
            dry_run=False,
            _force_step_exception=synthetic,
        )
        record(
            "force-hook-rejected-on-live",
            False,
            "live run with _force_step_exception did NOT raise ValueError",
        )
    except ValueError as exc:
        ok = "dry-run-only" in str(exc)
        record(
            "force-hook-rejected-on-live",
            ok,
            "ValueError raised on live force-hook usage as expected"
            if ok
            else f"unexpected ValueError text: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "force-hook-rejected-on-live",
            False,
            f"raised {type(exc).__name__} (expected ValueError): {exc}",
        )

    # 30. force-hook-rejected-on-live-resume — same enforcement on resume.
    try:
        # Build any persisted session quickly to give resume a target.
        store_l = SessionStore(tmp_path / "force_live.db")
        crashed_l = run_supervisor(
            "force-hook-rejected-on-live-resume seed.",
            dry_run=True,
            store=store_l,
            _stop_after="planner",
        )
        synthetic = Exception("simulated")
        resume_supervisor(
            crashed_l.run_id,
            store=store_l,
            directive="seed",
            dry_run=False,
            _force_step_exception=synthetic,
        )
        record(
            "force-hook-rejected-on-live-resume",
            False,
            "live resume with _force_step_exception did NOT raise ValueError",
        )
    except ValueError as exc:
        ok = "dry-run-only" in str(exc)
        record(
            "force-hook-rejected-on-live-resume",
            ok,
            "ValueError raised on live resume force-hook usage as expected"
            if ok
            else f"unexpected ValueError text: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "force-hook-rejected-on-live-resume",
            False,
            f"raised {type(exc).__name__} (expected ValueError): {exc}",
        )

    # --- 4.7 main scope: supervisor + tool registry integration -------------
    # Each scenario drives a full run_supervisor(..., dry_run=True, store=...)
    # whose plan carries tool_calls on a step. The full hook chain
    # (permission → surface → secrets → approval → execute → sign →
    # persist) runs, and the supervisor halt path / message-folding /
    # tool_call row persistence are all asserted.
    import os  # noqa: PLC0415 - local: only needed for the ORCHESTRA_ROOT toggling here

    def _plan_with_tool_calls(
        target: str,
        tool_calls: list[dict[str, Any]],
        *,
        action_surface: str = ACTION_SURFACE_SAFE,
    ) -> dict[str, Any]:
        return {
            "summary": "supervisor+tools scenario",
            "steps": [
                {
                    "id": 1,
                    "target": target,
                    "message": "Acknowledge tool results.",
                    "action_surface": action_surface,
                    "reason": "supervisor+tools scenario step",
                    "tool_calls": tool_calls,
                }
            ],
            "final_response_instruction": "Summarize the tool outputs.",
        }

    # 31. plan-tool-calls-validate-unknown-tool: planner emits a tool_call
    # for an unregistered tool. _validate_plan refuses.
    try:
        bad_plan = _plan_with_tool_calls(
            "Cody",
            [{"tool_name": "bogus.thing", "args": {}}],
        )
        payload = f"<orchestra_plan>{json.dumps(bad_plan)}</orchestra_plan>"
        run_t = run_supervisor(
            "tool calls scenario",
            dry_run=True,
            _dry_run_planner_content=payload,
        )
        ok = (
            run_t.status == SUPERVISOR_STATUS_BLOCKED
            and len(run_t.blockers) == 1
            and run_t.blockers[0]["phase"] == "plan_parse_or_validate"
            and "unknown tool" in run_t.blockers[0]["reason"]
        )
        record(
            "plan-tool-calls-validate-unknown-tool",
            ok,
            f"status={run_t.status}, blockers={[b.get('phase') for b in run_t.blockers]}",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "plan-tool-calls-validate-unknown-tool",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    # 32. plan-tool-calls-validate-unauthorized-agent: Scribe attempts
    # github.get_repo. _validate_plan refuses.
    try:
        bad_plan = _plan_with_tool_calls(
            "Scribe",
            [
                {
                    "tool_name": "github.get_repo",
                    "args": {"owner": "ClarityOps-Apps", "repo": "agent-orchestra"},
                }
            ],
        )
        payload = f"<orchestra_plan>{json.dumps(bad_plan)}</orchestra_plan>"
        run_t = run_supervisor(
            "tool calls scenario",
            dry_run=True,
            _dry_run_planner_content=payload,
        )
        ok = (
            run_t.status == SUPERVISOR_STATUS_BLOCKED
            and "not authorized" in (run_t.blockers[0]["reason"] if run_t.blockers else "")
        )
        record(
            "plan-tool-calls-validate-unauthorized-agent",
            ok,
            f"status={run_t.status}, reason={(run_t.blockers[0]['reason'] if run_t.blockers else None)}",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "plan-tool-calls-validate-unauthorized-agent",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    # 33. supervisor-tool-call-ok-persisted: Cody runs filesystem.read_file
    # on a synthetic file under ORCHESTRA_ROOT; tool_call row is persisted
    # and the run completes with status=complete.
    try:
        with tempfile.TemporaryDirectory(prefix="sv-tool-ok-") as t:
            os.environ["ORCHESTRA_ROOT"] = t
            try:
                # Seed a target file inside the root.
                target_file = Path(t) / "scratch.txt"
                target_file.write_text("hello", encoding="utf-8")
                good_plan = _plan_with_tool_calls(
                    "Cody",
                    [
                        {
                            "tool_name": "filesystem.read_file",
                            "args": {"path": str(target_file)},
                        }
                    ],
                )
                payload = f"<orchestra_plan>{json.dumps(good_plan)}</orchestra_plan>"
                store_ok = SessionStore(Path(t) / "sessions.db")
                run_t = run_supervisor(
                    "tool calls scenario",
                    dry_run=True,
                    store=store_ok,
                    _dry_run_planner_content=payload,
                )
                tool_calls = store_ok.load_tool_calls(run_t.run_id)
                ok = (
                    run_t.status == SUPERVISOR_STATUS_COMPLETE
                    and len(tool_calls) == 1
                    and tool_calls[0]["status"] == "ok"
                    and tool_calls[0]["tool_name"] == "filesystem.read_file"
                    and tool_calls[0]["signed_message"].startswith("[Cody · ")
                )
                record(
                    "supervisor-tool-call-ok-persisted",
                    ok,
                    f"status={run_t.status}, tool_calls={len(tool_calls)}, "
                    f"row_status={tool_calls[0]['status'] if tool_calls else None}",
                )
            finally:
                os.environ.pop("ORCHESTRA_ROOT", None)
    except Exception as exc:  # noqa: BLE001
        record(
            "supervisor-tool-call-ok-persisted",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    # 34. supervisor-tool-call-secrets-block: Cody supplies a secret-shaped
    # token in tool args. Hook chain blocks before execution; tool_call row
    # status=blocked; supervisor halts with status=blocked.
    try:
        os.environ.setdefault("ORCHESTRA_ROOT", str(Path(__file__).resolve().parent))
        secret = "sk-proj-CCCCCCCCCCCCCCCCCCCC"
        bad_plan = _plan_with_tool_calls(
            "Cody",
            [
                {
                    "tool_name": "asana.add_comment",
                    "args": {"task_id": "123", "text": f"please use {secret}"},
                }
            ],
            action_surface=ACTION_SURFACE_SAFE,
        )
        payload = f"<orchestra_plan>{json.dumps(bad_plan)}</orchestra_plan>"
        with tempfile.TemporaryDirectory(prefix="sv-tool-sec-") as t:
            store_sec = SessionStore(Path(t) / "sessions.db")
            run_t = run_supervisor(
                "tool calls scenario",
                dry_run=True,
                store=store_sec,
                _dry_run_planner_content=payload,
            )
            tool_calls = store_sec.load_tool_calls(run_t.run_id)
            ok = (
                run_t.status == SUPERVISOR_STATUS_BLOCKED
                and len(tool_calls) == 1
                and tool_calls[0]["status"] == "blocked"
                and secret not in (tool_calls[0]["args_json"] or "")
                and secret not in (tool_calls[0]["signed_message"] or "")
            )
            record(
                "supervisor-tool-call-secrets-block",
                ok,
                f"status={run_t.status}, "
                f"row_status={tool_calls[0]['status'] if tool_calls else None}, "
                f"raw_secret_survived="
                f"{tool_calls and secret in (tool_calls[0]['args_json'] or '')}",
            )
    except Exception as exc:  # noqa: BLE001
        record(
            "supervisor-tool-call-secrets-block",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    # 35. supervisor-tool-call-human-approved-pending: Cody attempts
    # github.push_branch (human-approved-only). Supervisor halts as
    # pending_human_approval; gate row + decision row + tool_call row
    # all present, no live execution.
    try:
        plan35 = _plan_with_tool_calls(
            "Cody",
            [
                {
                    "tool_name": "github.push_branch",
                    "args": {"branch": "main"},
                }
            ],
        )
        payload = f"<orchestra_plan>{json.dumps(plan35)}</orchestra_plan>"
        with tempfile.TemporaryDirectory(prefix="sv-tool-gate-") as t:
            store_g = SessionStore(Path(t) / "sessions.db")
            run_t = run_supervisor(
                "tool calls scenario",
                dry_run=True,
                store=store_g,
                _dry_run_planner_content=payload,
            )
            tool_calls = store_g.load_tool_calls(run_t.run_id)
            gates = store_g.load_gates(run_t.run_id)
            decisions = store_g.load_decisions(run_t.run_id)
            ok = (
                run_t.status == SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL
                and len(tool_calls) == 1
                and tool_calls[0]["status"] == "pending-human-approval"
                and len(gates) == 1
                and gates[0]["action_surface"] == "human-approved-only"
                and any(
                    d["phase"].endswith("_tool_pending_human_approval")
                    for d in decisions
                )
            )
            record(
                "supervisor-tool-call-human-approved-pending",
                ok,
                f"status={run_t.status}, gates={len(gates)}, "
                f"decisions={[d['phase'] for d in decisions]}",
            )
    except Exception as exc:  # noqa: BLE001
        record(
            "supervisor-tool-call-human-approved-pending",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    # 36. supervisor-tool-call-fs-deny-blocked: Cody attempts
    # filesystem.read_file on a deny-listed path (.env). Hook chain blocks
    # before any FS open; tool_call row=blocked, supervisor halt=blocked.
    try:
        with tempfile.TemporaryDirectory(prefix="sv-tool-fsdeny-") as t:
            os.environ["ORCHESTRA_ROOT"] = t
            try:
                plan36 = _plan_with_tool_calls(
                    "Cody",
                    [
                        {
                            "tool_name": "filesystem.read_file",
                            "args": {"path": f"{t}/.env"},
                        }
                    ],
                )
                payload = f"<orchestra_plan>{json.dumps(plan36)}</orchestra_plan>"
                store_fd = SessionStore(Path(t) / "sessions.db")
                run_t = run_supervisor(
                    "tool calls scenario",
                    dry_run=True,
                    store=store_fd,
                    _dry_run_planner_content=payload,
                )
                tool_calls = store_fd.load_tool_calls(run_t.run_id)
                ok = (
                    run_t.status == SUPERVISOR_STATUS_BLOCKED
                    and len(tool_calls) == 1
                    and tool_calls[0]["status"] == "blocked"
                    and "deny pattern" in (tool_calls[0]["result_summary"] or "")
                )
                record(
                    "supervisor-tool-call-fs-deny-blocked",
                    ok,
                    f"status={run_t.status}, "
                    f"row_summary={(tool_calls[0]['result_summary'][:80] if tool_calls else None)}",
                )
            finally:
                os.environ.pop("ORCHESTRA_ROOT", None)
    except Exception as exc:  # noqa: BLE001
        record(
            "supervisor-tool-call-fs-deny-blocked",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    for case in passes:
        print(sign_action("Cody", f"supervisor dry-run pass — {case}"))
    for case in failures:
        print(sign_action("Cody", f"supervisor dry-run FAIL — {case}"))

    return 0 if not failures else 1


def _format_run(run: SupervisorRun) -> str:
    """Render a SupervisorRun as a human-readable block for CLI output.

    Surfaces every signed blocker/error message Atlas produced so an
    operator reading the CLI output of a blocked or errored run sees the
    Atlas-signed line directly without inspecting dict internals. The
    underlying structured record (with `phase`, `reason`/`error`, and any
    extra metadata) is still printed below the signed line for full
    fidelity.
    """
    lines = [
        f"  run_id            : {run.run_id}",
        f"  session_id        : {run.session_id}",
        f"  directive_summary : {run.directive_summary}",
        f"  status            : {run.status}",
        f"  created_at        : {run.created_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"  completed_at      : {run.completed_at.strftime('%Y-%m-%dT%H:%M:%SZ') if run.completed_at else '-'}",
        f"  max_steps         : {run.max_steps}",
        f"  dry_run           : {run.dry_run}",
        f"  planner_env_id    : {run.planner_envelope.id if run.planner_envelope else '-'}",
        f"  finalizer_env_id  : {run.finalizer_envelope.id if run.finalizer_envelope else '-'}",
        f"  plan.summary      : {run.plan.get('summary') if run.plan else '-'}",
        f"  steps             : {len(run.steps)}",
    ]
    if run.blockers:
        lines.append(f"  blockers          : {len(run.blockers)}")
        for idx, blocker in enumerate(run.blockers, start=1):
            signed = blocker.get("signed_message", "(no signed_message — pre-addendum record)")
            lines.append(f"    [{idx}] {signed}")
            for k, v in blocker.items():
                if k == "signed_message":
                    continue
                lines.append(f"        {k}: {v}")
    else:
        lines.append("  blockers          : 0")
    if run.errors:
        lines.append(f"  errors            : {len(run.errors)}")
        for idx, err in enumerate(run.errors, start=1):
            signed = err.get("signed_message", "(no signed_message — pre-addendum record)")
            lines.append(f"    [{idx}] {signed}")
            for k, v in err.items():
                if k == "signed_message":
                    continue
                lines.append(f"        {k}: {v}")
    else:
        lines.append("  errors            : 0")
    for step in run.steps:
        env = step.response_envelope
        lines.append(
            f"    [{step.id}] {step.target} ({step.action_surface}) — status={step.status}, "
            f"envelope={env.id if env else '-'}"
        )
        if env is not None:
            for content_line in env.content.splitlines():
                lines.append(f"        {content_line}")
    if run.finalizer_envelope is not None:
        lines.append("  finalizer.content :")
        for line in run.finalizer_envelope.content.splitlines():
            lines.append(f"    {line}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supervisor",
        description="Supervisor loop validator for Agent Orchestra (M4 task 4.5/4.6).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the 36 supervisor scenarios (11 unit + 7 signed-halt + 10 persisted including resume + 2 force-hook guards + 6 tool-registry integration) without any provider API call.",
    )
    parser.add_argument(
        "--directive",
        metavar="TEXT",
        help="Run a single live supervisor invocation for the given directive.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help=f"Maximum subagent steps. Default {DEFAULT_MAX_STEPS}, hard cap {HARD_CAP_MAX_STEPS}.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Persist the run to the SQLite session store (task 4.6).",
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        help="Override SQLite DB path. Honors ORCHESTRA_SESSIONS_DB env var otherwise.",
    )
    parser.add_argument(
        "--resume",
        metavar="ID",
        help="Resume a stored run by run_id or session_id (requires --db-path or "
        "ORCHESTRA_SESSIONS_DB). Pair with --directive to re-supply the original "
        "directive for live resume; omit for terminal-state rehydration.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        return _supervisor_dry_run()

    store: SessionStore | None = None
    if args.persist or args.resume or args.db_path:
        store = SessionStore(args.db_path)
        store.ensure_schema()

    if args.resume:
        if store is None:  # pragma: no cover - --resume forces store construction above
            print(sign_action("Cody", "supervisor: --resume requires a SessionStore."))
            return 1
        run = resume_supervisor(
            args.resume,
            store=store,
            directive=args.directive,
        )
        print(sign_action("Cody", f"supervisor resumed {args.resume} → status={run.status}"))
        print(_format_run(run))
        return 0 if run.status == SUPERVISOR_STATUS_COMPLETE else 1

    if args.directive:
        run = run_supervisor(
            args.directive,
            max_steps=args.max_steps,
            store=store,
        )
        suffix = f" (persisted to {store.db_path})" if store is not None else ""
        print(
            sign_action(
                "Cody",
                f"supervisor run {run.run_id} → status={run.status}{suffix}",
            )
        )
        print(_format_run(run))
        return 0 if run.status == SUPERVISOR_STATUS_COMPLETE else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
