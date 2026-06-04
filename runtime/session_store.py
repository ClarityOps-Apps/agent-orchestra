"""SQLite-backed session persistence for Agent Orchestra (M4 task 4.6).

Durable memory layer for the supervisor loop. Each `run_supervisor()`
invocation can be persisted incrementally so that:

- Supervisor runs survive process restart with a coherent prefix.
- Subsequent invocations can rehydrate or resume an interrupted run.
- 4.8 (public CLI) and 4.11 (REST API) can later query past sessions
  without re-running the agents.

Per Atlas's 4.6 packet (Asana comment ``1215432160850930``), this module
owns:

- Connection lifecycle with WAL journaling.
- Idempotent schema initialization with a versioned migrations table.
- Five tables: ``sessions``, ``messages``, ``actions``, ``gates``,
  ``decisions``.
- Defense-in-depth ``redact()`` on free-form text before any row write.
- Already-signed text (envelope ``content``, blocker/error
  ``signed_message``) is preserved verbatim.
- Read APIs that 4.8/4.11 can call.
- A rehydration API that lifts persisted state into
  ``SupervisorRun`` / ``SupervisorStep`` objects from
  ``runtime.supervisor``.

This module does NOT implement:

- The supervisor loop itself (``runtime/supervisor.py``).
- Resume scheduling — ``resume_supervisor()`` lives in supervisor.py so it
  can call back into the planner/step/finalizer functions; this module
  exposes the read primitives it needs.
- MCP tool execution (task 4.7).
- The public CLI on ``orchestra.py`` (task 4.8).
- REST endpoints (task 4.11).

Module CLI for validation (from ``runtime/``):

    uv run python -m session_store --init
    uv run python -m session_store --self-test
    uv run python -m session_store --list-sessions
    uv run python -m session_store --show <run_id_or_session_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from hooks.identity_signing import enforce_signed, sign_action
from hooks.secrets_check import find_secrets, redact


# --- Constants ---------------------------------------------------------------

#: Schema version managed by both ``PRAGMA user_version`` and the
#: ``schema_migrations`` bookkeeping table. Bump when the schema changes
#: and add a migration in ``_MIGRATIONS``.
SCHEMA_VERSION = 1

#: Runtime root (one level up from this file). Default DB path lives at
#: ``runtime/memory/sessions.db``, already covered by the
#: ``runtime/memory/*.db`` gitignore rule.
_RUNTIME_ROOT = Path(__file__).resolve().parent

#: Default on-disk location for the session store. Override with the
#: ``ORCHESTRA_SESSIONS_DB`` env var or by passing ``db_path`` explicitly.
DEFAULT_DB_PATH = _RUNTIME_ROOT / "memory" / "sessions.db"

#: Env var override for the DB path. Honored by ``SessionStore()`` when
#: no explicit ``db_path`` is given.
ORCHESTRA_SESSIONS_DB_ENV = "ORCHESTRA_SESSIONS_DB"

#: Busy-timeout for the SQLite connection (ms). Used to wait for a held
#: lock instead of immediately failing under WAL.
BUSY_TIMEOUT_MS = 5000

#: Kinds of supervisor-owned decisions persisted to the ``decisions``
#: table. Mirrors the two helpers in ``runtime/supervisor.py``.
DECISION_KIND_BLOCKER = "blocker"
DECISION_KIND_ERROR = "error"
VALID_DECISION_KINDS: frozenset[str] = frozenset(
    {DECISION_KIND_BLOCKER, DECISION_KIND_ERROR}
)

#: Phases under which messages can be persisted. Matches the supervisor
#: loop's phase names.
PHASE_PLANNER = "planner"
PHASE_STEP = "step"
PHASE_FINALIZER = "finalizer"
PHASE_BLOCKER = "blocker"
VALID_PHASES: frozenset[str] = frozenset(
    {PHASE_PLANNER, PHASE_STEP, PHASE_FINALIZER, PHASE_BLOCKER}
)

#: Gate lifecycle states. 4.6 only writes ``pending``; 4.11 will own the
#: resolution transitions.
GATE_STATUS_PENDING = "pending"
GATE_STATUS_APPROVED = "approved"
GATE_STATUS_REJECTED = "rejected"
VALID_GATE_STATUSES: frozenset[str] = frozenset(
    {GATE_STATUS_PENDING, GATE_STATUS_APPROVED, GATE_STATUS_REJECTED}
)


class SessionStoreError(RuntimeError):
    """Raised on persistence-layer failures that callers should surface."""


# --- Schema ------------------------------------------------------------------

# ``messages.id`` is the canonical envelope id from
# ``llm.types.MessageEnvelope``. ``actions.response_envelope_id`` references
# it via FK so the response envelope is queryable by step. ``gates`` and
# ``decisions`` are keyed by their own UUIDs so multiple gates/decisions
# per (run, step) are storable. Foreign keys cascade on session delete so
# operator cleanup leaves no orphans.
_SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version       INTEGER PRIMARY KEY,
        applied_at    TEXT    NOT NULL,
        description   TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        run_id                TEXT    PRIMARY KEY,
        session_id            TEXT    NOT NULL UNIQUE,
        directive_summary     TEXT    NOT NULL,
        status                TEXT    NOT NULL,
        max_steps             INTEGER NOT NULL,
        dry_run               INTEGER NOT NULL,
        created_at            TEXT    NOT NULL,
        updated_at            TEXT    NOT NULL,
        completed_at          TEXT,
        planner_envelope_id   TEXT,
        finalizer_envelope_id TEXT,
        plan_json             TEXT,
        error_count           INTEGER NOT NULL DEFAULT 0,
        blocker_count         INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id              TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL,
        session_id      TEXT NOT NULL,
        parent_id       TEXT,
        sender          TEXT NOT NULL,
        target          TEXT NOT NULL,
        message_type    TEXT NOT NULL,
        action_surface  TEXT NOT NULL,
        content         TEXT NOT NULL,
        metadata_json   TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        phase           TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES sessions(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS actions (
        run_id               TEXT    NOT NULL,
        session_id           TEXT    NOT NULL,
        step_id              INTEGER NOT NULL,
        target               TEXT    NOT NULL,
        action_surface       TEXT    NOT NULL,
        message              TEXT    NOT NULL,
        reason               TEXT    NOT NULL,
        status               TEXT    NOT NULL,
        started_at           TEXT,
        completed_at         TEXT,
        response_envelope_id TEXT,
        PRIMARY KEY (run_id, step_id),
        FOREIGN KEY (run_id) REFERENCES sessions(run_id) ON DELETE CASCADE,
        FOREIGN KEY (response_envelope_id) REFERENCES messages(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gates (
        gate_id         TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL,
        session_id      TEXT NOT NULL,
        step_id         INTEGER,
        target          TEXT NOT NULL,
        action_surface  TEXT NOT NULL,
        status          TEXT NOT NULL,
        signed_message  TEXT NOT NULL,
        metadata_json   TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        resolved_at     TEXT,
        resolved_by     TEXT,
        rationale       TEXT,
        FOREIGN KEY (run_id) REFERENCES sessions(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id      TEXT NOT NULL PRIMARY KEY,
        run_id           TEXT NOT NULL,
        session_id       TEXT NOT NULL,
        kind             TEXT NOT NULL,
        phase            TEXT NOT NULL,
        signed_message   TEXT NOT NULL,
        reason_or_error  TEXT NOT NULL,
        metadata_json    TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES sessions(run_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_gates_status ON gates(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_session_created ON decisions(session_id, created_at)",
)


_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "initial schema: sessions, messages, actions, gates, decisions"),
)


# --- Helpers -----------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt: datetime | None) -> str | None:
    """Format a ``datetime`` as UTC ISO Z; ``None`` passes through."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_iso(value: str | None) -> datetime | None:
    """Parse a UTC ISO Z string back into an aware ``datetime``."""
    if value is None:
        return None
    text = value.rstrip("Z")
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _dump_metadata(metadata: Any) -> str:
    """Serialize a metadata dict to canonical JSON with redaction.

    Free-form text fields inside metadata may carry sender-controlled
    strings; we run ``redact()`` defensively across the whole JSON blob
    so synthetic secret-shaped tokens cannot survive in stored rows
    even when 4.4 envelope content was already redacted.
    """
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise SessionStoreError(
            f"metadata must be dict-like, got {type(metadata).__name__}"
        )
    payload = json.dumps(metadata, sort_keys=True, ensure_ascii=False, default=str)
    return redact(payload) if find_secrets(payload) else payload


