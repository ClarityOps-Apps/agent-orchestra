# Agent Orchestra Runtime

Phase 1 runtime scaffold for Agent Orchestra.

## Operator CLI — `python orchestra.py --directive "..."` (M4 4.8)

Polished operator entry point. Routes through `runtime/supervisor.py` (`run_supervisor` + `resume_supervisor`) and persists every run via `SessionStore` (4.6 schema v2 with the 4.7 `tool_calls` table). Each phase boundary emits a signed activity line in real time via the `event_sink` callback wired into the supervisor.

Common shapes:

```bash
# Orchestrated run, default DB at runtime/memory/sessions.db.
uv run python orchestra.py --directive "Ask Cody to acknowledge the 4.8 smoke."

# Dry-run — full hook + persistence chain, no provider calls.
uv run python orchestra.py --directive "Ask Cody to acknowledge." --dry-run

# Custom DB path + step budget.
uv run python orchestra.py --directive "..." --max-steps 3 --db-path /tmp/orchestra.db

# Resume an existing run_id or session_id (terminal-state rehydrate prints
# the stored run; in-flight resume re-runs only the missing phases).
uv run python orchestra.py --resume <run_id_or_session_id> --db-path /tmp/orchestra.db
```

### Exit codes

| Code | Meaning                                  | Run statuses                                  |
|------|------------------------------------------|-----------------------------------------------|
| `0`  | Complete                                 | `complete`                                    |
| `10` | Operator action required (not a crash)   | `blocked`, `pending_human_approval`           |
| `1`  | Errored or unexpected runtime failure    | `errored` and any unknown state               |

The final signed line names the exit category (`complete` / `operator-action-required` / `errored`). When exit is `10`, every persisted blocker/error line and a resume hint are printed before the CLI returns.

### Inspecting persisted runs

```bash
uv run python -m session_store --list-sessions
uv run python -m session_store --show <run_id_or_session_id>
```

### Legacy positional smoke

Preserved unchanged for M2/M3/M4 receipts. Runs the no-op Atlas+Cody hook flow without contacting any provider:

```bash
python orchestra.py "hello team"
```

Expected result: signed log entries from Atlas and Cody, plus lifecycle entries in `memory/activity.log`.

No real model or MCP calls are made on the legacy path. API credentials belong in `.env` on the target machine and are never committed.

### Lifecycle flags (unchanged)

```bash
python orchestra.py --self-test           # hook smoke checks
python orchestra.py --daemon --interval N # long-lived heartbeat (systemd unit)
```

## REST API (M4 4.11)

`runtime/api.py` is the FastAPI surface backing the M5 Retool flight deck.
It composes with the existing supervisor / session_store / tool_registry
rather than duplicating them — the same code path the CLI runs serves
the REST endpoints, so signed activity, redaction, gate semantics, and
schema discipline stay in one place. Source of truth for the design:
`08-FLIGHT-DECK-PRD.md` v0.3, `07-PHASE-1-SCOPE-OF-WORK.md` v3.1, and
Atlas's 4.11 directive (Asana comment `1215607965656701`).

### Local run

```bash
# Self-test against a fresh temp DB — exercises every endpoint group,
# auth, project persistence, run snapshot serialization, SSE, halt,
# cost estimate, gate persistence, and token redaction. No provider /
# network calls.
uv run python -m api --dry-run

# Bind uvicorn locally (loopback by default).
ORCHESTRA_API_TOKEN=$(cat /path/to/local-only-token) \
  uv run python -m api --serve --host 127.0.0.1 --port 8765

# Auto-generated docs (Swagger + OpenAPI schema) under /docs and
# /openapi.json once the server is up.
```

### Authentication

MVP single-user bearer auth per PRD Q4 lock.

- Set `ORCHESTRA_API_TOKEN` in `runtime/.env` or the process env.
- All protected endpoints require `Authorization: Bearer <token>`.
- When `ORCHESTRA_API_TOKEN` is **absent**, protected endpoints fail
  closed with `503 auth_unavailable` — they do NOT silently allow
  access. Health (`GET /health`) and OpenAPI docs remain public.
- Real token generation + Retool secret handoff is **M5 task 5.2**, not
  4.11. The API ships with the bearer scaffold and refuses to operate
  without a configured token.

### Endpoint groups

| Group | Endpoints |
|---|---|
| Health | `GET /health` (public) |
| Projects | `GET /projects`, `POST /projects`, `GET /projects/{id}`, `PATCH /projects/{id}` |
| Directives & Runs | `POST /projects/{id}/directive?dry_run=`, `GET /projects/{id}/runs`, `GET /projects/{id}/runs/{run_id}`, `GET /.../stream` (SSE), `POST /.../halt`, `GET /.../preview`, `GET /.../cost_estimate` |
| Gates | `POST /.../gates/{gate_id}/approve`, `POST /.../gates/{gate_id}/reject` |
| Agents | `GET /projects/{id}/agents` |
| Integrations | `GET /projects/{id}/integrations`, `POST /.../{type}/auth`, `DELETE /.../{type}` |
| Artifact Preview | `GET /projects/{id}/runs/{run_id}/artifacts/{artifact_id}/diff` |

Every protected response is JSON; every signed line in the persisted
runtime (planner content, step messages, tool-call summaries, decision
records, gate envelopes) is surfaced verbatim in the run-detail and SSE
payloads.

### Background runs + halt semantics

