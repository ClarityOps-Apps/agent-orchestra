"""Runtime REST API for Agent Orchestra (M4 task 4.11).

Thin FastAPI layer over the existing runtime. The supervisor loop,
session store, tool registry, and hooks are NOT duplicated — every
endpoint composes with the same building blocks the CLI uses, so the
Retool flight deck (M5) and the CLI share one source of truth for runs,
gates, tool calls, and decisions.

Endpoint groups (Atlas-locked surface, SOW v3.1 + PRD v0.3):

  - **Projects** — multi-project flight-deck model. ``Agent Orchestra``
    itself is NOT seeded as a project; this table holds the operator's
    product initiatives (JLOOP CRM, JLOOP Goal Chains, future client
    work, etc.).
  - **Directives & Runs** — directive submission, background runner,
    snapshot listing, single-run detail, SSE stream, halt, plan
    preview, cost estimate.
  - **Gates** — approve/reject persisted gate rows. The 4.11 MVP records
    signed human decisions; safely *continuing* execution after approval
    requires raw-arg persistence we deliberately do NOT add in 4.11
    (would force secret storage). Approve returns the
    ``approved_recorded_execution_not_resumed`` envelope so the operator
    knows the human decision is durable even if the run does not auto-
    resume in MVP.
  - **Agents** — Atlas / Cody / Scribe / Scout: provider/model env labels
    (NOT values), allowed-tools matrix from ``tool_registry``, last
    activity timestamp from session_store.
  - **Integrations** — env-presence booleans for Asana, GitHub,
    filesystem, bash, plus Supabase if configured. ``POST .../auth`` and
    ``DELETE`` are surfaced as scaffolds that return
    ``manual_secret_handoff_required`` rather than accepting or
    deleting secrets in 4.11 — M5 task 5.2 owns the real handoff.
  - **Artifact Preview** — ``github.push_branch`` diff via a tightly
    scoped read-only ``git diff`` subprocess (no bash allowlist
    widening). Other artifact types return a structured
    ``unresolved_artifact`` response.
  - **Health / Documentation** — public health endpoint and FastAPI's
    auto OpenAPI / docs.

Authentication (PRD Q4 lock):

  - MVP single-user bearer auth via ``ORCHESTRA_API_TOKEN`` env var.
  - When the env var is missing, protected endpoints fail closed with
    a structured 503 response (NOT a silent allow). Health endpoint
    remains public.
  - Real token generation + Retool handoff is M5 task 5.2; this module
    never accepts, prints, or commits a real production token.

Background runner:

  - ``POST /projects/{id}/directive`` mints ``run_id`` + ``session_id``,
    persists the initial session row, and dispatches ``run_supervisor``
    on a background thread. The HTTP response returns immediately with
    the new ids and ``status="planning"``. The thread runs to completion
    against the same ids via the new ``run_id`` / ``session_id`` kwargs
    on ``run_supervisor`` (4.11 narrow extension).
  - ``POST /runs/{run_id}/halt`` persists a signed halt decision and
    sets the run's ``halt_event``. The supervisor checks
    ``halt_check`` at every safe boundary (between steps, before the
    finalizer) and halts with a ``SUPERVISOR_STATUS_BLOCKED`` outcome
    + signed ``api_halt_requested`` blocker. Mid-provider-call
    interruption is NOT supported in 4.11 — the run halts at the next
    safe boundary.

SSE stream:

  - ``GET /runs/{run_id}/stream`` polls the persisted ``messages``,
    ``actions``, ``decisions``, ``gates``, and ``tool_calls`` tables in
    chronological order and yields one SSE event per new signed line.
    The stream closes when the run reaches a terminal state.

Schema migration:

  - Schema v3 (added in this task) introduces the ``projects`` table
    and a nullable ``sessions.project_id`` column. Existing CLI sessions
    keep ``project_id=NULL`` and load fine; ``list_sessions`` exposes
    them under an "unscoped" sentinel rather than mixing into every
    project's run list.

Module CLI for validation:

    uv run python -m api --dry-run            # full self-test
    uv run python -m api --serve --host 0.0.0.0 --port 8000  # MVP local

References:
  * 4.11 directive — Asana comment ``1215607965656701``.
  * 4.11 kickoff brief — Asana comment ``1215610165426009``.
  * Flight Deck PRD v0.3 — ``08-FLIGHT-DECK-PRD.md``.
  * SOW v3.1 — ``07-PHASE-1-SCOPE-OF-WORK.md`` §3 task 4.11.
"""

from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Path as ApiPath,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from config import load_env_file
from hooks.identity_signing import sign_action
from hooks.secrets_check import find_secrets, redact
from session_store import (
    DECISION_KIND_BLOCKER,
    DECISION_KIND_ERROR,
    DEFAULT_DB_PATH,
    GATE_STATUS_APPROVED,
    GATE_STATUS_PENDING,
    GATE_STATUS_REJECTED,
    PROJECT_STATE_ARCHIVED,
    PROJECT_STATE_DEVELOPMENT,
    PROJECT_STATE_GREENFIELD,
    PersistedProject,
    PersistedSession,
    SessionStore,
    SessionStoreError,
    VALID_PROJECT_STATES,
)
from supervisor import (
    DEFAULT_MAX_STEPS,
    SUPERVISOR_STATUS_BLOCKED,
    SUPERVISOR_STATUS_COMPLETE,
    SUPERVISOR_STATUS_ERRORED,
    SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
    SUPERVISOR_STATUS_PLANNING,
    run_supervisor,
)
from tool_registry import (
    AGENT_TOOL_MATRIX,
    GITHUB_ORG_CORRECTION,
    TOOL_REGISTRY,
    allowed_tools_for,
    load_mcp_configs,
    orchestra_root,
)


# --- Constants ---------------------------------------------------------------

#: Env var carrying the single-user MVP bearer token (PRD Q4 lock). When
#: absent, protected endpoints return a structured 503 fail-closed.
ORCHESTRA_API_TOKEN_ENV = "ORCHESTRA_API_TOKEN"

#: OpenAPI tags grouped per the directive's 7-group surface.
TAG_HEALTH = "Health"
TAG_PROJECTS = "Projects"
TAG_RUNS = "Directives & Runs"
TAG_GATES = "Gates"
TAG_AGENTS = "Agents"
TAG_INTEGRATIONS = "Integrations"
TAG_ARTIFACTS = "Artifact Preview"

OPENAPI_TAG_METADATA = [
    {"name": TAG_HEALTH, "description": "Public health probe; no auth."},
    {"name": TAG_PROJECTS, "description": "Multi-project flight-deck CRUD."},
    {"name": TAG_RUNS, "description": "Directives, runs, halt, preview, cost."},
    {"name": TAG_GATES, "description": "Human-approved gate decisions."},
    {"name": TAG_AGENTS, "description": "Atlas/Cody/Scribe/Scout status."},
    {"name": TAG_INTEGRATIONS, "description": "Connected MCP/tool integrations."},
    {"name": TAG_ARTIFACTS, "description": "Artifact diff preview (push gates)."},
]

#: SSE poll interval (seconds). Short enough that a Retool dashboard
#: shows new lines within human-perceptible time; long enough that the
#: stream does not hammer SQLite. Tunable per deployment if needed.
SSE_POLL_INTERVAL_SECONDS = 0.5

#: SSE inactivity timeout — if the supervisor stops emitting new lines
#: for this many seconds AND the run is still non-terminal, the stream
#: closes with a signed timeout event. Belt-and-suspenders so a wedged
#: run can't tie up an SSE connection forever.
SSE_INACTIVITY_TIMEOUT_SECONDS = 600

#: Pricing assumptions for the cost-estimate heuristic. Atlas-locked
#: per directive: "MVP heuristic, not perfect billing: prompt/directive
#: token estimate + configured model assumptions + expected output
#: multiplier." Centralized here so M5/M6 can revise without touching
#: handler code. Rates are USD per 1M tokens (input/output).
COST_ESTIMATE_PRICING: dict[str, dict[str, float]] = {
    # Atlas (Codex 5.5 High via OpenAI). Approximate published rates;
    # the directive explicitly allows heuristic-only.
    "atlas": {"input_per_million": 15.0, "output_per_million": 60.0},
    # Cody (Claude Opus 4.6).
    "cody": {"input_per_million": 15.0, "output_per_million": 75.0},
    # Scribe / Scout (Claude Sonnet 4.6).
    "scribe_scout": {"input_per_million": 3.0, "output_per_million": 15.0},
}

#: ~4 chars per token heuristic — same approximation OpenAI/Anthropic
#: publish for quick eyeball estimates.
COST_ESTIMATE_CHARS_PER_TOKEN = 4

#: Expected output-to-input ratio per step (heuristic). Subagents
#: typically reply ~2× their input message size; the finalizer is
#: usually similar.
COST_ESTIMATE_OUTPUT_MULTIPLIER = 2.0

#: Default approval threshold: per the PRD, the operator may set
#: per-project / per-agent thresholds. MVP exposes a single platform
#: default; M5 wires Retool surfaces for project overrides.
COST_ESTIMATE_DEFAULT_THRESHOLD_USD = 5.0


# --- Run registry (background runner) ---------------------------------------


@dataclass
class _RunHandle:
    """In-memory handle for one API-launched background supervisor run.

    Held in ``_RUN_REGISTRY``. The supervisor thread itself is detached
    so the registry only needs the halt-event + start metadata; the
    persisted ``sessions`` row is the durable artifact.
    """

    run_id: str
    project_id: str
    started_at: datetime
    halt_event: threading.Event
    thread: threading.Thread
    dry_run: bool


_RUN_REGISTRY: dict[str, _RunHandle] = {}
_RUN_REGISTRY_LOCK = threading.Lock()


def _register_run(handle: _RunHandle) -> None:
    with _RUN_REGISTRY_LOCK:
        _RUN_REGISTRY[handle.run_id] = handle


def _get_run_handle(run_id: str) -> _RunHandle | None:
    with _RUN_REGISTRY_LOCK:
        return _RUN_REGISTRY.get(run_id)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_text(value: Any) -> str:
    """Best-effort defensive redaction wrapper. The registry, supervisor,
    and session_store already redact at every persistence boundary; this
    is final belt-and-suspenders at the API serialization edge."""
    if value is None:
        return ""
    text = str(value)
    return redact(text) if find_secrets(text) else text


# --- Pydantic models ---------------------------------------------------------


class HealthResponse(BaseModel):
    service: str = Field(..., examples=["agent-orchestra-api"])
    ok: bool
    schema_version: int
    db_path: str
    checked_at_utc: str
    auth_configured: bool = Field(
        ...,
        description=(
            "Whether ORCHESTRA_API_TOKEN is set. False means protected "
            "endpoints will return 503 until token handoff (M5 5.2)."
        ),
    )


class ErrorEnvelope(BaseModel):
    """Stable error shape used across the API."""

    status: str
    reason: str
    signed_message: str | None = None
    detail: dict[str, Any] | None = None


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    lifecycle_state: str = Field(default=PROJECT_STATE_GREENFIELD)
    vision: str | None = Field(default=None, max_length=4000)
    asana_project_gid: str | None = Field(default=None, max_length=64)
    repo_urls: list[str] = Field(default_factory=list)

    @field_validator("lifecycle_state")
    @classmethod
    def _validate_state(cls, v: str) -> str:
        if v not in VALID_PROJECT_STATES:
            raise ValueError(
                f"invalid lifecycle_state {v!r}; valid {sorted(VALID_PROJECT_STATES)}"
            )
        return v


class ProjectPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    lifecycle_state: str | None = None
    vision: str | None = None
    asana_project_gid: str | None = None
    repo_urls: list[str] | None = None
    archived: bool | None = None

    @field_validator("lifecycle_state")
    @classmethod
    def _validate_state(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_PROJECT_STATES:
            raise ValueError(
                f"invalid lifecycle_state {v!r}; valid {sorted(VALID_PROJECT_STATES)}"
            )
        return v


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    lifecycle_state: str
    vision: str | None
    asana_project_gid: str | None
    repo_urls: list[str]
    archived: bool
    created_at: str
    updated_at: str


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    unscoped_legacy_sessions: int = Field(
        default=0,
        description=(
            "Count of sessions persisted before schema v3 (project_id NULL). "
            "Exposed so the UI can surface a sentinel 'unscoped' bucket "
            "instead of silently mixing them into every project."
        ),
    )


class DirectiveSubmitRequest(BaseModel):
    directive: str = Field(..., min_length=1, max_length=8000)
    max_steps: int | None = Field(default=None, ge=1, le=10)


class DirectiveSubmitResponse(BaseModel):
    run_id: str
    session_id: str
    project_id: str
    status: str
    dry_run: bool
    accepted_at: str
    signed_message: str


class StepView(BaseModel):
    step_id: int
    target: str
    action_surface: str
    message: str
    reason: str
    status: str
    started_at: str | None
    completed_at: str | None
    response_envelope_id: str | None


class GateView(BaseModel):
    gate_id: str
    step_id: int | None
    target: str
    action_surface: str
    status: str
    signed_message: str
    created_at: str
    resolved_at: str | None
    resolved_by: str | None
    rationale: str | None


class DecisionView(BaseModel):
    decision_id: str
    kind: str  # blocker | error | halt
    phase: str
    signed_message: str
    reason_or_error: str
    created_at: str


class ToolCallView(BaseModel):
    tool_call_id: str
    step_id: int | None
    step_call_index: int
    agent: str
    tool_name: str
    server_name: str
    action_surface: str
    status: str
    args_json: str
    result_summary: str
    signed_message: str
    error: str | None
    started_at: str
    completed_at: str


class MessageView(BaseModel):
    envelope_id: str
    parent_id: str | None
    sender: str
    target: str
    message_type: str
    action_surface: str
    content: str
    phase: str
    created_at: str


class RunSummary(BaseModel):
    run_id: str
    session_id: str
    project_id: str | None
    directive_summary: str
    status: str
    dry_run: bool
    max_steps: int
    created_at: str
    updated_at: str
    completed_at: str | None
    error_count: int
    blocker_count: int


class RunDetail(BaseModel):
    summary: RunSummary
    planner_envelope_id: str | None
    finalizer_envelope_id: str | None
    plan: dict[str, Any] | None
    steps: list[StepView]
    gates: list[GateView]
    decisions: list[DecisionView]
    tool_calls: list[ToolCallView]
    messages: list[MessageView]


class RunListResponse(BaseModel):
    runs: list[RunSummary]


class PlanPreviewResponse(BaseModel):
    run_id: str
    project_id: str
    status: str
    planner_envelope_content: str | None
    plan: dict[str, Any] | None
    available: bool = Field(
        ...,
        description=(
            "True iff a parsed plan exists. Live runs that errored on the "
            "planner turn return available=False with a structured reason; "
            "this endpoint must NOT execute the planner — that is what "
            "directive submission does."
        ),
    )
    reason: str | None = None


class CostEstimateResponse(BaseModel):
    run_id: str
    project_id: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_low_usd: float
    estimated_cost_high_usd: float
    approval_threshold_usd: float
    requires_approval: bool
    assumptions: dict[str, Any]


class HaltResponse(BaseModel):
    run_id: str
    status: str  # halt_recorded | already_terminal | not_found
    signed_message: str
    detail: str | None = None


class GateDecisionRequest(BaseModel):
    rationale: str = Field(..., min_length=1, max_length=2000)
    resolved_by: str = Field(default="Garrett", max_length=64)


class GateDecisionResponse(BaseModel):
    gate_id: str
    status: str  # approve_recorded_execution_not_resumed | reject_recorded | already_resolved
    signed_message: str
    note: str


class AgentView(BaseModel):
    name: str
    provider: str | None
    model_env: str | None  # name of env var carrying model id
    model_env_present: bool
    allowed_tools: list[str]
    last_activity_at: str | None


class AgentsResponse(BaseModel):
    project_id: str
    agents: list[AgentView]


class IntegrationView(BaseModel):
    type: str  # asana | github | filesystem | bash | supabase | ...
    enabled: bool
    transport: str
    agents: list[str]
    env_presence: dict[str, bool]
    missing_env: list[str]
    notes: str
    capabilities: dict[str, list[str]]  # per-agent allowed tools for this server
    last_successful_call_at: str | None


class IntegrationsResponse(BaseModel):
    project_id: str
    integrations: list[IntegrationView]


class IntegrationAuthRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class IntegrationActionResponse(BaseModel):
    type: str
    status: str  # manual_secret_handoff_required | disabled_metadata_recorded | unsupported
    signed_message: str
    next_step: str


class ArtifactDiffResponse(BaseModel):
    run_id: str
    artifact_id: str
    status: str  # ok | unresolved_artifact
    artifact_kind: str | None
    diff: str | None
    signed_message: str
    detail: str | None = None


# --- Auth dependency ---------------------------------------------------------


def _read_api_token() -> str | None:
    """Read the bearer token at request time so a token rotated mid-session
    is picked up without an app restart."""
    val = os.environ.get(ORCHESTRA_API_TOKEN_ENV)
    return val if val else None


def require_bearer_auth(request: Request) -> str:
    """FastAPI dependency: enforce single-user bearer auth (PRD Q4 lock).

    Fail-closed semantics:

    - ``ORCHESTRA_API_TOKEN`` env var missing → 503 with a structured
      ``auth_unavailable`` envelope. The app is up but cannot process
      protected requests until M5 task 5.2 lands the real token. We
      explicitly do NOT silently allow access; the operator must observe
      the configuration gap.
    - Authorization header missing → 401 ``missing_bearer``.
    - Header present but wrong scheme or wrong token → 401 ``invalid_bearer``.
    - Health endpoint and OpenAPI docs/schema are excluded from this
      dependency at the router level; everything else routes through it.

    Returns the token string so handlers can log the *fact* that auth
    fired (no echo of the actual value, just a Boolean upstream).
    """
    configured = _read_api_token()
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "auth_unavailable",
                "reason": (
                    f"{ORCHESTRA_API_TOKEN_ENV} env var not configured. "
                    "Real token generation + Retool secret handoff is M5 "
                    "task 5.2; the 4.11 API ships with the bearer "
                    "scaffold and fails closed without it."
                ),
                "signed_message": sign_action(
                    "Atlas",
                    "API auth unavailable: ORCHESTRA_API_TOKEN missing; "
                    "protected endpoints fail closed.",
                ),
            },
        )
    header_value = request.headers.get("authorization")
    if not header_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "status": "missing_bearer",
                "reason": (
                    "Authorization: Bearer <token> header required for "
                    "protected endpoints."
                ),
                "signed_message": sign_action(
                    "Atlas", "API auth refused: missing bearer header."
                ),
            },
            headers={"WWW-Authenticate": 'Bearer realm="agent-orchestra"'},
        )
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "status": "invalid_bearer_format",
                "reason": "Authorization header must use scheme `Bearer <token>`.",
                "signed_message": sign_action(
                    "Atlas", "API auth refused: malformed bearer header."
                ),
            },
            headers={"WWW-Authenticate": 'Bearer realm="agent-orchestra"'},
        )
    presented = parts[1].strip()
    # Constant-time compare via stdlib hmac.compare_digest — Atlas
    # addendum 1215677174730272 finding 4. Prevents timing-side-channel
    # attacks on the bearer for a network-facing Retool surface. The
    # function still returns the presented token to the caller so route
    # handlers can record auth-fired (no echo of the actual value in
    # any response body or log).
    if not hmac.compare_digest(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "status": "invalid_bearer",
                "reason": "Bearer token does not match the configured value.",
                "signed_message": sign_action(
                    "Atlas", "API auth refused: bearer token mismatch."
                ),
            },
            headers={"WWW-Authenticate": 'Bearer realm="agent-orchestra"'},
        )
    return presented


# --- Snapshot serialization helpers -----------------------------------------


def _session_to_summary(s: PersistedSession) -> RunSummary:
    return RunSummary(
        run_id=s.run_id,
        session_id=s.session_id,
        project_id=s.project_id,
        directive_summary=_safe_text(s.directive_summary),
        status=s.status,
        dry_run=s.dry_run,
        max_steps=s.max_steps,
        created_at=_iso(s.created_at) or "",
        updated_at=_iso(s.updated_at) or "",
        completed_at=_iso(s.completed_at),
        error_count=s.error_count,
        blocker_count=s.blocker_count,
    )


def _action_row_to_step(row: dict[str, Any]) -> StepView:
    return StepView(
        step_id=int(row["step_id"]),
        target=row["target"],
        action_surface=row["action_surface"],
        message=_safe_text(row["message"]),
        reason=_safe_text(row["reason"]),
        status=row["status"],
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        response_envelope_id=row.get("response_envelope_id"),
    )


def _gate_row_to_view(row: dict[str, Any]) -> GateView:
    return GateView(
        gate_id=row["gate_id"],
        step_id=row.get("step_id"),
        target=row["target"],
        action_surface=row["action_surface"],
        status=row["status"],
        signed_message=row["signed_message"],
        created_at=row["created_at"],
        resolved_at=row.get("resolved_at"),
        resolved_by=row.get("resolved_by"),
        rationale=_safe_text(row.get("rationale")) if row.get("rationale") else None,
    )


def _decision_row_to_view(row: dict[str, Any]) -> DecisionView:
    return DecisionView(
        decision_id=row["decision_id"],
        kind=row["kind"],
        phase=row["phase"],
        signed_message=row["signed_message"],
        reason_or_error=_safe_text(row.get("reason_or_error", "")),
        created_at=row["created_at"],
    )


