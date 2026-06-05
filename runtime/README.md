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