- `POST /projects/{id}/directive` returns a `202 Accepted` with a
  freshly-minted `run_id` / `session_id` immediately. The supervisor
  runs in a daemon thread against the same row; the operator can
  follow progress via `GET /runs/{run_id}` snapshots or the SSE
  stream.
- `POST /runs/{run_id}/halt` persists a signed
  `api_halt_request` decision and sets a per-run halt flag. The
  supervisor checks the flag at every safe boundary (between steps and
  before the finalizer) and halts with a signed
  `api_halt_requested_pre_step` / `_pre_finalizer` blocker. Mid-provider
  call interruption is **NOT** supported in MVP — the run halts at the
  next safe boundary.

### Gate approve/reject

- Approving records a signed human decision plus updates the gate row
  to `approved`. It returns
  `approved_recorded_execution_not_resumed` because safely resuming a
  halted human-approved-only tool call would need raw-arg persistence
  (potentially secret-bearing). The operator's signed approval is
  durable; M5+ owns the live continue surface.
- Rejecting records a signed `reject_recorded` decision.

### Cost estimate

- Deterministic heuristic (no billing API). Token estimate ≈
  `len(directive) / 4` × planner overhead; output ≈ input × 2; cost
  via the per-agent pricing constants in `COST_ESTIMATE_PRICING`
  (Atlas/Cody/Scribe-Scout).
- Returns a low/high band, the approval threshold, and a
  `requires_approval` flag.

### M5 Retool handoff notes

- M5 task 5.1 onwards configures Retool to call this API. The 4.11
  surface is the contract; Retool clicks should never need code-level
  changes to the runtime.
- M5 task 5.2 owns token generation, rotation, and the secret handoff
  into Retool's vault. **Never** commit a real bearer token or echo
  one through the API.
- The Retool app should bind to `/health` for connection status, then
  hold the bearer token in its secret manager and pass it on every
  protected call.

### Schema v3

- Adds the `projects` table and a nullable `sessions.project_id`
  column (4.11 migration).
- Existing v1/v2 databases auto-upgrade on first `ensure_schema()` —
  the ALTER is guarded by a `PRAGMA table_info(sessions)` check so it
  runs exactly once.
- Legacy CLI sessions (4.6 / 4.8) with `project_id=NULL` keep loading
  fine; the API surfaces them under a sentinel `unscoped_legacy_sessions`
  count on `GET /projects` and never mixes them into a project's run
  list.

## Runtime health-check (M4 4.9)

A tiny stdlib-only health surface lives at `runtime/status.py`. Two CLI shapes:

```bash
uv run python -m status --json        # JSON payload; exit 0 on healthy
uv run python -m status --self-test   # signed Atlas PASS line; exit 0 on healthy
```

The payload includes `service`, `ok`, `runtime_root`, `python_version`, `checked_at_utc`, and a `checks` array. No network, no secrets, no DB writes.

## VPS deployment prerequisite — `uv`

The DigitalOcean runtime host (`agent-orchestra-1` at `159.89.86.113`) must have [`uv`](https://docs.astral.sh/uv/) (Astral's Python package + project manager) installed. From M4 onward, `pyproject.toml` declares runtime dependencies (`openai`, `anthropic`) that the supervisor loop, provider adapters, and factory all rely on, and `uv.lock` is the authoritative lockfile. `uv sync` installs the locked dependencies into `runtime/.venv` on every deploy; `uv run python -m llm.*` is how the factory and provider dry-runs / pings get executed.

### Install (one-time, run as `root` on the VPS)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Source: <https://docs.astral.sh/uv/getting-started/installation/>

The Astral standalone installer writes the binary to `/root/.local/bin/uv`. Verify:

```bash
/root/.local/bin/uv --version
# uv 0.11.19 (or newer)
```

### PATH note

Non-interactive SSH shells (the kind used by deploy scripts and the orchestra MCP path) do **not** have `~/.local/bin` on `PATH` by default. Two options:

1. Always invoke uv with the explicit path: `/root/.local/bin/uv sync`, `/root/.local/bin/uv run python -m llm.agent_factory --dry-run`, etc. This is the convention deploy scripts use today.
2. Add the PATH update to `~/.bashrc` (or `/etc/profile.d/uv.sh` for system-wide) so interactive root shells pick it up: `source $HOME/.local/bin/env`. This is optional and only helps interactive sessions.

The systemd unit `orchestra.service` uses `/usr/bin/python3` directly and does not depend on `uv` at all today — the daemon does not import the provider SDKs. That changes when M4 task 4.5 (supervisor loop) wires the providers into the orchestra runtime; at that point the systemd unit will need to either point at `uv run python` or at `/opt/agent-orchestra/runtime/.venv/bin/python`. That deploy change is in 4.5's scope, not this README's.

### Standard deploy step (after any commit that touches `pyproject.toml` / `uv.lock`)

```bash
ssh root@159.89.86.113 'set -e
cd /opt/agent-orchestra
git fetch origin main && git pull --ff-only origin main
cd runtime
/root/.local/bin/uv sync
systemctl restart orchestra.service
systemctl status orchestra.service --no-pager | head -5
python3 orchestra.py --self-test
python3 orchestra.py "hello team"
/root/.local/bin/uv run python -m llm.agent_factory --dry-run
'
```

Rollback for the uv installation itself: `rm -rf /root/.local/bin/uv /root/.local/bin/uvx /root/.cache/uv`.