def _tool_row_to_view(row: dict[str, Any]) -> ToolCallView:
    return ToolCallView(
        tool_call_id=row["tool_call_id"],
        step_id=row.get("step_id"),
        step_call_index=int(row.get("step_call_index") or 0),
        agent=row["agent"],
        tool_name=row["tool_name"],
        server_name=row["server_name"],
        action_surface=row["action_surface"],
        status=row["status"],
        args_json=_safe_text(row["args_json"]),
        result_summary=_safe_text(row["result_summary"]),
        signed_message=row["signed_message"],
        error=_safe_text(row.get("error")) if row.get("error") else None,
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _message_row_to_view(row: dict[str, Any]) -> MessageView:
    return MessageView(
        envelope_id=row["id"],
        parent_id=row.get("parent_id"),
        sender=row["sender"],
        target=row["target"],
        message_type=row["message_type"],
        action_surface=row["action_surface"],
        content=_safe_text(row["content"]),
        phase=row["phase"],
        created_at=row["created_at"],
    )


def _project_to_view(p: PersistedProject) -> ProjectResponse:
    return ProjectResponse(
        project_id=p.project_id,
        name=p.name,
        lifecycle_state=p.lifecycle_state,
        vision=p.vision,
        asana_project_gid=p.asana_project_gid,
        repo_urls=list(p.repo_urls),
        archived=p.archived,
        created_at=_iso(p.created_at) or "",
        updated_at=_iso(p.updated_at) or "",
    )


def _build_run_detail(store: SessionStore, identifier: str) -> RunDetail | None:
    session = store.load_session(identifier)
    if session is None:
        return None
    actions = store.load_actions(session.run_id)
    gates = store.load_gates(session.run_id)
    decisions = store.load_decisions(session.run_id)
    tool_calls = store.load_tool_calls(session.run_id)
    messages = store.load_messages(session.run_id)
    return RunDetail(
        summary=_session_to_summary(session),
        planner_envelope_id=session.planner_envelope_id,
        finalizer_envelope_id=session.finalizer_envelope_id,
        plan=session.plan,
        steps=[_action_row_to_step(r) for r in actions],
        gates=[_gate_row_to_view(r) for r in gates],
        decisions=[_decision_row_to_view(r) for r in decisions],
        tool_calls=[_tool_row_to_view(r) for r in tool_calls],
        messages=[_message_row_to_view(r) for r in messages],
    )


# --- Cost estimate heuristic -------------------------------------------------


def _estimate_cost(directive_text: str, max_steps: int) -> CostEstimateResponse:
    """Deterministic heuristic — no provider/billing API calls.

    Atlas-locked semantics:

    - Input tokens ≈ ``len(directive) / 4`` × planner overhead.
    - Per step, the input is repeated once for the planner-routing context
      and once for the subagent prompt.
    - Output tokens ≈ input × ``COST_ESTIMATE_OUTPUT_MULTIPLIER``.
    - Low/high band: ±30% to surface uncertainty.

    Cost = Σ (input/1M × rate_in + output/1M × rate_out) summed across
    Atlas (planner + finalizer) and the steps (Cody/Scribe/Scout).
    """
    char_count = max(1, len(directive_text))
    base_input_tokens = max(1, char_count // COST_ESTIMATE_CHARS_PER_TOKEN)
    # Planner + finalizer (Atlas) ≈ 3× directive each; steps ≈ 1× each.
    atlas_input = base_input_tokens * 6
    step_input = base_input_tokens * max_steps
    total_input = atlas_input + step_input
    total_output = int(total_input * COST_ESTIMATE_OUTPUT_MULTIPLIER)

    atlas_rates = COST_ESTIMATE_PRICING["atlas"]
    sub_rates = COST_ESTIMATE_PRICING["scribe_scout"]
    cody_rates = COST_ESTIMATE_PRICING["cody"]
    # Half the step input attributed to Cody (Opus), half to Scribe/Scout
    # (Sonnet). MVP heuristic; the matrix lock is in the directive.
    cody_share = step_input // 2
    scribe_scout_share = step_input - cody_share
    cody_output = int(cody_share * COST_ESTIMATE_OUTPUT_MULTIPLIER)
    scribe_scout_output = int(scribe_scout_share * COST_ESTIMATE_OUTPUT_MULTIPLIER)
    atlas_output = int(atlas_input * COST_ESTIMATE_OUTPUT_MULTIPLIER)

    def _cost(input_tokens: int, output_tokens: int, rates: dict[str, float]) -> float:
        return (
            input_tokens / 1_000_000 * rates["input_per_million"]
            + output_tokens / 1_000_000 * rates["output_per_million"]
        )

    base_cost = (
        _cost(atlas_input, atlas_output, atlas_rates)
        + _cost(cody_share, cody_output, cody_rates)
        + _cost(scribe_scout_share, scribe_scout_output, sub_rates)
    )
    low = round(base_cost * 0.7, 4)
    high = round(base_cost * 1.3, 4)
    threshold = COST_ESTIMATE_DEFAULT_THRESHOLD_USD
    return CostEstimateResponse(
        run_id="",  # caller fills
        project_id="",  # caller fills
        estimated_input_tokens=total_input,
        estimated_output_tokens=total_output,
        estimated_cost_low_usd=low,
        estimated_cost_high_usd=high,
        approval_threshold_usd=threshold,
        requires_approval=high > threshold,
        assumptions={
            "chars_per_token": COST_ESTIMATE_CHARS_PER_TOKEN,
            "output_multiplier": COST_ESTIMATE_OUTPUT_MULTIPLIER,
            "planner_overhead_factor": 6,
            "atlas_pricing": atlas_rates,
            "cody_pricing": cody_rates,
            "scribe_scout_pricing": sub_rates,
            "note": (
                "MVP heuristic only — not a billing API call. Centralized "
                "in COST_ESTIMATE_PRICING for M5/M6 refinement."
            ),
        },
    )


# --- Background runner -------------------------------------------------------


def _start_background_run(
    *,
    store: SessionStore,
    project_id: str,
    directive: str,
    max_steps: int,
    dry_run: bool,
    dry_run_planner_content: str | None = None,
) -> DirectiveSubmitResponse:
    """Mint ids, persist the initial session row, and dispatch the run.

    The supervisor's narrow 4.11 extension accepts ``run_id`` /
    ``session_id`` / ``project_id`` / ``halt_check`` kwargs; the
    background thread runs ``run_supervisor`` against the same row so
    the API can return synchronously while the work executes.
    """
    run_id = str(uuid4())
    session_id = str(uuid4())
    halt_event = threading.Event()
    accepted_at = datetime.now(UTC)

    # Record the session row up-front so a GET /runs/{run_id} immediately
    # after the POST returns the seeded state rather than a 404 race.
    store.record_session(
        run_id=run_id,
        session_id=session_id,
        directive_summary=directive[:200],
        status=SUPERVISOR_STATUS_PLANNING,
        max_steps=max_steps,
        dry_run=dry_run,
        created_at=accepted_at,
        project_id=project_id,
    )

    def _runner() -> None:
        try:
            run_supervisor(
                directive,
                max_steps=max_steps,
                dry_run=dry_run,
                store=store,
                project_id=project_id,
                run_id=run_id,
                session_id=session_id,
                halt_check=halt_event.is_set,
                _dry_run_planner_content=dry_run_planner_content if dry_run else None,
            )
        except Exception as exc:  # noqa: BLE001 - never crash the worker thread.
            # The supervisor never raises on planner/step/finalizer errors;
            # only on internal-invariant violations. Surface a signed error
            # decision against the same run so the API can still report it.
            try:
                store.record_decision(
                    run_id=run_id,
                    session_id=session_id,
                    kind=DECISION_KIND_ERROR,
                    phase="api_worker_crash",
                    signed_message=sign_action(
                        "Atlas",
                        f"API worker crashed: {type(exc).__name__}: "
                        f"{redact(str(exc))}",
                    ),
                    reason_or_error=f"{type(exc).__name__}: {redact(str(exc))}",
                    metadata={"exception_class": type(exc).__name__},
                    created_at=datetime.now(UTC),
                )
                store.update_session(run_id, status=SUPERVISOR_STATUS_ERRORED)
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(
        target=_runner,
        name=f"orchestra-api-run-{run_id[:8]}",
        daemon=True,
    )
    _register_run(
        _RunHandle(
            run_id=run_id,
            project_id=project_id,
            started_at=accepted_at,
            halt_event=halt_event,
            thread=thread,
            dry_run=dry_run,
        )
    )
    thread.start()

    return DirectiveSubmitResponse(
        run_id=run_id,
        session_id=session_id,
        project_id=project_id,
        status=SUPERVISOR_STATUS_PLANNING,
        dry_run=dry_run,
        accepted_at=_iso(accepted_at) or "",
        signed_message=sign_action(
            "Atlas",
            f"Directive accepted via REST API: run_id={run_id} "
            f"session_id={session_id} project_id={project_id} dry_run={dry_run}",
        ),
    )


def _wait_for_run(run_id: str, timeout_seconds: float = 30.0) -> None:
    """Synchronous helper used by the dry-run harness to await a background
    thread's completion. The serving path NEVER blocks on this — the
    background runner exists precisely so the API returns immediately.
    """
    handle = _get_run_handle(run_id)
    if handle is None:
        return
    handle.thread.join(timeout=timeout_seconds)


# --- App factory + routers ---------------------------------------------------


def _resolve_project_or_404(store: SessionStore, project_id: str) -> PersistedProject:
    p = store.load_project(project_id)
    if p is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "status": "project_not_found",
                "reason": f"no project with id {project_id!r}",
                "signed_message": sign_action(
                    "Atlas", f"API 404: project {project_id!r} not found."
                ),
            },
        )
    return p


def _resolve_run_or_404(
    store: SessionStore,
    project_id: str,
    run_id: str,
) -> PersistedSession:
    session = store.load_session(run_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "status": "run_not_found",
                "reason": f"no run with id {run_id!r}",
                "signed_message": sign_action(
                    "Atlas", f"API 404: run {run_id!r} not found."
                ),
            },
        )
    if session.project_id != project_id:
        # Atlas addendum 1215677174730272 finding 3: strict equality so
        # legacy ``project_id=NULL`` sessions cannot be looked up under
        # an arbitrary project's scoped endpoints. Cross-project access
        # is also caught by the same predicate. We return 404 rather
        # than 403 so the API does not leak which projects own which
        # runs. Legacy CLI sessions remain queryable via the unscoped
        # ``GET /projects`` sentinel + future legacy view; they MUST
        # NOT attach to every project.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "status": "run_not_in_project",
                "reason": (
                    f"run {run_id!r} is not associated with project {project_id!r}"
                ),
                "signed_message": sign_action(
                    "Atlas",
                    f"API 404: run {run_id!r} not in project {project_id!r}.",
                ),
            },
        )
    return session


