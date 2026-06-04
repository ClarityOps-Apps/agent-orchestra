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
(`uv run python -m supervisor --dry-run | --directive`) is validation-only.
This module does NOT implement:
- SQLite session persistence (task 4.6).
- MCP tool wiring inside the loop (task 4.7).
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


# --- Dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorStep:
    """In-memory record of one subagent turn."""

    id: int
    target: str
    message: str
    action_surface: str
    reason: str
    status: str
    response_envelope: MessageEnvelope | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class SupervisorRun:
    """In-memory record of one supervisor invocation.

    Persistence is intentionally absent — task 4.6 (SQLite) is the place
    that will write these to disk. For 4.5 the run lives only in the
    returning caller's address space.
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
        "If the directive cannot be safely planned, emit a JSON plan with "
        '"steps": [] and a `summary` explaining why; the finalizer turn '
        "will surface that to Garrett."
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
        normalized.append(
            {
                "id": step_id,
                "target": canonical_target,
                "message": message,
                "action_surface": action_surface,
                "reason": reason,
            }
        )
    return normalized


# --- Provider-call wrappers (planner / step / finalizer) ---------------------


def _planner_envelope_for_dry_run(
    directive: str, session_id: str, parent_id: str | None
) -> MessageEnvelope:
    """Synthetic planner envelope used by dry-run: contains a valid one-step plan."""
    dry_plan = {
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
    payload = f"<orchestra_plan>{json.dumps(dry_plan)}</orchestra_plan>"
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
) -> MessageEnvelope:
    """Run the planner turn. Returns the planner's response envelope.

    The envelope flows through the same 4.4 protocol used for subagent
    sends, so the secrets check, identity-sign, and runtime UTC stamping
    rules all apply uniformly.
    """
    if dry_run:
        return _planner_envelope_for_dry_run(
            directive, session_id, parent_id=None
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
) -> SupervisorStep:
    """Run one subagent step and return its SupervisorStep record."""
    started_at = _utc_now()
    try:
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
) -> SupervisorRun:
    """Run one supervisor invocation and return a `SupervisorRun` record.

    See module docstring for the turn structure. This function never raises
    on planner/step/finalizer failures — every halt path returns a
    `SupervisorRun` with the relevant status and a signed blocker envelope
    or error record. Internal invariant violations (e.g. status enum drift)
    are the only paths that raise.

    Args:
        directive: Garrett's directive text.
        max_steps: maximum subagent steps the planner may emit. Clamped to
            [1, HARD_CAP_MAX_STEPS].
        dry_run: when True, no provider API calls are made. The planner
            turn returns a canned valid plan; subagent and finalizer calls
            use the 4.4 protocol's `_skip_provider=True` path. Used by the
            module CLI's --dry-run and by CI/local checks without secrets.

    Returns:
        SupervisorRun: in-memory record of the run (no persistence).
    """
    if not isinstance(directive, str):
        raise TypeError("run_supervisor: directive must be a string.")

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

    def finalize(status: str) -> SupervisorRun:
        return _build_run(
            run_id=run_id,
            session_id=session_id,
            directive_summary=directive_summary,
            status=status,
            created_at=created_at,
            completed_at=_utc_now(),
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
        blockers.append(
            {
                "phase": "preflight_secrets_check",
                "reason": secrets_result.reason,
                "secret_kinds": list(secrets_result.matches),
            }
        )
        return finalize(SUPERVISOR_STATUS_BLOCKED)

    # --- Planner turn ---------------------------------------------------------
    try:
        planner_envelope = _plan_turn(directive, session_id, max_steps_norm, dry_run)
    except (UnknownAgentError, ProtocolError, MissingEnvError) as exc:
        errors.append({"phase": "planner_call", "error": str(exc)})
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except ImportError as exc:
        errors.append({"phase": "planner_sdk", "error": str(exc)})
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except Exception as exc:  # noqa: BLE001 - last-resort: don't surface raw traceback
        errors.append(
            {"phase": "planner_call", "error": f"{type(exc).__name__}: {exc}"}
        )
        return finalize(SUPERVISOR_STATUS_ERRORED)

    if planner_envelope.message_type == MESSAGE_TYPE_BLOCKER:
        blockers.append(
            {
                "phase": "planner_blocker",
                "reason": "planner returned a blocker envelope",
                "envelope_id": planner_envelope.id,
                "metadata": dict(planner_envelope.metadata),
            }
        )
        return finalize(SUPERVISOR_STATUS_BLOCKED)

    # --- Plan parse + validate -----------------------------------------------
    try:
        plan = _parse_plan(planner_envelope.content)
        validated_steps = _validate_plan(plan, max_steps_norm)
    except PlanError as exc:
        blockers.append(
            {
                "phase": "plan_parse_or_validate",
                "reason": str(exc),
                "planner_envelope_id": planner_envelope.id,
            }
        )
        return finalize(SUPERVISOR_STATUS_BLOCKED)

    # --- Step execution -------------------------------------------------------
    for step_spec in validated_steps:
        step = _execute_step(
            step_spec=step_spec,
            session_id=session_id,
            parent_id=planner_envelope.id,
            dry_run=dry_run,
        )
        steps.append(step)
        if step.status == STEP_STATUS_BLOCKED:
            decision = (
                step.response_envelope.metadata.get("decision")
                if step.response_envelope is not None
                else None
            )
            phase = step.response_envelope.metadata.get("blocker_phase") if step.response_envelope else None
            blockers.append(
                {
                    "phase": f"step_{step.id}_blocker",
                    "reason": (
                        step.response_envelope.metadata.get("blocker_reason")
                        if step.response_envelope is not None
                        else "step blocked"
                    ),
                    "step_id": step.id,
                    "target": step.target,
                    "envelope_id": (
                        step.response_envelope.id
                        if step.response_envelope is not None
                        else None
                    ),
                    "blocker_phase": phase,
                    "decision": decision,
                }
            )
            if decision == "pending-human-approval":
                return finalize(SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL)
            return finalize(SUPERVISOR_STATUS_BLOCKED)
        if step.status == STEP_STATUS_ERRORED:
            errors.append(
                {
                    "phase": f"step_{step.id}_error",
                    "step_id": step.id,
                    "target": step.target,
                    "reason": "send_message raised; see receipts",
                }
            )
            return finalize(SUPERVISOR_STATUS_ERRORED)

    # --- Finalizer turn ------------------------------------------------------
    try:
        finalizer_envelope = _finalize_turn(
            plan, tuple(steps), session_id, planner_envelope.id, dry_run
        )
    except (UnknownAgentError, ProtocolError, MissingEnvError) as exc:
        errors.append({"phase": "finalizer_call", "error": str(exc)})
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except ImportError as exc:
        errors.append({"phase": "finalizer_sdk", "error": str(exc)})
        return finalize(SUPERVISOR_STATUS_ERRORED)
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {"phase": "finalizer_call", "error": f"{type(exc).__name__}: {exc}"}
        )
        return finalize(SUPERVISOR_STATUS_ERRORED)

    if finalizer_envelope.message_type == MESSAGE_TYPE_BLOCKER:
        blockers.append(
            {
                "phase": "finalizer_blocker",
                "reason": "finalizer returned a blocker envelope",
                "envelope_id": finalizer_envelope.id,
                "metadata": dict(finalizer_envelope.metadata),
            }
        )
        return finalize(SUPERVISOR_STATUS_BLOCKED)

    return finalize(SUPERVISOR_STATUS_COMPLETE)


# --- Dry-run + CLI ----------------------------------------------------------


def _supervisor_dry_run() -> int:
    """Exercise the supervisor without any provider API calls.

    Scenarios: happy-path (synthetic plan → safe step → synthetic
    finalizer); pre-flight secrets block; invalid planner JSON (direct
    `_parse_plan`); plan validation failures (unknown target, invalid
    surface, max-step overflow, empty steps, missing field); pending-human
    -approval step (synthetic plan injected via _execute_step). Emits one
    signed Cody line per scenario.
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

    for case in passes:
        print(sign_action("Cody", f"supervisor dry-run pass — {case}"))
    for case in failures:
        print(sign_action("Cody", f"supervisor dry-run FAIL — {case}"))

    return 0 if not failures else 1


def _format_run(run: SupervisorRun) -> str:
    """Render a SupervisorRun as a human-readable block for CLI output."""
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
        f"  blockers          : {list(run.blockers)}",
        f"  errors            : {list(run.errors)}",
        f"  plan.summary      : {run.plan.get('summary') if run.plan else '-'}",
        f"  steps             : {len(run.steps)}",
    ]
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
        description="Supervisor loop validator for Agent Orchestra (M4 task 4.5).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the 11 supervisor scenarios without any provider API call.",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        return _supervisor_dry_run()
    if args.directive:
        run = run_supervisor(args.directive, max_steps=args.max_steps)
        print(sign_action("Cody", f"supervisor run {run.run_id} → status={run.status}"))
        print(_format_run(run))
        return 0 if run.status == SUPERVISOR_STATUS_COMPLETE else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