def _safe_text(value: str | None) -> str | None:
    """Apply ``redact()`` to free-form text before persistence."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return redact(value) if find_secrets(value) else value


def _resolve_db_path(db_path: str | Path | None) -> Path:
    """Resolve the SQLite DB path with env-override precedence.

    Precedence:
      1. Explicit ``db_path`` argument.
      2. ``ORCHESTRA_SESSIONS_DB`` env var.
      3. ``DEFAULT_DB_PATH``.

    Relative paths are resolved against the runtime root, mirroring the
    hooks' ``_audit_dir()`` convention.
    """
    if db_path is not None:
        candidate = Path(db_path)
    else:
        env_val = os.environ.get(ORCHESTRA_SESSIONS_DB_ENV)
        candidate = Path(env_val) if env_val else DEFAULT_DB_PATH
    if not candidate.is_absolute():
        candidate = _RUNTIME_ROOT / candidate
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


# --- Persisted rows ---------------------------------------------------------


@dataclass(frozen=True)
class PersistedSession:
    """A ``sessions`` row, decoded into a typed snapshot.

    Used by read APIs so callers do not need to know SQLite Row indexing.
    """

    run_id: str
    session_id: str
    directive_summary: str
    status: str
    max_steps: int
    dry_run: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    planner_envelope_id: str | None
    finalizer_envelope_id: str | None
    plan: dict[str, Any] | None
    error_count: int
    blocker_count: int


# --- The store ---------------------------------------------------------------


class SessionStore:
    """SQLite-backed durable session memory for the supervisor loop.

    Construct once per process. ``init_db()`` is idempotent; calling it
    again is a no-op once the schema is at the current
    ``SCHEMA_VERSION``. All write APIs accept primitive arguments to
    keep this module independent of ``llm.types`` and ``supervisor``
    at import time — the rehydration helper imports those lazily.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path: Path = _resolve_db_path(db_path)

    # --- connection helpers ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection with PRAGMAs applied.

        Each call opens a fresh connection; the surrounding ``transaction``
        helper manages commit/rollback. WAL mode is process-wide once set
        on the database; the first connection sets it, later connections
        inherit it.
        """
        conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # manual transactions via BEGIN/COMMIT
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager that yields a connection and closes it."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Wrap a unit of work in a SQLite transaction.

        Begins IMMEDIATE so concurrent writers fail fast rather than
        deadlock at COMMIT. Commits on success; rolls back on any
        exception (the exception then propagates).
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    # --- schema ------------------------------------------------------------

    def init_db(self) -> int:
        """Idempotently apply the schema. Returns the resulting version."""
        with self.transaction() as conn:
            for ddl in _SCHEMA_DDL:
                conn.execute(ddl)
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            applied_at = _utc_now_iso()
            for version, description in _MIGRATIONS:
                if version > current:
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations "
                        "(version, applied_at, description) VALUES (?, ?, ?)",
                        (version, applied_at, description),
                    )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return SCHEMA_VERSION

    def ensure_schema(self) -> int:
        """Synonym for ``init_db()`` for callers that prefer the name."""
        return self.init_db()

    def schema_version(self) -> int:
        """Return the ``PRAGMA user_version`` recorded in the DB."""
        with self.connection() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    # --- writes: sessions --------------------------------------------------

    def record_session(
        self,
        *,
        run_id: str,
        session_id: str,
        directive_summary: str,
        status: str,
        max_steps: int,
        dry_run: bool,
        created_at: datetime,
    ) -> None:
        """Insert (or upsert) the initial session row."""
        now = _iso(created_at) or _utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    run_id, session_id, directive_summary, status, max_steps,
                    dry_run, created_at, updated_at,
                    completed_at, planner_envelope_id, finalizer_envelope_id,
                    plan_json, error_count, blocker_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 0, 0)
                ON CONFLICT(run_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    directive_summary = excluded.directive_summary,
                    status = excluded.status,
                    max_steps = excluded.max_steps,
                    dry_run = excluded.dry_run,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    session_id,
                    _safe_text(directive_summary) or "",
                    status,
                    int(max_steps),
                    1 if dry_run else 0,
                    now,
                    now,
                ),
            )

    def update_session(
        self,
        run_id: str,
        *,
        status: str | None = None,
        planner_envelope_id: str | None = None,
        finalizer_envelope_id: str | None = None,
        plan: dict[str, Any] | None = None,
        completed_at: datetime | None = None,
        error_count: int | None = None,
        blocker_count: int | None = None,
    ) -> None:
        """Partial-update a session row. ``None`` fields are left alone."""
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [_utc_now_iso()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if planner_envelope_id is not None:
            sets.append("planner_envelope_id = ?")
            params.append(planner_envelope_id)
        if finalizer_envelope_id is not None:
            sets.append("finalizer_envelope_id = ?")
            params.append(finalizer_envelope_id)
        if plan is not None:
            sets.append("plan_json = ?")
            params.append(_dump_metadata(plan))
        if completed_at is not None:
            sets.append("completed_at = ?")
            params.append(_iso(completed_at))
        if error_count is not None:
            sets.append("error_count = ?")
            params.append(int(error_count))
        if blocker_count is not None:
            sets.append("blocker_count = ?")
            params.append(int(blocker_count))
        params.append(run_id)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE run_id = ?",
                params,
            )

    # --- writes: messages --------------------------------------------------

    def record_message(
        self,
        *,
        envelope_id: str,
        run_id: str,
        session_id: str,
        parent_id: str | None,
        sender: str,
        target: str,
        message_type: str,
        action_surface: str,
        content: str,
        metadata: dict[str, Any] | None,
        created_at: datetime,
        phase: str,
    ) -> None:
        """Persist a ``MessageEnvelope`` row.

        ``content`` is expected to be already signed/redacted by the 4.4
        protocol layer; we preserve it verbatim. ``metadata`` runs
        through ``_dump_metadata()`` which applies defense-in-depth
        redaction.
        """
        if phase not in VALID_PHASES:
            raise SessionStoreError(
                f"record_message: unknown phase {phase!r}; valid {sorted(VALID_PHASES)}"
            )
        # Defense-in-depth: 4.4's inbound protocol redaction normally cleans
        # provider responses before they reach this layer, but synthetic
        # envelopes injected via supervisor dry-run hooks (or any future
        # bug) could carry secret-shaped strings. If find_secrets() detects
        # a token in the content, fall back to redact() before persistence.
        # Already-signed `[Atlas · ...]` prefix is preserved by redact()
        # because the signature itself doesn't match any secret pattern.
        safe_content = (
            redact(content) if isinstance(content, str) and find_secrets(content) else content
        )
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO messages (
                    id, run_id, session_id, parent_id, sender, target,
                    message_type, action_surface, content, metadata_json,
                    created_at, phase
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope_id,
                    run_id,
                    session_id,
                    parent_id,
                    sender,
                    target,
                    message_type,
                    action_surface,
                    safe_content,
                    _dump_metadata(metadata or {}),
                    _iso(created_at) or _utc_now_iso(),
                    phase,
                ),
            )

    # --- writes: actions ---------------------------------------------------

    def record_action(
        self,
        *,
        run_id: str,
        session_id: str,
        step_id: int,
        target: str,
        action_surface: str,
        message: str,
        reason: str,
        status: str,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        response_envelope_id: str | None = None,
    ) -> None:
        """Upsert an ``actions`` row for one planned/executed step.

        ``message`` and ``reason`` are redacted defensively even though the
        supervisor's own planner-validation pipeline should reject any
        secret-shaped step content well before this layer runs.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO actions (
                    run_id, session_id, step_id, target, action_surface,
                    message, reason, status, started_at, completed_at,
                    response_envelope_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, step_id) DO UPDATE SET
                    target = excluded.target,
                    action_surface = excluded.action_surface,
                    message = excluded.message,
                    reason = excluded.reason,
                    status = excluded.status,
                    started_at = COALESCE(excluded.started_at, actions.started_at),
                    completed_at = COALESCE(excluded.completed_at, actions.completed_at),
                    response_envelope_id = COALESCE(
                        excluded.response_envelope_id, actions.response_envelope_id
                    )
                """,
                (
                    run_id,
                    session_id,
                    int(step_id),
                    target,
                    action_surface,
                    _safe_text(message) or "",
                    _safe_text(reason) or "",
                    status,
                    _iso(started_at),
                    _iso(completed_at),
                    response_envelope_id,
                ),
            )

    def update_action(
        self,
        run_id: str,
        step_id: int,
        *,
        status: str | None = None,
        response_envelope_id: str | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """Partial-update an action row, leaving unspecified fields alone."""
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if response_envelope_id is not None:
            sets.append("response_envelope_id = ?")
            params.append(response_envelope_id)
        if completed_at is not None:
            sets.append("completed_at = ?")
            params.append(_iso(completed_at))
        if not sets:
            return
        params.extend([run_id, int(step_id)])
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE actions SET {', '.join(sets)} "
                "WHERE run_id = ? AND step_id = ?",
                params,
            )

    # --- writes: gates -----------------------------------------------------

    def record_gate(
        self,
        *,
        run_id: str,
        session_id: str,
        step_id: int | None,
        target: str,
        action_surface: str,
        signed_message: str,
        metadata: dict[str, Any] | None,
        created_at: datetime,
    ) -> str:
        """Insert a pending gate row. Returns the generated ``gate_id``.

        ``signed_message`` is checked with ``enforce_signed()`` to make
        sure callers are not persisting an unsigned line — gates are the
        operator-visible artifact 4.11 will surface.
        """
        enforce_signed(signed_message)
        gate_id = str(uuid4())
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO gates (
                    gate_id, run_id, session_id, step_id, target,
                    action_surface, status, signed_message, metadata_json,
                    created_at, resolved_at, resolved_by, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    gate_id,
                    run_id,
                    session_id,
                    int(step_id) if step_id is not None else None,
                    target,
                    action_surface,
                    GATE_STATUS_PENDING,
                    signed_message,
                    _dump_metadata(metadata or {}),
                    _iso(created_at) or _utc_now_iso(),
                ),
            )
        return gate_id

    # --- writes: decisions -------------------------------------------------

    def record_decision(
        self,
        *,
        run_id: str,
        session_id: str,
        kind: str,
        phase: str,
        signed_message: str,
        reason_or_error: str,
        metadata: dict[str, Any] | None,
        created_at: datetime,
    ) -> str:
        """Insert a supervisor-owned blocker/error decision row.

        ``signed_message`` is required and validated via
        ``enforce_signed()`` — the whole point of 4.5's signed-halt
        addendum is that every supervisor-owned halt carries an
        Atlas-signed artifact, and 4.6 makes it durable.
        """
        if kind not in VALID_DECISION_KINDS:
            raise SessionStoreError(
                f"record_decision: unknown kind {kind!r}; "
                f"valid {sorted(VALID_DECISION_KINDS)}"
            )
        enforce_signed(signed_message)
        decision_id = str(uuid4())
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO decisions (
                    decision_id, run_id, session_id, kind, phase,
                    signed_message, reason_or_error, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    session_id,
                    kind,
                    phase,
                    signed_message,
                    _safe_text(reason_or_error) or "",
                    _dump_metadata(metadata or {}),
                    _iso(created_at) or _utc_now_iso(),
                ),
            )
        return decision_id

    # --- reads --------------------------------------------------------------

    def _resolve_run_id(self, conn: sqlite3.Connection, identifier: str) -> str | None:
        """Allow callers to pass either ``run_id`` or ``session_id``."""
        row = conn.execute(
            "SELECT run_id FROM sessions WHERE run_id = ? OR session_id = ?",
            (identifier, identifier),
        ).fetchone()
        return row["run_id"] if row else None

    def load_session(self, identifier: str) -> PersistedSession | None:
        """Return a ``PersistedSession`` for the run, or ``None``."""
        with self.connection() as conn:
            run_id = self._resolve_run_id(conn, identifier)
            if run_id is None:
                return None
            row = conn.execute(
                "SELECT * FROM sessions WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        plan = json.loads(row["plan_json"]) if row["plan_json"] else None
        return PersistedSession(
            run_id=row["run_id"],
            session_id=row["session_id"],
            directive_summary=row["directive_summary"],
            status=row["status"],
            max_steps=int(row["max_steps"]),
            dry_run=bool(row["dry_run"]),
            created_at=_from_iso(row["created_at"]) or datetime.now(UTC),
            updated_at=_from_iso(row["updated_at"]) or datetime.now(UTC),
            completed_at=_from_iso(row["completed_at"]),
            planner_envelope_id=row["planner_envelope_id"],
            finalizer_envelope_id=row["finalizer_envelope_id"],
            plan=plan,
            error_count=int(row["error_count"]),
            blocker_count=int(row["blocker_count"]),
        )

    def list_sessions(self, *, limit: int = 50) -> list[PersistedSession]:
        """Return the most recent sessions."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT run_id FROM sessions ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        sessions: list[PersistedSession] = []
        for row in rows:
            session = self.load_session(row["run_id"])
            if session is not None:
                sessions.append(session)
        return sessions

    def load_messages(self, identifier: str) -> list[dict[str, Any]]:
        """Return all message rows for a run, oldest first."""
        with self.connection() as conn:
            run_id = self._resolve_run_id(conn, identifier)
            if run_id is None:
                return []
            rows = conn.execute(
                "SELECT * FROM messages WHERE run_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_actions(self, identifier: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            run_id = self._resolve_run_id(conn, identifier)
            if run_id is None:
                return []
            rows = conn.execute(
                "SELECT * FROM actions WHERE run_id = ? ORDER BY step_id ASC",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_gates(self, identifier: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            run_id = self._resolve_run_id(conn, identifier)
            if run_id is None:
                return []
            rows = conn.execute(
                "SELECT * FROM gates WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_decisions(self, identifier: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            run_id = self._resolve_run_id(conn, identifier)
            if run_id is None:
                return []
            rows = conn.execute(
                "SELECT * FROM decisions WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- rehydration -------------------------------------------------------

    def load_supervisor_run(self, identifier: str) -> Any | None:
        """Rebuild a ``runtime.supervisor.SupervisorRun`` from the DB.

        Imports ``runtime.supervisor`` and ``llm.types`` lazily so this
        module stays usable from utility contexts (CLI ``--list-sessions``
        etc.) without dragging the whole supervisor module in.
        """
        from llm.types import MessageEnvelope  # local: avoid top-level cycle
        from supervisor import SupervisorRun, SupervisorStep  # local

        session = self.load_session(identifier)
        if session is None:
            return None

        message_rows = {m["id"]: m for m in self.load_messages(identifier)}
        action_rows = self.load_actions(identifier)
        decisions = self.load_decisions(identifier)

        def _row_to_envelope(row: dict[str, Any]) -> MessageEnvelope:
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
                created_at=_from_iso(row["created_at"]) or datetime.now(UTC),
            )

        planner_envelope = (
            _row_to_envelope(message_rows[session.planner_envelope_id])
            if session.planner_envelope_id
            and session.planner_envelope_id in message_rows
            else None
        )
        finalizer_envelope = (
            _row_to_envelope(message_rows[session.finalizer_envelope_id])
            if session.finalizer_envelope_id
            and session.finalizer_envelope_id in message_rows
            else None
        )

        steps: list[SupervisorStep] = []
        for action in action_rows:
            response_envelope = None
            resp_id = action["response_envelope_id"]
            if resp_id and resp_id in message_rows:
                response_envelope = _row_to_envelope(message_rows[resp_id])
            steps.append(
                SupervisorStep(
                    id=int(action["step_id"]),
                    target=action["target"],
                    message=action["message"],
                    action_surface=action["action_surface"],
                    reason=action["reason"],
                    status=action["status"],
                    response_envelope=response_envelope,
                    started_at=_from_iso(action["started_at"]),
                    completed_at=_from_iso(action["completed_at"]),
                )
            )

        blockers: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for decision in decisions:
            record = {
                "phase": decision["phase"],
                "signed_message": decision["signed_message"],
            }
            try:
                meta = json.loads(decision["metadata_json"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            record.update(meta)
            if decision["kind"] == DECISION_KIND_BLOCKER:
                record["reason"] = decision["reason_or_error"]
                blockers.append(record)
            else:
                record["error"] = decision["reason_or_error"]
                errors.append(record)

        return SupervisorRun(
            run_id=session.run_id,
            session_id=session.session_id,
            directive_summary=session.directive_summary,
            status=session.status,
            created_at=session.created_at,
            completed_at=session.completed_at,
            planner_envelope=planner_envelope,
            plan=session.plan,
            steps=tuple(steps),
            finalizer_envelope=finalizer_envelope,
            blockers=tuple(blockers),
            errors=tuple(errors),
            max_steps=session.max_steps,
            dry_run=session.dry_run,
        )


# --- Convenience module-level entry points ----------------------------------


def init_db(db_path: str | Path | None = None) -> int:
    """Initialize the schema at ``db_path`` (or default). Idempotent."""
    return SessionStore(db_path).init_db()


# --- Self-test --------------------------------------------------------------


def _self_test() -> int:
    """Round-trip every write/read API against a fresh temp DB.

    Exits 0 on success, 1 on any failure. Designed to run without
    credentials and without any network call.
    """
    failures: list[str] = []

    def record(case: str, ok: bool, detail: str) -> None:
        if not ok:
            failures.append(f"{case}: {detail}")
        print(sign_action("Cody", f"session_store self-test {'pass' if ok else 'FAIL'} — {case}: {detail}"))

    with tempfile.TemporaryDirectory(prefix="orchestra-session-store-test-") as tmp:
        db_path = Path(tmp) / "sessions.db"
        store = SessionStore(db_path)

        # 1. Schema init
        try:
            version = store.init_db()
            record("schema-init", version == SCHEMA_VERSION,
                   f"PRAGMA user_version={version}")
        except Exception as exc:  # noqa: BLE001
            record("schema-init", False, f"{type(exc).__name__}: {exc}")
            return 1

        # 2. Idempotent re-init
        try:
            store.init_db()
            record("schema-idempotent", True, "second init_db() did not raise")
        except Exception as exc:  # noqa: BLE001
            record("schema-idempotent", False, f"{type(exc).__name__}: {exc}")

        # 3. Session row round-trip
        run_id = str(uuid4())
        session_id = str(uuid4())
        now = datetime.now(UTC)
        try:
            store.record_session(
                run_id=run_id,
                session_id=session_id,
                directive_summary="self-test directive",
                status="planning",
                max_steps=5,
                dry_run=True,
                created_at=now,
            )
            loaded = store.load_session(run_id)
            ok = (
                loaded is not None
                and loaded.run_id == run_id
                and loaded.session_id == session_id
                and loaded.status == "planning"
                and loaded.dry_run is True
                and loaded.max_steps == 5
            )
            record("session-roundtrip", ok, f"loaded={loaded}")
            # Also via session_id
            via_session = store.load_session(session_id)
            record("session-load-by-session-id", via_session is not None and via_session.run_id == run_id,
                   f"resolved run_id={via_session.run_id if via_session else None}")
        except Exception as exc:  # noqa: BLE001
            record("session-roundtrip", False, f"{type(exc).__name__}: {exc}")

        # 4. Message round-trip
        envelope_id = str(uuid4())
        try:
            store.record_message(
                envelope_id=envelope_id,
                run_id=run_id,
                session_id=session_id,
                parent_id=None,
                sender="Atlas",
                target="Atlas",
                message_type="response",
                action_surface="safe",
                content="[Atlas · 2026-06-04T22:30Z] planner envelope content",
                metadata={"phase": "planner"},
                created_at=now,
                phase=PHASE_PLANNER,
            )
            msgs = store.load_messages(run_id)
            record("message-roundtrip", len(msgs) == 1 and msgs[0]["id"] == envelope_id,
                   f"messages={len(msgs)}")
        except Exception as exc:  # noqa: BLE001
            record("message-roundtrip", False, f"{type(exc).__name__}: {exc}")

        # 5. Update session: link planner envelope + plan
        try:
            store.update_session(
                run_id,
                status="executing",
                planner_envelope_id=envelope_id,
                plan={"summary": "test", "steps": [{"id": 1}]},
            )
            loaded = store.load_session(run_id)
            ok = (
                loaded is not None
                and loaded.status == "executing"
                and loaded.planner_envelope_id == envelope_id
                and loaded.plan == {"summary": "test", "steps": [{"id": 1}]}
            )
            record("session-update", ok, f"status={loaded.status if loaded else None}")
        except Exception as exc:  # noqa: BLE001
            record("session-update", False, f"{type(exc).__name__}: {exc}")

        # 6. Action round-trip + update
        try:
            store.record_action(
                run_id=run_id,
                session_id=session_id,
                step_id=1,
                target="Cody",
                action_surface="safe",
                message="Acknowledge.",
                reason="self-test",
                status="planned",
                started_at=now,
            )
            store.update_action(
                run_id, 1, status="responded",
                response_envelope_id=envelope_id, completed_at=now,
            )
            actions = store.load_actions(run_id)
            ok = (
                len(actions) == 1
                and actions[0]["status"] == "responded"
                and actions[0]["response_envelope_id"] == envelope_id
            )
            record("action-roundtrip", ok, f"actions={actions}")
        except Exception as exc:  # noqa: BLE001
            record("action-roundtrip", False, f"{type(exc).__name__}: {exc}")

        # 7. Decision (signed) round-trip; rejects unsigned
        try:
            signed = sign_action("Atlas", "Supervisor blocker [test]: synthetic")
            decision_id = store.record_decision(
                run_id=run_id,
                session_id=session_id,
                kind=DECISION_KIND_BLOCKER,
                phase="test",
                signed_message=signed,
                reason_or_error="synthetic blocker",
                metadata={"k": "v"},
                created_at=now,
            )
            decisions = store.load_decisions(run_id)
            ok = (
                len(decisions) == 1
                and decisions[0]["decision_id"] == decision_id
                and decisions[0]["signed_message"] == signed
            )
            record("decision-signed-roundtrip", ok, f"decisions={len(decisions)}")
        except Exception as exc:  # noqa: BLE001
            record("decision-signed-roundtrip", False, f"{type(exc).__name__}: {exc}")

        # 8. Decision rejects unsigned signed_message
        try:
            store.record_decision(
                run_id=run_id,
                session_id=session_id,
                kind=DECISION_KIND_BLOCKER,
                phase="test",
                signed_message="this is not signed",
                reason_or_error="should fail",
                metadata={},
                created_at=now,
            )
            record("decision-rejects-unsigned", False, "did NOT raise SignatureError")
        except Exception as exc:  # noqa: BLE001
            # enforce_signed raises SignatureError
            record("decision-rejects-unsigned", "SignatureError" in type(exc).__name__,
                   f"raised {type(exc).__name__}")

        # 9. Gate round-trip
        try:
            signed = sign_action("Atlas", "Blocked pending Garrett approval. APPROVAL REQUEST")
            gate_id = store.record_gate(
                run_id=run_id,
                session_id=session_id,
                step_id=1,
                target="Cody",
                action_surface="human-approved-only",
                signed_message=signed,
                metadata={"reason": "human-approval"},
                created_at=now,
            )
            gates = store.load_gates(run_id)
            ok = (
                len(gates) == 1
                and gates[0]["gate_id"] == gate_id
                and gates[0]["status"] == GATE_STATUS_PENDING
            )
            record("gate-roundtrip", ok, f"gates={len(gates)}")
        except Exception as exc:  # noqa: BLE001
            record("gate-roundtrip", False, f"{type(exc).__name__}: {exc}")

        # 10. Redaction proof — synthetic token in metadata must not survive
        try:
            secret_token = "sk-proj-AAAAAAAAAAAAAAAAAAAA"
            store.record_decision(
                run_id=run_id,
                session_id=session_id,
                kind=DECISION_KIND_ERROR,
                phase="redaction_test",
                signed_message=sign_action("Atlas", "Supervisor error [redaction_test]: synthetic"),
                reason_or_error=f"leaked {secret_token} in reason",
                metadata={"leak": secret_token, "nested": {"more": secret_token}},
                created_at=now,
            )
            decisions = store.load_decisions(run_id)
            relevant = [d for d in decisions if d["phase"] == "redaction_test"]
            ok = bool(relevant)
            if ok:
                d = relevant[0]
                blob = d["metadata_json"] + " " + d["reason_or_error"]
                if secret_token in blob:
                    ok = False
                    record("redaction-no-raw-secret", False,
                           f"raw token survived in row: {blob[:120]}")
                else:
                    record("redaction-no-raw-secret", True,
                           "synthetic token replaced with [REDACTED:openai_api_key]")
            else:
                record("redaction-no-raw-secret", False, "no redaction_test row")
        except Exception as exc:  # noqa: BLE001
            record("redaction-no-raw-secret", False, f"{type(exc).__name__}: {exc}")

        # 11. list_sessions returns at least our row
        try:
            sessions = store.list_sessions(limit=10)
            ok = any(s.run_id == run_id for s in sessions)
            record("list-sessions", ok, f"count={len(sessions)}")
        except Exception as exc:  # noqa: BLE001
            record("list-sessions", False, f"{type(exc).__name__}: {exc}")

        # 12. load_supervisor_run rehydration
        try:
            store.update_session(
                run_id,
                status="complete",
                finalizer_envelope_id=envelope_id,
                completed_at=now,
            )
            run = store.load_supervisor_run(run_id)
            ok = (
                run is not None
                and run.run_id == run_id
                and run.status == "complete"
                and len(run.steps) == 1
                and run.steps[0].target == "Cody"
                and run.planner_envelope is not None
                and run.finalizer_envelope is not None
                and len(run.blockers) >= 1  # the decision we wrote above
            )
            record("load-supervisor-run", ok, f"run.status={run.status if run else None}, steps={len(run.steps) if run else 0}")
        except Exception as exc:  # noqa: BLE001
            record("load-supervisor-run", False, f"{type(exc).__name__}: {exc}")

        # 13. ORCHESTRA_SESSIONS_DB env override
        try:
            env_tmp = Path(tmp) / "env_override.db"
            previous = os.environ.get(ORCHESTRA_SESSIONS_DB_ENV)
            os.environ[ORCHESTRA_SESSIONS_DB_ENV] = str(env_tmp)
            try:
                env_store = SessionStore()
                env_store.init_db()
                ok = env_tmp.exists()
                record("env-override", ok, f"path={env_tmp}")
            finally:
                if previous is None:
                    os.environ.pop(ORCHESTRA_SESSIONS_DB_ENV, None)
                else:
                    os.environ[ORCHESTRA_SESSIONS_DB_ENV] = previous
        except Exception as exc:  # noqa: BLE001
            record("env-override", False, f"{type(exc).__name__}: {exc}")

    return 0 if not failures else 1


# --- Module CLI -------------------------------------------------------------


def _format_session(s: PersistedSession) -> str:
    lines = [
        f"  run_id              : {s.run_id}",
        f"  session_id          : {s.session_id}",
        f"  directive_summary   : {s.directive_summary}",
        f"  status              : {s.status}",
        f"  max_steps / dry_run : {s.max_steps} / {s.dry_run}",
        f"  created_at          : {_iso(s.created_at)}",
        f"  updated_at          : {_iso(s.updated_at)}",
        f"  completed_at        : {_iso(s.completed_at) or '-'}",
        f"  planner_env_id      : {s.planner_envelope_id or '-'}",
        f"  finalizer_env_id    : {s.finalizer_envelope_id or '-'}",
        f"  error_count         : {s.error_count}",
        f"  blocker_count       : {s.blocker_count}",
    ]
    return "\n".join(lines)


def _cli_list(store: SessionStore, limit: int) -> int:
    sessions = store.list_sessions(limit=limit)
    if not sessions:
        print(sign_action("Cody", "session_store: no persisted sessions."))
        return 0
    print(sign_action("Cody", f"session_store: {len(sessions)} session(s) (most recent first)."))
    for s in sessions:
        print(_format_session(s))
        print("  ----")
    return 0


def _cli_show(store: SessionStore, identifier: str) -> int:
    session = store.load_session(identifier)
    if session is None:
        print(sign_action("Cody", f"session_store: no session matching {identifier!r}."))
        return 1
    print(sign_action("Cody", f"session_store: session {session.run_id}"))
    print(_format_session(session))
    msgs = store.load_messages(session.run_id)
    actions = store.load_actions(session.run_id)
    gates = store.load_gates(session.run_id)
    decisions = store.load_decisions(session.run_id)
    print(f"  messages   : {len(msgs)}")
    print(f"  actions    : {len(actions)}")
    print(f"  gates      : {len(gates)}")
    print(f"  decisions  : {len(decisions)}")
    for d in decisions:
        print(f"    [{d['kind']}:{d['phase']}] {d['signed_message']}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session_store",
        description="SQLite session persistence for Agent Orchestra (M4 task 4.6).",
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Initialize the schema at the default or ORCHESTRA_SESSIONS_DB path.",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Run round-trip checks against a fresh temp DB. No credentials needed.",
    )
    parser.add_argument(
        "--list-sessions", action="store_true",
        help="Print persisted sessions, most recent first.",
    )
    parser.add_argument(
        "--show", metavar="ID",
        help="Print details for one session (run_id or session_id).",
    )
    parser.add_argument(
        "--limit", type=int, default=25,
        help="Limit for --list-sessions. Default 25.",
    )
    parser.add_argument(
        "--db-path", metavar="PATH",
        help="Override DB path. Otherwise honors ORCHESTRA_SESSIONS_DB and the default.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    store = SessionStore(args.db_path)
    if args.init:
        version = store.init_db()
        print(sign_action(
            "Cody",
            f"session_store initialized at {store.db_path} (schema v{version}).",
        ))
        return 0
    if args.list_sessions:
        store.ensure_schema()
        return _cli_list(store, args.limit)
    if args.show:
        store.ensure_schema()
        return _cli_show(store, args.show)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