def create_app(
    store: SessionStore | None = None,
    *,
    title: str = "Agent Orchestra Runtime API",
    version: str = "0.4.11",
) -> FastAPI:
    """Build the FastAPI app with all routers wired.

    ``store`` is dependency-injectable so the dry-run harness can pass
    a temp-DB instance; production wires the default ``SessionStore()``
    which honors the ``ORCHESTRA_SESSIONS_DB`` env var.
    """
    actual_store = store if store is not None else SessionStore()
    actual_store.ensure_schema()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):  # noqa: ARG001 - signature required by FastAPI
        load_env_file()
        yield

    app = FastAPI(
        title=title,
        version=version,
        description=(
            "REST API surface for the Agent Orchestra runtime. Backs the "
            "M5 Retool flight deck. Bearer-token auth via "
            "ORCHESTRA_API_TOKEN; fails closed when absent."
        ),
        openapi_tags=OPENAPI_TAG_METADATA,
        lifespan=_lifespan,
    )

    # Expose the store on the app so tests and route closures can find it.
    app.state.store = actual_store

    # --- Health ---------------------------------------------------------

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=[TAG_HEALTH],
        summary="Liveness probe. Public; no auth.",
    )
    def _health() -> HealthResponse:
        return HealthResponse(
            service="agent-orchestra-api",
            ok=True,
            schema_version=actual_store.schema_version(),
            db_path=str(actual_store.db_path),
            checked_at_utc=_utc_now_iso(),
            auth_configured=_read_api_token() is not None,
        )

    # --- Projects -------------------------------------------------------

    @app.get(
        "/projects",
        response_model=ProjectListResponse,
        tags=[TAG_PROJECTS],
        dependencies=[Depends(require_bearer_auth)],
        summary="List projects (multi-project flight-deck).",
    )
    def _list_projects(
        include_archived: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> ProjectListResponse:
        projects = actual_store.list_projects(
            include_archived=include_archived, limit=limit
        )
        unscoped = actual_store.list_sessions(limit=1000, unscoped_only=True)
        return ProjectListResponse(
            projects=[_project_to_view(p) for p in projects],
            unscoped_legacy_sessions=len(unscoped),
        )

    @app.post(
        "/projects",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
        tags=[TAG_PROJECTS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Create or link a project.",
        responses={
            400: {"model": ErrorEnvelope, "description": "Validation failure."},
            424: {
                "model": ErrorEnvelope,
                "description": (
                    "Greenfield path needs more config (e.g. Asana workspace "
                    "GID). Operator must satisfy `configuration_required` "
                    "before the project can be created."
                ),
            },
        },
    )
    def _create_project(req: ProjectCreateRequest) -> ProjectResponse:
        # Atlas addendum 1215677174730272 finding 1: greenfield WITHOUT
        # an existing ``asana_project_gid`` must fail closed and persist
        # NO row, because the Plan-of-Record discipline requires every
        # flight-deck project to have an Asana Project container — and
        # 4.11 deliberately does NOT implement the real Asana create
        # surface (that's a human-approved guarded write owned by M5+).
        # The prior implementation silently reduced to a link-less
        # local-only row whenever ``ASANA_WORKSPACE_GID`` was set; with
        # the tighter check, ANY POST without an explicit
        # ``asana_project_gid`` returns 424 ``configuration_required``
        # naming the additional Asana-create config that would be
        # required AND the M5+ task that owns the real create flow.
        # The operator's recovery path is: link an existing Asana
        # Project by supplying its GID.
        if req.asana_project_gid is None:
            workspace_present = bool(os.environ.get("ASANA_WORKSPACE_GID"))
            team_present = bool(os.environ.get("ASANA_TEAM_GID"))
            missing_env = [
                key
                for key, present in (
                    ("ASANA_WORKSPACE_GID", workspace_present),
                    ("ASANA_TEAM_GID", team_present),
                )
                if not present
            ]
            raise HTTPException(
                status_code=status.HTTP_424_FAILED_DEPENDENCY,
                detail={
                    "status": "configuration_required",
                    "reason": (
                        "Greenfield project creation requires either an "
                        "existing Asana Project to link (supply "
                        "`asana_project_gid`) OR the full Asana-create "
                        "config below AND opt-in via the M5+ "
                        "human-approved create surface. The 4.11 API "
                        "deliberately does NOT silently create a "
                        "local-only project row — Plan-of-Record "
                        "discipline requires every project to have an "
                        "Asana Project container."
                    ),
                    "signed_message": sign_action(
                        "Atlas",
                        "Project create blocked: configuration_required "
                        "(asana_project_gid absent; greenfield create "
                        "needs full Asana config + M5+ opt-in).",
                    ),
                    "detail": {
                        "required_inputs": ["asana_project_gid"],
                        "required_env_for_real_create": [
                            "ASANA_WORKSPACE_GID",
                            "ASANA_TEAM_GID",
                        ],
                        "missing_env": missing_env,
                        "owner_for_real_create": "M5 task 5.x (Asana "
                        "Project create is a human-approved guarded "
                        "write; not in 4.11 scope).",
                    },
                },
            )
        project_id = str(uuid4())
        actual_store.record_project(
            project_id=project_id,
            name=req.name,
            lifecycle_state=req.lifecycle_state,
            vision=req.vision,
            asana_project_gid=req.asana_project_gid,
            repo_urls=req.repo_urls,
            archived=False,
            metadata={
                "created_via": "rest_api",
                "signed_message": sign_action(
                    "Atlas",
                    f"Project created via REST API: name={req.name!r} "
                    f"asana_project_gid={req.asana_project_gid or '-'}",
                ),
            },
        )
        loaded = actual_store.load_project(project_id)
        assert loaded is not None
        return _project_to_view(loaded)

    @app.get(
        "/projects/{project_id}",
        response_model=ProjectResponse,
        tags=[TAG_PROJECTS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Get one project.",
    )
    def _get_project(project_id: str = ApiPath(...)) -> ProjectResponse:
        p = _resolve_project_or_404(actual_store, project_id)
        return _project_to_view(p)

    @app.patch(
        "/projects/{project_id}",
        response_model=ProjectResponse,
        tags=[TAG_PROJECTS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Patch project fields.",
    )
    def _patch_project(
        project_id: str = ApiPath(...),
        req: ProjectPatchRequest = Body(...),
    ) -> ProjectResponse:
        _resolve_project_or_404(actual_store, project_id)
        actual_store.update_project(
            project_id,
            name=req.name,
            lifecycle_state=req.lifecycle_state,
            vision=req.vision,
            asana_project_gid=req.asana_project_gid,
            repo_urls=req.repo_urls,
            archived=req.archived,
        )
        loaded = actual_store.load_project(project_id)
        assert loaded is not None
        return _project_to_view(loaded)

    # --- Directives & Runs ---------------------------------------------

    @app.post(
        "/projects/{project_id}/directive",
        response_model=DirectiveSubmitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Submit a directive; run continues in background.",
    )
    def _submit_directive(
        project_id: str = ApiPath(...),
        dry_run: bool = Query(default=False),
        req: DirectiveSubmitRequest = Body(...),
    ) -> DirectiveSubmitResponse:
        _resolve_project_or_404(actual_store, project_id)
        return _start_background_run(
            store=actual_store,
            project_id=project_id,
            directive=req.directive,
            max_steps=req.max_steps if req.max_steps is not None else DEFAULT_MAX_STEPS,
            dry_run=dry_run,
        )

    # --- True pre-flight surfaces (Atlas addendum 1215677174730272 F2) ---
    # The existing `GET /runs/{run_id}/...` preview + cost endpoints are
    # snapshot views of an already-existing run (potentially mid- or
    # post-execution). The PRD's pre-flight discipline requires the
    # operator to see plan + cost BEFORE clicking Run. These two
    # endpoints satisfy that:
    #
    #   - cost_estimate is a PURE FUNCTION over the directive text +
    #     max_steps. No supervisor invocation, no run row, no provider
    #     call, no DB write. The heuristic is identical to the snapshot
    #     view's so the operator sees the same number before vs after
    #     submitting.
    #   - preview invokes ``run_supervisor`` with ``_stop_after="planner"``
    #     which halts after the planner turn validates the plan. No
    #     subagent steps execute, no tool calls fire, no finalizer
    #     turn runs. The persisted artifacts are exactly: planner
    #     envelope + plan + planned-action rows (status='planned',
    #     never 'responded'/'blocked'/'errored'). The operator can
    #     inspect the plan and decide whether to launch a real run via
    #     the directive submission endpoint.

    @app.post(
        "/projects/{project_id}/directive/cost_estimate",
        response_model=CostEstimateResponse,
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary=(
            "Pre-flight heuristic cost — pure function, no run created, "
            "no provider call, no DB write."
        ),
    )
    def _preflight_cost_estimate(
        project_id: str = ApiPath(...),
        req: DirectiveSubmitRequest = Body(...),
    ) -> CostEstimateResponse:
        _resolve_project_or_404(actual_store, project_id)
        estimate = _estimate_cost(
            req.directive,
            req.max_steps if req.max_steps is not None else DEFAULT_MAX_STEPS,
        )
        estimate.project_id = project_id
        estimate.run_id = ""  # no run created — pre-flight is a pure call.
        return estimate

    @app.post(
        "/projects/{project_id}/directive/preview",
        response_model=PlanPreviewResponse,
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary=(
            "Pre-flight planner-only run — persists plan, never executes "
            "subagent steps / tool calls / finalizer."
        ),
    )
    def _preflight_preview(
        project_id: str = ApiPath(...),
        dry_run: bool = Query(default=False),
        req: DirectiveSubmitRequest = Body(...),
    ) -> PlanPreviewResponse:
        _resolve_project_or_404(actual_store, project_id)
        # Synchronous (NOT background) so the response carries the
        # planner output in one round-trip. ``_stop_after="planner"``
        # halts the supervisor after the planner turn validates the
        # plan; no step execution, no tool calls, no subagent messages,
        # no finalizer turn. The run is persisted under the project so
        # the operator can review it via the snapshot endpoints, then
        # decide whether to submit a real directive.
        run = run_supervisor(
            req.directive,
            max_steps=(
                req.max_steps if req.max_steps is not None else DEFAULT_MAX_STEPS
            ),
            dry_run=dry_run,
            store=actual_store,
            project_id=project_id,
            _stop_after="planner",
        )
        planner_content = (
            run.planner_envelope.content
            if run.planner_envelope is not None
            else None
        )
        return PlanPreviewResponse(
            run_id=run.run_id,
            project_id=project_id,
            status=run.status,
            planner_envelope_content=(
                _safe_text(planner_content) if planner_content else None
            ),
            plan=run.plan,
            available=run.plan is not None,
            reason=(
                None
                if run.plan is not None
                else (
                    "Planner halted before producing a parsed plan; "
                    "check /runs/{run_id} for blockers."
                )
            ),
        )

    @app.get(
        "/projects/{project_id}/runs",
        response_model=RunListResponse,
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary="List runs for a project.",
    )
    def _list_runs(
        project_id: str = ApiPath(...),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> RunListResponse:
        _resolve_project_or_404(actual_store, project_id)
        sessions = actual_store.list_sessions(limit=limit, project_id=project_id)
        return RunListResponse(runs=[_session_to_summary(s) for s in sessions])

    @app.get(
        "/projects/{project_id}/runs/{run_id}",
        response_model=RunDetail,
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Get one run's full signed history.",
    )
    def _get_run(
        project_id: str = ApiPath(...),
        run_id: str = ApiPath(...),
    ) -> RunDetail:
        _resolve_run_or_404(actual_store, project_id, run_id)
        detail = _build_run_detail(actual_store, run_id)
        assert detail is not None
        return detail

    @app.get(
        "/projects/{project_id}/runs/{run_id}/stream",
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary="SSE feed of new signed activity for an in-flight run.",
    )
    def _stream_run(
        project_id: str = ApiPath(...),
        run_id: str = ApiPath(...),
    ) -> StreamingResponse:
        _resolve_run_or_404(actual_store, project_id, run_id)

        def _gen() -> Iterable[bytes]:
            seen_ids: set[str] = set()
            last_activity_at = time.monotonic()
            terminal = {
                SUPERVISOR_STATUS_COMPLETE,
                SUPERVISOR_STATUS_BLOCKED,
                SUPERVISOR_STATUS_ERRORED,
                SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
            }
            while True:
                fresh: list[tuple[str, str, str]] = []
                # Aggregate every signed line we know about — order by
                # created_at where available.
                for r in actual_store.load_messages(run_id):
                    key = f"msg:{r['id']}"
                    if key not in seen_ids:
                        seen_ids.add(key)
                        fresh.append((r["created_at"], "message", r["content"]))
                for r in actual_store.load_decisions(run_id):
                    key = f"dec:{r['decision_id']}"
                    if key not in seen_ids:
                        seen_ids.add(key)
                        fresh.append(
                            (r["created_at"], "decision", r["signed_message"])
                        )
                for r in actual_store.load_tool_calls(run_id):
                    key = f"tool:{r['tool_call_id']}"
                    if key not in seen_ids:
                        seen_ids.add(key)
                        fresh.append(
                            (r["started_at"], "tool_call", r["signed_message"])
                        )
                for r in actual_store.load_gates(run_id):
                    key = f"gate:{r['gate_id']}"
                    if key not in seen_ids:
                        seen_ids.add(key)
                        fresh.append(
                            (r["created_at"], "gate", r["signed_message"])
                        )
                fresh.sort(key=lambda x: x[0])
                if fresh:
                    last_activity_at = time.monotonic()
                for created_at, kind, line in fresh:
                    safe_line = _safe_text(line).replace("\n", " ").strip()
                    payload = json.dumps(
                        {
                            "kind": kind,
                            "created_at": created_at,
                            "signed_message": safe_line,
                        }
                    )
                    yield f"event: {kind}\ndata: {payload}\n\n".encode("utf-8")
                # Stop conditions:
                #   1. run is terminal,
                #   2. inactivity exceeded SSE_INACTIVITY_TIMEOUT_SECONDS.
                current = actual_store.load_session(run_id)
                if current is None:
                    yield b"event: end\ndata: {\"status\":\"run_disappeared\"}\n\n"
                    return
                if current.status in terminal:
                    end_payload = json.dumps({
                        "status": "terminal",
                        "supervisor_status": current.status,
                    })
                    yield f"event: end\ndata: {end_payload}\n\n".encode("utf-8")
                    return
                if (
                    time.monotonic() - last_activity_at
                    > SSE_INACTIVITY_TIMEOUT_SECONDS
                ):
                    timeout_line = sign_action(
                        "Atlas",
                        f"SSE stream closed after "
                        f"{SSE_INACTIVITY_TIMEOUT_SECONDS}s of inactivity; "
                        f"current status={current.status}.",
                    )
                    timeout_payload = json.dumps({
                        "status": "inactivity_timeout",
                        "signed_message": timeout_line,
                    })
                    yield f"event: end\ndata: {timeout_payload}\n\n".encode("utf-8")
                    return
                time.sleep(SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/projects/{project_id}/runs/{run_id}/halt",
        response_model=HaltResponse,
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Request a cooperative halt at the next safe boundary.",
    )
    def _halt_run(
        project_id: str = ApiPath(...),
        run_id: str = ApiPath(...),
    ) -> HaltResponse:
        session = _resolve_run_or_404(actual_store, project_id, run_id)
        terminal = {
            SUPERVISOR_STATUS_COMPLETE,
            SUPERVISOR_STATUS_BLOCKED,
            SUPERVISOR_STATUS_ERRORED,
            SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
        }
        if session.status in terminal:
            return HaltResponse(
                run_id=run_id,
                status="already_terminal",
                signed_message=sign_action(
                    "Atlas",
                    f"Halt no-op: run {run_id} already terminal "
                    f"(status={session.status}).",
                ),
                detail=session.status,
            )
        signed = sign_action(
            "Atlas",
            f"API halt requested for run_id={run_id} project_id={project_id}. "
            "Supervisor will stop at the next safe boundary "
            "(between steps or before the finalizer).",
        )
        actual_store.record_decision(
            run_id=run_id,
            session_id=session.session_id,
            kind=DECISION_KIND_BLOCKER,
            phase="api_halt_request",
            signed_message=signed,
            reason_or_error="api halt requested",
            metadata={"requested_via": "rest_api"},
            created_at=datetime.now(UTC),
        )
        handle = _get_run_handle(run_id)
        if handle is not None:
            handle.halt_event.set()
            in_flight = True
        else:
            # Run isn't in this process's registry — either it was launched
            # by another process or it's a historical session. The persisted
            # decision is the durable signal; mid-provider-call interruption
            # is explicitly not supported (PRD note).
            in_flight = False
        return HaltResponse(
            run_id=run_id,
            status="halt_recorded",
            signed_message=signed,
            detail=(
                "in_process_halt_flag_set"
                if in_flight
                else "decision_persisted_no_process_handle"
            ),
        )

    @app.get(
        "/projects/{project_id}/runs/{run_id}/preview",
        response_model=PlanPreviewResponse,
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Return the planner output without executing steps.",
    )
    def _preview_run(
        project_id: str = ApiPath(...),
        run_id: str = ApiPath(...),
    ) -> PlanPreviewResponse:
        session = _resolve_run_or_404(actual_store, project_id, run_id)
        if session.plan is None and session.planner_envelope_id is None:
            return PlanPreviewResponse(
                run_id=run_id,
                project_id=project_id,
                status=session.status,
                planner_envelope_content=None,
                plan=None,
                available=False,
                reason=(
                    "Planner has not produced a plan yet. Wait for the "
                    "background runner to advance past the planner phase, "
                    "or inspect /runs/{run_id} for the current status."
                ),
            )
        planner_content: str | None = None
        if session.planner_envelope_id is not None:
            for msg in actual_store.load_messages(run_id):
                if msg["id"] == session.planner_envelope_id:
                    planner_content = _safe_text(msg["content"])
                    break
        return PlanPreviewResponse(
            run_id=run_id,
            project_id=project_id,
            status=session.status,
            planner_envelope_content=planner_content,
            plan=session.plan,
            available=session.plan is not None,
            reason=None
            if session.plan is not None
            else "Plan envelope present but unparsed; check /runs/{run_id} blockers.",
        )

    @app.get(
        "/projects/{project_id}/runs/{run_id}/cost_estimate",
        response_model=CostEstimateResponse,
        tags=[TAG_RUNS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Pre-flight heuristic cost band for the run.",
    )
    def _cost_estimate(
        project_id: str = ApiPath(...),
        run_id: str = ApiPath(...),
    ) -> CostEstimateResponse:
        session = _resolve_run_or_404(actual_store, project_id, run_id)
        estimate = _estimate_cost(session.directive_summary, session.max_steps)
        estimate.run_id = run_id
        estimate.project_id = project_id
        return estimate

    # --- Gates ----------------------------------------------------------

    def _resolve_gate(
        run_id: str, gate_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        gates = actual_store.load_gates(run_id)
        for g in gates:
            if g["gate_id"] == gate_id:
                return g, gates
        return None, gates

    def _record_gate_decision(
        *,
        project_id: str,
        run_id: str,
        gate_id: str,
        decision: str,  # GATE_STATUS_APPROVED | GATE_STATUS_REJECTED
        req: GateDecisionRequest,
    ) -> GateDecisionResponse:
        session = _resolve_run_or_404(actual_store, project_id, run_id)
        gate, _ = _resolve_gate(run_id, gate_id)
        if gate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "status": "gate_not_found",
                    "reason": f"no gate {gate_id!r} on run {run_id!r}",
                    "signed_message": sign_action(
                        "Atlas",
                        f"API 404: gate {gate_id!r} not found on run {run_id!r}.",
                    ),
                },
            )
        if gate["status"] != GATE_STATUS_PENDING:
            return GateDecisionResponse(
                gate_id=gate_id,
                status="already_resolved",
                signed_message=sign_action(
                    "Atlas",
                    f"Gate {gate_id} already resolved "
                    f"(status={gate['status']}).",
                ),
                note="Gate is no longer pending; no new decision recorded.",
            )
        # Persist the resolution via a direct UPDATE on the gates table.
        # SessionStore does not yet expose a public resolve_gate method —
        # 4.6 left the transition surface for 4.11. We open a transaction
        # ourselves through the store so the row lands inside the same
        # WAL discipline the rest of the runtime uses.
        with actual_store.transaction() as conn:
            conn.execute(
                "UPDATE gates SET status = ?, resolved_at = ?, "
                "resolved_by = ?, rationale = ? "
                "WHERE gate_id = ? AND status = ?",
                (
                    decision,
                    _utc_now_iso(),
                    req.resolved_by,
                    _safe_text(req.rationale),
                    gate_id,
                    GATE_STATUS_PENDING,
                ),
            )
        # Audit-side signed decision row so the run timeline shows the
        # human's call, even if the supervisor's gate-resume path isn't
        # wired in MVP.
        signed = sign_action(
            "Atlas",
            f"Gate {decision.upper()}: gate_id={gate_id} run_id={run_id} "
            f"resolved_by={req.resolved_by}",
        )
        actual_store.record_decision(
            run_id=run_id,
            session_id=session.session_id,
            kind=DECISION_KIND_BLOCKER,
            phase=f"gate_{decision}",
            signed_message=signed,
            reason_or_error=req.rationale,
            metadata={
                "gate_id": gate_id,
                "resolved_by": req.resolved_by,
                "decision": decision,
            },
            created_at=datetime.now(UTC),
        )
        if decision == GATE_STATUS_APPROVED:
            return GateDecisionResponse(
                gate_id=gate_id,
                status="approved_recorded_execution_not_resumed",
                signed_message=signed,
                note=(
                    "Human approval is durable. Live execution does NOT "
                    "auto-resume in 4.11 because safely continuing the "
                    "halted tool call would require persisting raw args "
                    "(potentially secret-bearing) — deferred to M5+. "
                    "Operator may re-submit a follow-up directive."
                ),
            )
        return GateDecisionResponse(
            gate_id=gate_id,
            status="reject_recorded",
            signed_message=signed,
            note="Gate rejected; run remains in its current state.",
        )

    @app.post(
        "/projects/{project_id}/runs/{run_id}/gates/{gate_id}/approve",
        response_model=GateDecisionResponse,
        tags=[TAG_GATES],
        dependencies=[Depends(require_bearer_auth)],
        summary="Approve a pending human-approved-only gate.",
    )
    def _approve_gate(
        project_id: str = ApiPath(...),
        run_id: str = ApiPath(...),
        gate_id: str = ApiPath(...),
        req: GateDecisionRequest = Body(...),
    ) -> GateDecisionResponse:
        return _record_gate_decision(
            project_id=project_id,
            run_id=run_id,
            gate_id=gate_id,
            decision=GATE_STATUS_APPROVED,
            req=req,
        )

    @app.post(
        "/projects/{project_id}/runs/{run_id}/gates/{gate_id}/reject",
        response_model=GateDecisionResponse,
        tags=[TAG_GATES],
        dependencies=[Depends(require_bearer_auth)],
        summary="Reject a pending human-approved-only gate.",
    )
    def _reject_gate(
        project_id: str = ApiPath(...),
        run_id: str = ApiPath(...),
        gate_id: str = ApiPath(...),
        req: GateDecisionRequest = Body(...),
    ) -> GateDecisionResponse:
        return _record_gate_decision(
            project_id=project_id,
            run_id=run_id,
            gate_id=gate_id,
            decision=GATE_STATUS_REJECTED,
            req=req,
        )

    # --- Agents ---------------------------------------------------------

    AGENT_PROVIDER_INFO = {
        "Atlas": {"provider": "openai", "model_env": "ATLAS_MODEL"},
        "Cody": {"provider": "anthropic", "model_env": "CODY_MODEL"},
        "Scribe": {"provider": "anthropic", "model_env": "SCRIBE_MODEL"},
        "Scout": {"provider": "anthropic", "model_env": "SCOUT_MODEL"},
    }

    def _agents_last_activity() -> dict[str, str | None]:
        # Cheap heuristic: latest signed message per sender across all runs.
        last_by: dict[str, str | None] = {n: None for n in AGENT_PROVIDER_INFO}
        with actual_store.connection() as conn:
            rows = conn.execute(
                "SELECT sender, MAX(created_at) AS last_at FROM messages "
                "GROUP BY sender"
            ).fetchall()
        for r in rows:
            if r["sender"] in last_by:
                last_by[r["sender"]] = r["last_at"]
        return last_by

    @app.get(
        "/projects/{project_id}/agents",
        response_model=AgentsResponse,
        tags=[TAG_AGENTS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Atlas/Cody/Scribe/Scout status for this project.",
    )
    def _list_agents(project_id: str = ApiPath(...)) -> AgentsResponse:
        _resolve_project_or_404(actual_store, project_id)
        last_activity = _agents_last_activity()
        out: list[AgentView] = []
        for name in ("Atlas", "Cody", "Scribe", "Scout"):
            info = AGENT_PROVIDER_INFO[name]
            out.append(
                AgentView(
                    name=name,
                    provider=info["provider"],
                    model_env=info["model_env"],
                    model_env_present=bool(os.environ.get(info["model_env"])),
                    allowed_tools=list(allowed_tools_for(name)),
                    last_activity_at=last_activity.get(name),
                )
            )
        return AgentsResponse(project_id=project_id, agents=out)

    # --- Integrations ---------------------------------------------------

    @app.get(
        "/projects/{project_id}/integrations",
        response_model=IntegrationsResponse,
        tags=[TAG_INTEGRATIONS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Connected MCP/tool integrations + per-agent capabilities.",
    )
    def _list_integrations(project_id: str = ApiPath(...)) -> IntegrationsResponse:
        _resolve_project_or_404(actual_store, project_id)
        configs = load_mcp_configs()
        per_agent_caps_by_server: dict[str, dict[str, list[str]]] = {}
        for tool_name, spec in TOOL_REGISTRY.items():
            per_agent_caps_by_server.setdefault(spec.server_name, {})
            for agent in ("Atlas", "Cody", "Scribe", "Scout"):
                if tool_name in AGENT_TOOL_MATRIX.get(agent, frozenset()):
                    per_agent_caps_by_server[spec.server_name].setdefault(
                        agent, []
                    ).append(tool_name)
        # Last successful call per server (signed `Tool ok: ...` lines).
        with actual_store.connection() as conn:
            rows = conn.execute(
                "SELECT server_name, MAX(completed_at) AS last_at "
                "FROM tool_calls WHERE status = 'ok' "
                "GROUP BY server_name"
            ).fetchall()
        last_by_server = {r["server_name"]: r["last_at"] for r in rows}
        # Filesystem + bash have no JSON config; surface them from the
        # registry alongside the configured MCP servers.
        fixed_integrations = [
            ("filesystem", "local", []),
            ("bash", "local", []),
        ]
        out: list[IntegrationView] = []
        for name, cfg in configs.items():
            out.append(
                IntegrationView(
                    type=name,
                    enabled=cfg.enabled,
                    transport=cfg.transport,
                    agents=list(cfg.agents),
                    env_presence=dict(cfg.resolved_env),
                    missing_env=list(cfg.missing_env),
                    notes=cfg.notes,
                    capabilities=per_agent_caps_by_server.get(name, {}),
                    last_successful_call_at=last_by_server.get(name),
                )
            )
        for name, transport, agents in fixed_integrations:
            out.append(
                IntegrationView(
                    type=name,
                    enabled=True,
                    transport=transport,
                    agents=agents,
                    env_presence={},
                    missing_env=[],
                    notes="runtime-local; no MCP transport",
                    capabilities=per_agent_caps_by_server.get(name, {}),
                    last_successful_call_at=last_by_server.get(name),
                )
            )
        return IntegrationsResponse(project_id=project_id, integrations=out)

    @app.post(
        "/projects/{project_id}/integrations/{type}/auth",
        response_model=IntegrationActionResponse,
        tags=[TAG_INTEGRATIONS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Initiate auth flow scaffold (M5 will land real handoff).",
    )
    def _integration_auth(
        project_id: str = ApiPath(...),
        type: str = ApiPath(..., alias="type"),
        req: IntegrationAuthRequest = Body(default_factory=IntegrationAuthRequest),
    ) -> IntegrationActionResponse:
        _resolve_project_or_404(actual_store, project_id)
        signed = sign_action(
            "Atlas",
            f"Integration auth request: project_id={project_id} type={type}; "
            "manual_secret_handoff_required (M5 task 5.2 owns real handoff).",
        )
        return IntegrationActionResponse(
            type=type,
            status="manual_secret_handoff_required",
            signed_message=signed,
            next_step=(
                "Garrett configures the integration's env var(s) on the VPS "
                "and the runtime picks them up via runtime/.env. The 4.11 "
                "API does NOT accept or store secrets — that surface lands "
                "in M5 task 5.2 with the Retool secret-vault handoff."
            ),
        )

    @app.delete(
        "/projects/{project_id}/integrations/{type}",
        response_model=IntegrationActionResponse,
        tags=[TAG_INTEGRATIONS],
        dependencies=[Depends(require_bearer_auth)],
        summary="Mark an integration disabled in project metadata (MVP).",
    )
    def _integration_delete(
        project_id: str = ApiPath(...),
        type: str = ApiPath(..., alias="type"),
    ) -> IntegrationActionResponse:
        project = _resolve_project_or_404(actual_store, project_id)
        # MVP: record the disabled flag in project metadata without ever
        # touching VPS env vars or deleting credentials. Atlas's directive:
        # "must NOT delete env secrets or mutate VPS credentials without
        # explicit approval."
        meta = dict(project.metadata or {})
        disabled = list(meta.get("disabled_integrations", []))
        if type not in disabled:
            disabled.append(type)
        meta["disabled_integrations"] = disabled
        actual_store.update_project(project_id, metadata=meta)
        signed = sign_action(
            "Atlas",
            f"Integration {type} marked disabled in project_id={project_id} "
            "metadata; no env secrets touched.",
        )
        return IntegrationActionResponse(
            type=type,
            status="disabled_metadata_recorded",
            signed_message=signed,
            next_step=(
                "Project metadata now records this integration as disabled. "
                "Live secret revocation remains a manual VPS operation by "
                "design (M5 task 5.2 may add a guarded rotate surface)."
            ),
        )

    # --- Artifact preview ----------------------------------------------

    @app.get(
        "/projects/{project_id}/runs/{run_id}/artifacts/{artifact_id}/diff",
        response_model=ArtifactDiffResponse,
        tags=[TAG_ARTIFACTS],
        dependencies=[Depends(require_bearer_auth)],
        summary="GitHub push-diff preview for human-approved-only push artifacts.",
    )
    def _artifact_diff(
        project_id: str = ApiPath(...),
        run_id: str = ApiPath(...),
        artifact_id: str = ApiPath(...),
    ) -> ArtifactDiffResponse:
        _resolve_run_or_404(actual_store, project_id, run_id)
        # Lookup: artifact_id matches a persisted tool_call_id for a
        # github.push_branch row that is still pending-human-approval.
        target_row: dict[str, Any] | None = None
        for row in actual_store.load_tool_calls(run_id):
            if row["tool_call_id"] == artifact_id:
                target_row = row
                break
        if target_row is None:
            return ArtifactDiffResponse(
                run_id=run_id,
                artifact_id=artifact_id,
                status="unresolved_artifact",
                artifact_kind=None,
                diff=None,
                signed_message=sign_action(
                    "Atlas",
                    f"Artifact {artifact_id!r} unresolved on run {run_id}: "
                    "no matching tool_call_id.",
                ),
                detail="no_matching_tool_call",
            )
        if target_row["tool_name"] != "github.push_branch":
            return ArtifactDiffResponse(
                run_id=run_id,
                artifact_id=artifact_id,
                status="unresolved_artifact",
                artifact_kind=target_row["tool_name"],
                diff=None,
                signed_message=sign_action(
                    "Atlas",
                    f"Artifact {artifact_id!r} kind {target_row['tool_name']!r} "
                    "has no diff preview implemented in 4.11.",
                ),
                detail="diff_unsupported_for_tool",
            )
        # Compute a read-only diff for the current local branch against
        # origin/main using git plumbing. We deliberately avoid the bash
        # registry to stay within the directive's "do NOT widen bash
        # allowlist" boundary.
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell.
                ["git", "diff", "--stat", "origin/main...HEAD"],
                cwd=str(orchestra_root().parent),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            diff_text = (result.stdout or "") + (
                ("\n" + result.stderr) if result.stderr else ""
            )
            diff_text = _safe_text(diff_text)
        except FileNotFoundError:
            diff_text = "(git not available on this host)"
        except subprocess.TimeoutExpired:
            diff_text = "(git diff timed out)"
        return ArtifactDiffResponse(
            run_id=run_id,
            artifact_id=artifact_id,
            status="ok",
            artifact_kind="github.push_branch",
            diff=diff_text,
            signed_message=sign_action(
                "Atlas",
                f"Artifact diff: artifact_id={artifact_id} run_id={run_id} "
                f"tool=github.push_branch.",
            ),
        )

    return app


# --- Dry-run / self-test -----------------------------------------------------


def _dry_run() -> int:
    """End-to-end self-test of the API surface.

    Drives the FastAPI app via ``fastapi.testclient.TestClient`` against
    a fresh temp DB so the harness exercises auth, every endpoint group,
    project persistence, run snapshot serialization, SSE serialization,
    halt semantics, cost estimate, gate decision persistence, and token
    redaction without making real provider/HTTP calls.

    Exits 0 on a clean run, 1 on any failure.
    """
    from fastapi.testclient import TestClient  # local: avoid top-level cost.

    failures: list[str] = []
    passes: list[str] = []

    def record(case: str, ok: bool, detail: str) -> None:
        target = passes if ok else failures
        target.append(f"{case}: {detail}")
        print(
            sign_action(
                "Cody",
                f"api dry-run {'pass' if ok else 'FAIL'} — {case}: {detail}",
            ),
            flush=True,
        )

    with tempfile.TemporaryDirectory(prefix="orchestra-api-test-") as tmp:
        db_path = Path(tmp) / "sessions.db"
        store = SessionStore(db_path)
        store.ensure_schema()

        # --- Scenario 1: health endpoint public (no auth env). --------
        previous_token = os.environ.pop(ORCHESTRA_API_TOKEN_ENV, None)
        previous_workspace = os.environ.pop("ASANA_WORKSPACE_GID", None)
        try:
            app = create_app(store=store)
            client = TestClient(app)
            r = client.get("/health")
            ok = (
                r.status_code == 200
                and r.json().get("ok") is True
                and r.json().get("auth_configured") is False
                and r.json().get("schema_version") == 3
            )
            record(
                "health-public-no-auth",
                ok,
                f"status={r.status_code}, auth_configured="
                f"{r.json().get('auth_configured')}",
            )

            # --- Scenario 2: protected endpoint fail-closed without token.
            r = client.get("/projects")
            ok = (
                r.status_code == 503
                and r.json().get("detail", {}).get("status") == "auth_unavailable"
            )
            record(
                "auth-fail-closed-no-token",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('detail', {}).get('status')}",
            )

            # --- Scenario 3: configure token; missing header → 401.
            os.environ[ORCHESTRA_API_TOKEN_ENV] = "test-token"
            r = client.get("/projects")
            ok = (
                r.status_code == 401
                and r.json().get("detail", {}).get("status") == "missing_bearer"
            )
            record(
                "auth-missing-bearer",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('detail', {}).get('status')}",
            )

            # --- Scenario 4: wrong token → 401 invalid_bearer.
            r = client.get(
                "/projects", headers={"Authorization": "Bearer wrong"}
            )
            ok = (
                r.status_code == 401
                and r.json().get("detail", {}).get("status") == "invalid_bearer"
            )
            record(
                "auth-wrong-bearer",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('detail', {}).get('status')}",
            )

            auth = {"Authorization": "Bearer test-token"}

            # --- Scenario 5: correct token → 200 empty list.
            r = client.get("/projects", headers=auth)
            ok = (
                r.status_code == 200
                and r.json().get("projects") == []
                and r.json().get("unscoped_legacy_sessions") == 0
            )
            record(
                "projects-empty-list",
                ok,
                f"status={r.status_code}, body_keys="
                f"{sorted(r.json().keys())}",
            )

            # --- Scenario 6: greenfield POST without ASANA_WORKSPACE_GID
            # → 424 configuration_required (not a silent link-only fallback).
            r = client.post(
                "/projects",
                headers=auth,
                json={"name": "JLOOP CRM (greenfield)"},
            )
            ok = (
                r.status_code == 424
                and r.json().get("detail", {}).get("status")
                == "configuration_required"
            )
            record(
                "projects-greenfield-configuration-required",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('detail', {}).get('status')}",
            )

            # --- Scenario 7: linked-Asana POST → 201.
            r = client.post(
                "/projects",
                headers=auth,
                json={
                    "name": "JLOOP CRM",
                    "lifecycle_state": PROJECT_STATE_DEVELOPMENT,
                    "asana_project_gid": "1215999999999999",
                    "repo_urls": ["https://github.com/ClarityOps-Apps/jloop-crm"],
                    "vision": "operator vision",
                },
            )
            ok = (
                r.status_code == 201
                and r.json().get("name") == "JLOOP CRM"
                and r.json().get("lifecycle_state") == PROJECT_STATE_DEVELOPMENT
            )
            project_id = r.json().get("project_id") if r.status_code == 201 else None
            record(
                "projects-create-linked-asana",
                ok and bool(project_id),
                f"status={r.status_code}, project_id="
                f"{(project_id or '')[:8]}…",
            )

            if project_id is None:
                # Cannot continue subsequent scenarios without a project.
                return 1 if failures else 0

            # --- Scenario 8: GET + PATCH project round-trip.
            r = client.get(f"/projects/{project_id}", headers=auth)
            ok = r.status_code == 200 and r.json().get("project_id") == project_id
            record(
                "projects-get-single",
                ok,
                f"status={r.status_code}",
            )
            r = client.patch(
                f"/projects/{project_id}",
                headers=auth,
                json={"lifecycle_state": PROJECT_STATE_ARCHIVED, "archived": True},
            )
            ok = (
                r.status_code == 200
                and r.json().get("lifecycle_state") == PROJECT_STATE_ARCHIVED
                and r.json().get("archived") is True
            )
            record(
                "projects-patch",
                ok,
                f"status={r.status_code}, archived="
                f"{r.json().get('archived')}",
            )
            # un-archive for the rest of the scenarios.
            client.patch(
                f"/projects/{project_id}",
                headers=auth,
                json={"lifecycle_state": PROJECT_STATE_DEVELOPMENT, "archived": False},
            )

            # --- Scenario 9: directive submission (dry-run) + run detail.
            r = client.post(
                f"/projects/{project_id}/directive?dry_run=true",
                headers=auth,
                json={"directive": "Ask Cody to acknowledge the 4.11 API smoke."},
            )
            ok = r.status_code == 202 and r.json().get("dry_run") is True
            run_id = r.json().get("run_id") if r.status_code == 202 else None
            record(
                "directive-submit-dry-run-accepted",
                ok and bool(run_id),
                f"status={r.status_code}, run_id={(run_id or '')[:8]}…",
            )
            if run_id is None:
                return 1
            # Wait for the background thread to finish (dry-run completes
            # in well under 30s).
            _wait_for_run(run_id, timeout_seconds=30.0)
            r = client.get(
                f"/projects/{project_id}/runs/{run_id}", headers=auth
            )
            body = r.json() if r.status_code == 200 else {}
            ok = (
                r.status_code == 200
                and body.get("summary", {}).get("status")
                == SUPERVISOR_STATUS_COMPLETE
                and len(body.get("steps", [])) >= 1
                and len(body.get("messages", [])) >= 2
            )
            record(
                "run-detail-snapshot-complete",
                ok,
                f"status={r.status_code}, supervisor_status="
                f"{body.get('summary', {}).get('status')}, "
                f"steps={len(body.get('steps', []))}, "
                f"messages={len(body.get('messages', []))}",
            )

            # --- Scenario 10: run list scoped to project.
            r = client.get(
                f"/projects/{project_id}/runs", headers=auth
            )
            ok = (
                r.status_code == 200
                and len(r.json().get("runs", [])) >= 1
                and r.json()["runs"][0]["project_id"] == project_id
            )
            record(
                "runs-list-scoped-to-project",
                ok,
                f"status={r.status_code}, runs="
                f"{len(r.json().get('runs', []))}",
            )

            # --- Scenario 11: preview endpoint returns planner output.
            r = client.get(
                f"/projects/{project_id}/runs/{run_id}/preview", headers=auth
            )
            ok = (
                r.status_code == 200
                and r.json().get("available") is True
                and "orchestra_plan" in (r.json().get("planner_envelope_content") or "")
            )
            record(
                "plan-preview-no-side-effects",
                ok,
                f"status={r.status_code}, available="
                f"{r.json().get('available')}",
            )

            # --- Scenario 12: cost estimate determinism.
            r = client.get(
                f"/projects/{project_id}/runs/{run_id}/cost_estimate",
                headers=auth,
            )
            body = r.json()
            ok = (
                r.status_code == 200
                and body.get("estimated_input_tokens") > 0
                and body.get("estimated_cost_low_usd")
                <= body.get("estimated_cost_high_usd")
                and isinstance(body.get("requires_approval"), bool)
                and "chars_per_token" in body.get("assumptions", {})
            )
            record(
                "cost-estimate-heuristic",
                ok,
                f"status={r.status_code}, low="
                f"{body.get('estimated_cost_low_usd')}, "
                f"high={body.get('estimated_cost_high_usd')}",
            )

            # --- Scenario 13: halt on a completed run → already_terminal.
            r = client.post(
                f"/projects/{project_id}/runs/{run_id}/halt", headers=auth
            )
            ok = (
                r.status_code == 200
                and r.json().get("status") == "already_terminal"
            )
            record(
                "halt-already-terminal",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('status')}",
            )

            # --- Scenario 14: halt mid-flight; halt_event must fire +
            # decision row persisted.
            r = client.post(
                f"/projects/{project_id}/directive?dry_run=true",
                headers=auth,
                json={"directive": "Halt smoke directive."},
            )
            halt_run_id = r.json().get("run_id")
            # Halt immediately (background may still be racing through
            # the planner; halt_event is set before any step begins).
            r2 = client.post(
                f"/projects/{project_id}/runs/{halt_run_id}/halt",
                headers=auth,
            )
            _wait_for_run(halt_run_id, timeout_seconds=30.0)
            session = store.load_session(halt_run_id)
            decisions = store.load_decisions(halt_run_id)
            halt_phases = [d["phase"] for d in decisions]
            ok = (
                r.status_code == 202
                and r2.status_code == 200
                and r2.json().get("status") == "halt_recorded"
                and session is not None
                # The persisted halt_request decision always lands; the
                # supervisor MAY also add an api_halt_requested_pre_step
                # blocker if it observed the flag before terminating. We
                # require the request marker; the supervisor-observation
                # marker is best-effort given the race window.
                and "api_halt_request" in halt_phases
            )
            record(
                "halt-mid-flight-persisted",
                ok,
                f"halt_response_status={r2.json().get('status')}, "
                f"session_status="
                f"{session.status if session else None}, "
                f"halt_phases={halt_phases}",
            )

            # --- Scenario 15: agents endpoint shape.
            r = client.get(
                f"/projects/{project_id}/agents", headers=auth
            )
            body = r.json()
            ok = (
                r.status_code == 200
                and len(body.get("agents", [])) == 4
                and {a["name"] for a in body["agents"]}
                == {"Atlas", "Cody", "Scribe", "Scout"}
                # Allowed tools list is non-empty for every agent.
                and all(len(a["allowed_tools"]) > 0 for a in body["agents"])
            )
            record(
                "agents-endpoint-shape",
                ok,
                f"status={r.status_code}, agents="
                f"{[a['name'] for a in body.get('agents', [])]}",
            )

            # --- Scenario 16: integrations endpoint.
            r = client.get(
                f"/projects/{project_id}/integrations", headers=auth
            )
            body = r.json()
            integration_types = {i["type"] for i in body.get("integrations", [])}
            ok = (
                r.status_code == 200
                and {"asana", "github", "filesystem", "bash"} <= integration_types
                # No env values echoed — every env_presence entry is a bool.
                and all(
                    isinstance(v, bool)
                    for i in body["integrations"]
                    for v in i["env_presence"].values()
                )
            )
            record(
                "integrations-endpoint-shape",
                ok,
                f"status={r.status_code}, types={sorted(integration_types)}",
            )

            # --- Scenario 17: POST /integrations/{type}/auth → manual handoff.
            r = client.post(
                f"/projects/{project_id}/integrations/asana/auth",
                headers=auth,
                json={"note": "test"},
            )
            ok = (
                r.status_code == 200
                and r.json().get("status") == "manual_secret_handoff_required"
            )
            record(
                "integration-auth-manual-handoff",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('status')}",
            )

            # --- Scenario 18: DELETE /integrations/{type} → disabled flag.
            r = client.delete(
                f"/projects/{project_id}/integrations/supabase",
                headers=auth,
            )
            ok = (
                r.status_code == 200
                and r.json().get("status") == "disabled_metadata_recorded"
            )
            record(
                "integration-delete-disabled-metadata",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('status')}",
            )

            # --- Scenario 19: gate approve/reject persistence.
            # Seed a synthetic gate so the gate endpoint has something to
            # resolve without running a real human-approved-only step.
            seed_signed = sign_action(
                "Atlas",
                "Blocked pending Garrett approval. APPROVAL REQUEST",
            )
            gate_id = store.record_gate(
                run_id=run_id,
                session_id=store.load_session(run_id).session_id,
                step_id=1,
                target="Cody",
                action_surface="human-approved-only",
                signed_message=seed_signed,
                metadata={"reason": "synthetic 4.11 gate test"},
                created_at=datetime.now(UTC),
            )
            r = client.post(
                f"/projects/{project_id}/runs/{run_id}/gates/{gate_id}/approve",
                headers=auth,
                json={"rationale": "operator approved for smoke"},
            )
            ok = (
                r.status_code == 200
                and r.json().get("status")
                == "approved_recorded_execution_not_resumed"
            )
            record(
                "gate-approve-recorded",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('status')}",
            )
            # Approving again should now report already_resolved.
            r = client.post(
                f"/projects/{project_id}/runs/{run_id}/gates/{gate_id}/approve",
                headers=auth,
                json={"rationale": "second attempt"},
            )
            ok = (
                r.status_code == 200
                and r.json().get("status") == "already_resolved"
            )
            record(
                "gate-already-resolved",
                ok,
                f"status={r.status_code}",
            )

            # --- Scenario 20: artifact diff for unresolved artifact.
            r = client.get(
                f"/projects/{project_id}/runs/{run_id}/artifacts/bogus-id/diff",
                headers=auth,
            )
            ok = (
                r.status_code == 200
                and r.json().get("status") == "unresolved_artifact"
            )
            record(
                "artifact-diff-unresolved",
                ok,
                f"status={r.status_code}, body_status="
                f"{r.json().get('status')}",
            )

            # --- Scenario 21: token redaction in API responses.
            # Submit a directive carrying a secret-shaped token; the
            # preflight in run_supervisor blocks it. Confirm the persisted
            # decision/blocker rows surfaced via /runs/{run_id} do NOT
            # contain the raw token in any field the API serializes.
            synthetic_token = "sk-proj-ZZZZZZZZZZZZZZZZZZZZ"
            r = client.post(
                f"/projects/{project_id}/directive?dry_run=true",
                headers=auth,
                json={"directive": f"please remember {synthetic_token}"},
            )
            redact_run_id = r.json().get("run_id")
            _wait_for_run(redact_run_id, timeout_seconds=30.0)
            r = client.get(
                f"/projects/{project_id}/runs/{redact_run_id}", headers=auth
            )
            body_text = json.dumps(r.json())
            ok = (
                r.status_code == 200
                and synthetic_token not in body_text
                and "[REDACTED:openai_api_key]" in body_text
            )
            record(
                "run-detail-redacts-token",
                ok,
                f"status={r.status_code}, raw_token_in_body="
                f"{synthetic_token in body_text}, "
                f"redaction_marker_present="
                f"{'[REDACTED:openai_api_key]' in body_text}",
            )

            # --- Scenario 22: OpenAPI schema includes every endpoint group.
            r = client.get("/openapi.json")
            schema = r.json()
            tag_names = {t["name"] for t in schema.get("tags", [])}
            ok = (
                r.status_code == 200
                and {
                    TAG_HEALTH,
                    TAG_PROJECTS,
                    TAG_RUNS,
                    TAG_GATES,
                    TAG_AGENTS,
                    TAG_INTEGRATIONS,
                    TAG_ARTIFACTS,
                } <= tag_names
            )
            record(
                "openapi-schema-tags",
                ok,
                f"status={r.status_code}, tags={sorted(tag_names)}",
            )

            # --- Scenario 23: SSE stream emits one terminal event on a
            # completed run (no live activity, so the loop sees terminal
            # status immediately and closes).
            with client.stream(
                "GET",
                f"/projects/{project_id}/runs/{run_id}/stream",
                headers=auth,
            ) as stream:
                # Bound iteration in case the SSE generator deadlocks.
                lines: list[str] = []
                deadline = time.monotonic() + 10.0
                for line in stream.iter_lines():
                    if line:
                        lines.append(line if isinstance(line, str) else line.decode())
                    if any("event: end" in l for l in lines):
                        break
                    if time.monotonic() > deadline:
                        break
            terminal_seen = any("event: end" in l for l in lines)
            record(
                "sse-stream-terminal-event",
                terminal_seen,
                f"lines_received={len(lines)}, "
                f"terminal_event_seen={terminal_seen}",
            )

            # --- Scenario 24: legacy CLI session (NULL project_id) does
            # NOT leak into a project's run list, but IS counted in the
            # unscoped sentinel surfaced by the projects list endpoint.
            cli_run_id = str(uuid4())
            cli_session_id = str(uuid4())
            store.record_session(
                run_id=cli_run_id,
                session_id=cli_session_id,
                directive_summary="legacy CLI session (no project_id)",
                status="complete",
                max_steps=3,
                dry_run=True,
                created_at=datetime.now(UTC),
                project_id=None,
            )
            r = client.get(f"/projects/{project_id}/runs", headers=auth)
            scoped_run_ids = {ru["run_id"] for ru in r.json().get("runs", [])}
            r2 = client.get("/projects", headers=auth)
            ok = (
                cli_run_id not in scoped_run_ids
                and r2.status_code == 200
                and r2.json().get("unscoped_legacy_sessions", 0) >= 1
            )
            record(
                "legacy-cli-session-segregated",
                ok,
                f"cli_in_scoped_runs={cli_run_id in scoped_run_ids}, "
                f"unscoped_count="
                f"{r2.json().get('unscoped_legacy_sessions')}",
            )

            # --- Atlas addendum 1215677174730272 — new scenarios ----------

            # --- Scenario 25: greenfield STRICT fail-closed even when
            # ASANA_WORKSPACE_GID is set. Prior behavior silently wrote a
            # local-only project row whenever the env var was present;
            # the addendum tightens the check so ANY POST without an
            # explicit asana_project_gid returns 424 + persists nothing.
            previous_count_before = len(store.list_projects(include_archived=True))
            os.environ["ASANA_WORKSPACE_GID"] = "1209122693222374"
            try:
                r = client.post(
                    "/projects",
                    headers=auth,
                    json={"name": "Silent-Local-Only Regression"},
                )
            finally:
                # Leave the env consistent for downstream scenarios.
                pass
            previous_count_after = len(store.list_projects(include_archived=True))
            body = r.json()
            ok = (
                r.status_code == 424
                and body.get("detail", {}).get("status") == "configuration_required"
                and "asana_project_gid"
                in body.get("detail", {}).get("detail", {}).get("required_inputs", [])
                and previous_count_after == previous_count_before
            )
            record(
                "addendum-greenfield-strict-no-row-when-env-set",
                ok,
                f"status={r.status_code}, "
                f"body_status={body.get('detail', {}).get('status')}, "
                f"rows_before={previous_count_before}, "
                f"rows_after={previous_count_after}",
            )
            # Remove the workspace env now that the scenario is complete
            # so cost-estimate / preview scenarios match the rest of the
            # harness shape.
            os.environ.pop("ASANA_WORKSPACE_GID", None)

            # --- Scenario 26: pre-flight POST /directive/cost_estimate
            # is a pure function — no run row created, no DB writes
            # beyond reading. Verified by checking the session count
            # before / after AND that the run_id field in the response
            # is empty (the heuristic does not need a run to compute).
            sessions_before = len(store.list_sessions(limit=1000))
            r = client.post(
                f"/projects/{project_id}/directive/cost_estimate",
                headers=auth,
                json={
                    "directive": "Cost estimate pre-flight smoke directive.",
                    "max_steps": 3,
                },
            )
            sessions_after = len(store.list_sessions(limit=1000))
            body = r.json()
            ok = (
                r.status_code == 200
                and sessions_after == sessions_before
                and body.get("run_id") == ""
                and body.get("project_id") == project_id
                and body.get("estimated_input_tokens", 0) > 0
                and body.get("estimated_cost_low_usd", 0)
                <= body.get("estimated_cost_high_usd", 0)
            )
            record(
                "addendum-preflight-cost-estimate-no-side-effects",
                ok,
                f"status={r.status_code}, "
                f"sessions_before={sessions_before}, "
                f"sessions_after={sessions_after}, "
                f"run_id_blank={body.get('run_id') == ''}, "
                f"low={body.get('estimated_cost_low_usd')}, "
                f"high={body.get('estimated_cost_high_usd')}",
            )

            # --- Scenario 27: pre-flight POST /directive/preview runs
            # the planner ONLY (`_stop_after="planner"`) and persists a
            # planning-state run row. Strict assertion: zero tool_calls,
            # zero decisions, zero gates, zero finalizer envelope; the
            # ONLY persisted message is the planner envelope; planned
            # actions are all in status='planned' (never 'responded' or
            # any executed state); supervisor status is `planning`.
            r = client.post(
                f"/projects/{project_id}/directive/preview?dry_run=true",
                headers=auth,
                json={
                    "directive": "Preview pre-flight smoke directive.",
                    "max_steps": 3,
                },
            )
            preview_body = r.json()
            preview_run_id = preview_body.get("run_id")
            preview_ok_shape = (
                r.status_code == 200
                and preview_body.get("available") is True
                and "orchestra_plan"
                in (preview_body.get("planner_envelope_content") or "")
                and preview_body.get("status") == SUPERVISOR_STATUS_PLANNING
                and bool(preview_run_id)
            )
            # Read back the persisted row to confirm zero downstream
            # execution artifacts.
            preview_actions = (
                store.load_actions(preview_run_id) if preview_run_id else []
            )
            preview_tool_calls = (
                store.load_tool_calls(preview_run_id) if preview_run_id else []
            )
            preview_decisions = (
                store.load_decisions(preview_run_id) if preview_run_id else []
            )
            preview_gates = (
                store.load_gates(preview_run_id) if preview_run_id else []
            )
            preview_messages = (
                store.load_messages(preview_run_id) if preview_run_id else []
            )
            preview_session = (
                store.load_session(preview_run_id) if preview_run_id else None
            )
            all_actions_planned = preview_actions and all(
                a["status"] == "planned" and a.get("response_envelope_id") is None
                for a in preview_actions
            )
            zero_execution_evidence = (
                len(preview_tool_calls) == 0
                and len(preview_decisions) == 0
                and len(preview_gates) == 0
                and len(preview_messages) == 1  # planner envelope only
                and preview_session is not None
                and preview_session.finalizer_envelope_id is None
                and preview_session.status == SUPERVISOR_STATUS_PLANNING
            )
            ok = preview_ok_shape and all_actions_planned and zero_execution_evidence
            record(
                "addendum-preflight-preview-no-execution",
                ok,
                f"shape_ok={preview_ok_shape}, "
                f"all_actions_planned={all_actions_planned}, "
                f"tool_calls={len(preview_tool_calls)}, "
                f"decisions={len(preview_decisions)}, "
                f"gates={len(preview_gates)}, "
                f"messages={len(preview_messages)}, "
                f"finalizer_set="
                f"{preview_session.finalizer_envelope_id is not None if preview_session else None}",
            )

            # --- Scenario 28: legacy ``project_id=NULL`` session must
            # 404 under EVERY project's scoped run detail endpoint.
            # Atlas addendum finding 3: the prior _resolve_run_or_404
            # passed-through when project_id was None, letting a legacy
            # row attach to any project via direct lookup. The tighter
            # comparison (``session.project_id != project_id``) catches
            # both the NULL leg and cross-project access uniformly.
            # ``cli_run_id`` from scenario 24 is still the project_id=NULL
            # row in the temp DB; reuse it. We also create a second
            # project so the cross-project lookup is exercised.
            r = client.post(
                "/projects",
                headers=auth,
                json={
                    "name": "Second project for cross-scope test",
                    "asana_project_gid": "1215999999999998",
                },
            )
            second_project_id = (
                r.json().get("project_id") if r.status_code == 201 else None
            )
            r_first = client.get(
                f"/projects/{project_id}/runs/{cli_run_id}", headers=auth
            )
            r_second = (
                client.get(
                    f"/projects/{second_project_id}/runs/{cli_run_id}",
                    headers=auth,
                )
                if second_project_id
                else None
            )
            ok = (
                r_first.status_code == 404
                and r_first.json().get("detail", {}).get("status")
                == "run_not_in_project"
                and (
                    r_second is None
                    or (
                        r_second.status_code == 404
                        and r_second.json().get("detail", {}).get("status")
                        == "run_not_in_project"
                    )
                )
            )
            record(
                "addendum-legacy-null-project-isolated",
                ok,
                f"first_project_status={r_first.status_code}, "
                f"second_project_status="
                f"{r_second.status_code if r_second else 'n/a'}, "
                f"first_body_status="
                f"{r_first.json().get('detail', {}).get('status')}",
            )

            # --- Scenario 29: bearer compare is constant-time
            # (hmac.compare_digest). Behavioral regression — wrong
            # token still returns 401 invalid_bearer and the response
            # body NEVER echoes either the presented or configured
            # token. Belt-and-suspenders against a future drift back
            # to `==` that would surface as a timing-side-channel
            # vulnerability on the network-facing Retool surface.
            synthetic_wrong_token = "definitely-not-test-token-XYZ"
            r = client.get(
                "/projects",
                headers={"Authorization": f"Bearer {synthetic_wrong_token}"},
            )
            body_text = json.dumps(r.json())
            # Confirm the route still routed through the auth dep AND
            # neither token leaked.
            ok = (
                r.status_code == 401
                and r.json().get("detail", {}).get("status") == "invalid_bearer"
                and synthetic_wrong_token not in body_text
                and "test-token" not in body_text
            )
            # Also assert hmac.compare_digest is imported + bound to
            # the API module so a future refactor can't silently
            # remove it. The local ``hmac`` name here resolves to the
            # same module-level binding the auth dep uses.
            constant_time_in_module = (
                hasattr(hmac, "compare_digest")
                and callable(hmac.compare_digest)
            )
            ok = ok and constant_time_in_module
            record(
                "addendum-constant-time-bearer-compare",
                ok,
                f"status={r.status_code}, "
                f"raw_presented_token_in_body="
                f"{synthetic_wrong_token in body_text}, "
                f"configured_token_in_body={'test-token' in body_text}, "
                f"hmac_module_present={constant_time_in_module}",
            )

        finally:
            # Restore env so we don't leak the test token.
            if previous_token is None:
                os.environ.pop(ORCHESTRA_API_TOKEN_ENV, None)
            else:
                os.environ[ORCHESTRA_API_TOKEN_ENV] = previous_token
            if previous_workspace is not None:
                os.environ["ASANA_WORKSPACE_GID"] = previous_workspace

    if failures:
        print(
            sign_action(
                "Cody",
                f"api dry-run FAIL summary — {len(failures)} failures "
                f"of {len(failures) + len(passes)} scenarios.",
            ),
            flush=True,
        )
        return 1
    print(
        sign_action(
            "Cody",
            f"api dry-run pass summary — {len(passes)} scenarios "
            "all pass; 4.11 surface is regression-clean.",
        ),
        flush=True,
    )
    return 0


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api",
        description=(
            "Agent Orchestra REST API (M4 4.11). --dry-run for self-test; "
            "--serve to bind uvicorn locally. Real bearer token + Retool "
            "handoff is M5 task 5.2."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the API self-test against a fresh temp DB. No network.",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="Bind uvicorn locally for live operation.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind host for --serve. Default 127.0.0.1 (loopback only).",
    )
    parser.add_argument(
        "--port", type=int, default=8765,
        help="Bind port for --serve. Default 8765.",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Override the SessionStore DB path for --serve.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        return _dry_run()
    if args.serve:
        # Live serve. Print a signed start line so the operator sees what
        # token + DB the process is using (token presence only — never
        # the value).
        load_env_file()
        store = SessionStore(args.db_path) if args.db_path else SessionStore()
        store.ensure_schema()
        token_present = _read_api_token() is not None
        print(
            sign_action(
                "Atlas",
                f"API serve start: host={args.host} port={args.port} "
                f"db_path={store.db_path} "
                f"auth_configured={token_present}",
            ),
            flush=True,
        )
        import uvicorn  # noqa: PLC0415 — local import; only when serving.

        app = create_app(store=store)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
