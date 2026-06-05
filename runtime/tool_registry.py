"""Tool registry / router for Agent Orchestra (M4 task 4.7 main scope).

Supervisor-mediated MCP/tool access. The runtime owns:

  * a declarative ToolSpec registry,
  * an agent → allowed-tools permission matrix (Atlas, Cody, Scribe, Scout),
  * an `execute_tool()` entry point that runs the standard hook order
    (canonicalize → permission → surface → arg validation → secrets_check →
    approval_gates → execute → sign → redact → persist),
  * MCP server config loading from `runtime/mcp/*.json` with `${ENV_VAR}`
    resolution that never echoes secret values,
  * adapter executors for the read-only safe surface (Asana/GitHub read,
    filesystem read/list) so live read-only proofs work without a stdio
    MCP server,
  * fail-closed posture for every unauthorized / unknown / disabled path,
  * `--dry-run` CLI exercising the entire registry shape end-to-end.

This module deliberately does NOT:

  * spawn stdio MCP servers — that requires Node/npx which is not on the VPS
    and the directive forbids installing it without Garrett's approval.
    A live stdio MCP path can land in M5 once the VPS gains node.
  * implement the public `python orchestra.py --directive` CLI (task 4.8).
  * expose REST endpoints (task 4.11).
  * touch provider/factory/protocol/hooks/locked-prompt/agent surfaces.

References:
  * 4.7 directive — Asana comment 1215456053408413.
  * Step 1 handoff brief — Asana comment 1215456415651063.
  * Hook layer — `runtime/hooks/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from config import load_env_file, optional_env
from hooks.approval_gates import (
    GUARDED,
    HUMAN_APPROVED_ONLY,
    SAFE,
    VALID_SURFACES,
    check_approval_gate,
    classify_surface,
)
from hooks.identity_signing import sign_action
from hooks.secrets_check import check_for_secrets, find_secrets, redact
from llm.agent_factory import (
    CANONICAL_AGENT_NAMES,
    UnknownAgentError,
    _canonicalize,
)


# --- Errors ------------------------------------------------------------------


class ToolRegistryError(ValueError):
    """Base class for tool-registry failures that callers should surface."""


class UnknownToolError(ToolRegistryError):
    """The requested tool name is not registered."""


class UnauthorizedAgentToolError(ToolRegistryError):
    """The agent is not permitted to use the requested tool."""


class ToolArgError(ToolRegistryError):
    """A tool argument failed validation (path traversal, deny-listed file,
    destructive bash command, missing required arg, etc.)."""


class McpConfigError(ToolRegistryError):
    """The MCP config directory or one of its JSON files is malformed."""


# --- Frozen dataclasses ------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Declarative spec for a registered tool.

    `tool_name` is the namespaced public name (`asana.get_task`). `server_name`
    identifies the MCP config (`runtime/mcp/{server_name}.json`) that owns the
    transport. `action_surface` is the default classification that the
    `classify_surface()` hook may upgrade — for example, a guarded
    `bash.run_smoke` call carrying a `rm -rf` token escalates to
    `human-approved-only` before any execution path is reached.
    """

    tool_name: str
    server_name: str
    action_surface: str
    description: str
    required_args: tuple[str, ...] = ()
    optional_args: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    declared_action: str = ""  # action name passed to approval_gates

    def __post_init__(self) -> None:
        if "." not in self.tool_name:
            raise ToolRegistryError(
                f"ToolSpec.tool_name must be namespaced (e.g. 'asana.get_task'); "
                f"got {self.tool_name!r}."
            )
        if self.action_surface not in VALID_SURFACES:
            raise ToolRegistryError(
                f"ToolSpec {self.tool_name!r} has unknown action_surface "
                f"{self.action_surface!r}; valid: {sorted(VALID_SURFACES)}."
            )


@dataclass(frozen=True)
class ToolCallRequest:
    """Inbound request to `execute_tool()`.

    Held as a dataclass so the supervisor's `tool_calls` plan rows can be
    deserialized directly into a request without ad-hoc dict shuffling.
    """

    agent: str
    tool_name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    session_id: str | None = None
    step_id: int | None = None


@dataclass(frozen=True)
class ToolCallResult:
    """The outcome of one `execute_tool()` invocation.

    Always carries a signed message (Atlas for system-level blockers,
    the acting agent for executed-or-attempted calls). `result_summary` is
    a redacted preview of the result body suitable for inclusion in a
    subagent's downstream context. The raw result body is not held here.
    """

    tool_call_id: str
    status: str  # 'ok' | 'blocked' | 'errored' | 'pending-human-approval'
    action_surface: str
    signed_message: str
    result_summary: str
    redacted_args: str
    agent: str
    tool_name: str
    server_name: str
    started_at: str
    completed_at: str
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


# --- Constants ---------------------------------------------------------------


#: Tool-call status enum values mirroring the persistence schema.
TOOL_STATUS_OK = "ok"
TOOL_STATUS_BLOCKED = "blocked"
TOOL_STATUS_ERRORED = "errored"
TOOL_STATUS_PENDING_HUMAN_APPROVAL = "pending-human-approval"
VALID_TOOL_STATUSES: frozenset[str] = frozenset(
    {
        TOOL_STATUS_OK,
        TOOL_STATUS_BLOCKED,
        TOOL_STATUS_ERRORED,
        TOOL_STATUS_PENDING_HUMAN_APPROVAL,
    }
)

#: GitHub org override — the existing `runtime/mcp/github.json` carries a
#: legacy `default_org=Clarity-Apps`; the directive explicitly corrects it to
#: `ClarityOps-Apps`. The registry rewrites the resolved value at load time;
#: the on-disk config still reads as the original string, which is fine: this
#: layer is the single source of truth that the supervisor consumes.
GITHUB_ORG_CORRECTION: tuple[str, str] = ("Clarity-Apps", "ClarityOps-Apps")

#: Default GitHub repo when GITHUB_REPO env is unset.
GITHUB_REPO_DEFAULT = "agent-orchestra"

#: Default Asana workspace gid lookup env var (set in .env on local/VPS).
ASANA_WORKSPACE_ENV = "ASANA_WORKSPACE_GID"

#: Bash smoke allowlist. Each entry is a substring; a command qualifies iff
#: its tokenized form starts with one of these prefixes. Long-form module
#: invocations (`python -m foo --self-test`) are the operator-visible shape
#: Scout is expected to drive in M4.
BASH_SMOKE_ALLOWLIST: tuple[str, ...] = (
    "python -m supervisor --dry-run",
    "uv run python -m supervisor --dry-run",
    "python -m session_store --self-test",
    "uv run python -m session_store --self-test",
    "python orchestra.py --self-test",
    "python -m llm.message_protocol --dry-run",
    "uv run python -m llm.message_protocol --dry-run",
    "python -m llm.agent_factory --dry-run",
    "uv run python -m llm.agent_factory --dry-run",
    "python -m llm.agent_factory --list-agents",
    "uv run python -m llm.agent_factory --list-agents",
    "python -m tool_registry --dry-run",
    "uv run python -m tool_registry --dry-run",
)

#: Substrings that, when present anywhere in a bash command, force-escalate
#: classification to HUMAN_APPROVED_ONLY so the call cannot bypass the gate
#: even if it nominally appears on the allowlist. The hooks layer's keyword
#: set covers the same shapes for approval_gates.classify_surface(); we
#: re-check here so the registry can name the destructive token in the
#: signed blocker.
BASH_DESTRUCTIVE_PATTERNS: tuple[str, ...] = (
    "rm -rf",
    "rm -fr",
    "drop table",
    "drop database",
    "truncate ",
    "--force",
    "force-push",
    "force push",
    "git push --force",
    "git reset --hard",
    "git clean -f",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "shutdown ",
    "reboot",
)

#: Per-call bash timeout (seconds). Short enough that a wedged smoke does
#: not hang the supervisor; long enough that the standard regression block
#: runs comfortably.
BASH_TIMEOUT_SECONDS = 120

#: Filesystem deny patterns — anchored to the ORCHESTRA_ROOT-resolved path.
FILESYSTEM_DENY_SUBSTRINGS: tuple[str, ...] = (
    "/.env",
    "/.venv/",
    "/memory/sessions.db",
    "/memory/audit/",
)

#: Filesystem deny extensions — DB / WAL / SHM siblings of sessions.db.
FILESYSTEM_DENY_EXTENSIONS: tuple[str, ...] = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".db-journal",
)

#: HTTP timeout for REST adapter calls (seconds).
HTTP_TIMEOUT_SECONDS = 20

#: User-Agent for REST adapter calls. Identifies the runtime to upstream
#: rate-limit dashboards without leaking host info.
HTTP_USER_AGENT = "agent-orchestra/runtime (M4 4.7)"


# --- Tool registry -----------------------------------------------------------


TOOL_REGISTRY: dict[str, ToolSpec] = {
    # --- Asana ----------------------------------------------------------------
    "asana.get_task": ToolSpec(
        tool_name="asana.get_task",
        server_name="asana",
        action_surface=SAFE,
        description="Read one Asana task by GID.",
        required_args=("task_id",),
        optional_args=("opt_fields",),
        required_env=("ASANA_ACCESS_TOKEN",),
    ),
    "asana.search_tasks": ToolSpec(
        tool_name="asana.search_tasks",
        server_name="asana",
        action_surface=SAFE,
        description="Search Asana tasks within a workspace.",
        required_args=("text",),
        optional_args=("workspace_gid", "limit"),
        required_env=("ASANA_ACCESS_TOKEN",),
    ),
    "asana.add_comment": ToolSpec(
        tool_name="asana.add_comment",
        server_name="asana",
        action_surface=GUARDED,
        description="Post a plain-text comment on an Asana task.",
        required_args=("task_id", "text"),
        required_env=("ASANA_ACCESS_TOKEN",),
        declared_action="asana_add_comment",
    ),
    "asana.update_task_status": ToolSpec(
        tool_name="asana.update_task_status",
        server_name="asana",
        action_surface=GUARDED,
        description="Toggle the `completed` field on an Asana task.",
        required_args=("task_id", "completed"),
        required_env=("ASANA_ACCESS_TOKEN",),
        declared_action="asana_update_task_status",
    ),
    # --- GitHub ---------------------------------------------------------------
    "github.get_repo": ToolSpec(
        tool_name="github.get_repo",
        server_name="github",
        action_surface=SAFE,
        description="Read repo metadata.",
        required_args=("owner", "repo"),
        required_env=("GITHUB_TOKEN",),
    ),
    "github.get_file": ToolSpec(
        tool_name="github.get_file",
        server_name="github",
        action_surface=SAFE,
        description="Read a file's metadata + base64 content from a repo.",
        required_args=("owner", "repo", "path"),
        optional_args=("ref",),
        required_env=("GITHUB_TOKEN",),
    ),
    "github.list_prs": ToolSpec(
        tool_name="github.list_prs",
        server_name="github",
        action_surface=SAFE,
        description="List pull requests on a repo.",
        required_args=("owner", "repo"),
        optional_args=("state",),
        required_env=("GITHUB_TOKEN",),
    ),
    "github.create_branch": ToolSpec(
        tool_name="github.create_branch",
        server_name="github",
        action_surface=GUARDED,
        description="Create a new branch ref from an existing ref.",
        required_args=("owner", "repo", "branch", "from_sha"),
        required_env=("GITHUB_TOKEN",),
        declared_action="github_create_branch",
    ),
    "github.open_pr": ToolSpec(
        tool_name="github.open_pr",
        server_name="github",
        action_surface=GUARDED,
        description="Open a pull request.",
        required_args=("owner", "repo", "head", "base", "title"),
        optional_args=("body",),
        required_env=("GITHUB_TOKEN",),
        declared_action="github_open_pr",
    ),
    "github.push_branch": ToolSpec(
        tool_name="github.push_branch",
        server_name="github",
        action_surface=HUMAN_APPROVED_ONLY,
        description="Push a branch to origin. Human-approved-only — never "
        "auto-executes from the registry; the gate halts the supervisor "
        "with a pending approval blocker.",
        required_args=("branch",),
        declared_action="force_push_main",
    ),
    "github.merge_pr": ToolSpec(
        tool_name="github.merge_pr",
        server_name="github",
        action_surface=HUMAN_APPROVED_ONLY,
        description="Merge a PR to main. Human-approved-only.",
        required_args=("owner", "repo", "pr_number"),
        required_env=("GITHUB_TOKEN",),
        declared_action="merge_to_main",
    ),
    # --- Filesystem -----------------------------------------------------------
    "filesystem.read_file": ToolSpec(
        tool_name="filesystem.read_file",
        server_name="filesystem",
        action_surface=SAFE,
        description="Read a UTF-8 file under ORCHESTRA_ROOT.",
        required_args=("path",),
    ),
    "filesystem.list_dir": ToolSpec(
        tool_name="filesystem.list_dir",
        server_name="filesystem",
        action_surface=SAFE,
        description="List entries in a directory under ORCHESTRA_ROOT.",
        required_args=("path",),
    ),
    "filesystem.write_file": ToolSpec(
        tool_name="filesystem.write_file",
        server_name="filesystem",
        action_surface=GUARDED,
        description="Write UTF-8 content to a file under ORCHESTRA_ROOT. "
        "Deny-listed paths (.env, .venv, memory DB files, audit logs) and "
        "path traversal outside the runtime root are blocked before any "
        "filesystem write happens.",
        required_args=("path", "content"),
        declared_action="filesystem_write_file",
    ),
    # --- Bash (Scout smoke) ---------------------------------------------------
    "bash.run_smoke": ToolSpec(
        tool_name="bash.run_smoke",
        server_name="bash",
        action_surface=GUARDED,
        description="Run an allowlisted smoke command. Destructive shell "
        "patterns auto-escalate to human-approved-only and are blocked at "
        "the gate before any subprocess spawn.",
        required_args=("command",),
        optional_args=("timeout",),
        declared_action="bash_run_smoke",
    ),
}


#: Per-agent allowed tools matrix. Exact set per the 4.7 directive.
AGENT_TOOL_MATRIX: dict[str, frozenset[str]] = {
    "Atlas": frozenset(
        {
            "asana.get_task",
            "asana.search_tasks",
            "github.get_repo",
            "github.get_file",
            "github.list_prs",
        }
    ),
    "Cody": frozenset(
        {
            "asana.get_task",
            "asana.search_tasks",
            "asana.add_comment",
            "asana.update_task_status",
            "github.get_repo",
            "github.get_file",
            "github.list_prs",
            "github.create_branch",
            "github.open_pr",
            "github.push_branch",
            "github.merge_pr",
            "filesystem.read_file",
            "filesystem.list_dir",
            "filesystem.write_file",
        }
    ),
    "Scribe": frozenset(
        {
            "asana.get_task",
            "asana.search_tasks",
            "asana.add_comment",
            "asana.update_task_status",
            "filesystem.read_file",
            "filesystem.list_dir",
            "filesystem.write_file",
        }
    ),
    "Scout": frozenset(
        {
            "asana.get_task",
            "asana.search_tasks",
            "asana.add_comment",
            "asana.update_task_status",
            "filesystem.read_file",
            "filesystem.list_dir",
            "filesystem.write_file",
            "bash.run_smoke",
        }
    ),
}


# --- Read accessors ----------------------------------------------------------


def list_tools() -> tuple[ToolSpec, ...]:
    """Return all registered tools in stable declaration order."""
    return tuple(TOOL_REGISTRY.values())


def get_tool(tool_name: str) -> ToolSpec:
    """Return a `ToolSpec`. Raises `UnknownToolError` for unregistered names."""
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        raise UnknownToolError(
            f"Unknown tool {tool_name!r}. Registered: {sorted(TOOL_REGISTRY)}."
        )
    return spec


def allowed_tools_for(agent_name: str) -> tuple[str, ...]:
    """Return the alphabetized list of tool names the agent may use.

    Unknown agent names raise `UnknownAgentError` via the factory canonicalizer
    — the registry refuses to silently allow or deny calls for an unknown
    agent identity.
    """
    canonical = _canonicalize(agent_name)
    return tuple(sorted(AGENT_TOOL_MATRIX.get(canonical, frozenset())))


def is_agent_allowed(agent_name: str, tool_name: str) -> bool:
    """Return True iff the agent is in the matrix for the tool."""
    canonical = _canonicalize(agent_name)
    return tool_name in AGENT_TOOL_MATRIX.get(canonical, frozenset())


# --- MCP config loading ------------------------------------------------------


@dataclass(frozen=True)
class McpServerConfig:
    """Loaded view of one `runtime/mcp/{server_name}.json` file.

    Secret-bearing env values are NEVER held here — `resolved_env` maps to
    booleans (`True` when the env var has a non-empty value) so a downstream
    inspector can see "ASANA_ACCESS_TOKEN is set" without seeing the token.
    `missing_env` lists required env vars that are absent.
    """

    name: str
    enabled: bool
    transport: str  # 'sse' | 'stdio' | '' for placeholders
    agents: tuple[str, ...]
    notes: str
    resolved_env: Mapping[str, bool] = field(default_factory=dict)
    missing_env: tuple[str, ...] = ()
    scope: Mapping[str, Any] = field(default_factory=dict)
    extras: Mapping[str, Any] = field(default_factory=dict)


_MCP_CONFIG_DIR = Path(__file__).resolve().parent / "mcp"

_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _resolve_env_in_string(
    value: str,
    *,
    env: Mapping[str, str] | None = None,
    redact_value: bool = True,
) -> tuple[str, tuple[str, bool], ...]:
    """Replace `${VAR}` tokens in a string without echoing secret values.

    Returns `(redacted_or_resolved, ((var, present), ...))`. The caller
    decides whether to keep the resolved (potentially secret) text or the
    redacted form. When `redact_value=True` (the default), the function
    replaces every `${VAR}` with `[ENV:{VAR}={present|missing}]` so a log
    line is safe to persist verbatim.
    """
    env = env if env is not None else os.environ
    presences: list[tuple[str, bool]] = []
    out_parts: list[str] = []
    last = 0
    for match in _ENV_PLACEHOLDER_RE.finditer(value):
        out_parts.append(value[last : match.start()])
        var = match.group(1)
        resolved = env.get(var)
        present = bool(resolved)
        presences.append((var, present))
        if redact_value:
            out_parts.append(f"[ENV:{var}={'present' if present else 'missing'}]")
        else:
            out_parts.append(resolved or "")
        last = match.end()
    out_parts.append(value[last:])
    return "".join(out_parts), tuple(presences)


def _required_env_for_config(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Collect every `${VAR}` referenced anywhere in a config blob.

    Walks the JSON tree, finds placeholders inside string leaves only.
    Returns a stable, alphabetized tuple.
    """
    found: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            for match in _ENV_PLACEHOLDER_RE.finditer(node):
                found.add(match.group(1))
        elif isinstance(node, Mapping):
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(raw)
    return tuple(sorted(found))


def _apply_github_org_correction(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Rewrite the legacy `Clarity-Apps` default_org to the correct one."""
    adjusted = dict(scope)
    legacy, corrected = GITHUB_ORG_CORRECTION
    default_org = adjusted.get("default_org")
    if isinstance(default_org, str) and default_org == legacy:
        adjusted["default_org"] = corrected
    return adjusted


def load_mcp_configs(
    config_dir: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, McpServerConfig]:
    """Load every `runtime/mcp/*.json` config into a typed view.

    Fails closed on:
      * missing config directory,
      * any JSON file that is not a dict,
      * any file whose `name` does not match its filename stem,
      * any file with an explicit unknown `transport`.

    Disabled servers (e.g. supabase.json with `enabled=false`) load into the
    map but the registry refuses to execute any of their tools — the failure
    is surfaced at the `execute_tool()` boundary, not at load time, so a
    config audit can still inspect every declared server.
    """
    base = Path(config_dir) if config_dir else _MCP_CONFIG_DIR
    if not base.exists() or not base.is_dir():
        raise McpConfigError(f"MCP config dir missing or not a directory: {base}")
    env_map = env if env is not None else os.environ

    out: dict[str, McpServerConfig] = {}
    for json_path in sorted(base.glob("*.json")):
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise McpConfigError(
                f"MCP config {json_path.name}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise McpConfigError(
                f"MCP config {json_path.name}: top level must be an object"
            )
        stem = json_path.stem
        declared_name = raw.get("name", stem)
        if not isinstance(declared_name, str) or declared_name != stem:
            raise McpConfigError(
                f"MCP config {json_path.name}: `name` ({declared_name!r}) "
                f"must match filename stem ({stem!r})"
            )
        transport = raw.get("transport", "")
        if transport and transport not in {"sse", "stdio"}:
            raise McpConfigError(
                f"MCP config {json_path.name}: unknown transport {transport!r}"
            )
        agents_raw = raw.get("agents", [])
        if not isinstance(agents_raw, list) or not all(
            isinstance(a, str) for a in agents_raw
        ):
            agents: tuple[str, ...] = ()
        else:
            agents = tuple(agents_raw)

        required_env = _required_env_for_config(raw)
        resolved = {var: bool(env_map.get(var)) for var in required_env}
        missing = tuple(v for v in required_env if not resolved.get(v))

        scope_raw = raw.get("scope")
        if isinstance(scope_raw, dict):
            scope: Mapping[str, Any] = (
                _apply_github_org_correction(scope_raw)
                if stem == "github"
                else dict(scope_raw)
            )
        else:
            scope = {}

        extras = {
            k: v
            for k, v in raw.items()
            if k not in {"name", "enabled", "transport", "agents", "notes", "scope"}
        }

        out[stem] = McpServerConfig(
            name=stem,
            enabled=bool(raw.get("enabled", False)),
            transport=transport or "",
            agents=agents,
            notes=str(raw.get("notes", "")),
            resolved_env=resolved,
            missing_env=missing,
            scope=scope,
            extras=extras,
        )
    return out


# --- ORCHESTRA_ROOT + filesystem path checking -------------------------------


def orchestra_root() -> Path:
    """Resolve the filesystem root for filesystem.* tools.

    Precedence:
      1. `ORCHESTRA_ROOT` env var (resolved to absolute).
      2. The directory containing this file (`runtime/`).

    Returning a `Path` that resolves symlinks lets the path-traversal check
    compare resolved candidates against the resolved root.
    """
    env_val = os.environ.get("ORCHESTRA_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return Path(__file__).resolve().parent


def _filesystem_path_allowed(raw_path: str) -> tuple[bool, str, Path | None]:
    """Decide whether a filesystem tool can touch `raw_path`.

    Returns `(allowed, reason, resolved_or_none)`:
      * `allowed=True, reason="ok"` — path resolves inside ORCHESTRA_ROOT and
        is not deny-listed.
      * `allowed=False` — path traversal, deny pattern, or denied extension.

    The resolved path is returned on success so callers don't re-resolve it.
    """
    if not isinstance(raw_path, str) or not raw_path:
        return False, "path must be a non-empty string", None
    root = orchestra_root()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        # `resolve(strict=False)` lets us check would-be paths for write_file
        # too — the parent directory must exist within root, but the file
        # itself need not pre-exist.
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        return False, f"could not resolve path: {exc}", None
    try:
        resolved.relative_to(root)
    except ValueError:
        return (
            False,
            f"path traversal outside ORCHESTRA_ROOT ({root}); refused: {raw_path}",
            None,
        )
    posix = str(resolved).replace("\\", "/")
    for substring in FILESYSTEM_DENY_SUBSTRINGS:
        if substring in posix:
            return (
                False,
                f"path matches filesystem deny pattern {substring!r}",
                None,
            )
    for ext in FILESYSTEM_DENY_EXTENSIONS:
        if posix.endswith(ext):
            return False, f"path uses denied extension {ext!r}", None
    return True, "ok", resolved


# --- Bash classification -----------------------------------------------------


def _bash_classification(command: str) -> tuple[str, str | None]:
    """Return `(classification, matched_pattern_or_none)`.

    classification ∈ {
      "allowlisted_smoke",   # safe-to-run after gate
      "destructive_blocked", # contains a destructive token
      "not_allowlisted",     # neither allowlisted nor destructive
    }
    """
    if not isinstance(command, str) or not command.strip():
        return "not_allowlisted", None
    lowered = command.strip().lower()
    for pattern in BASH_DESTRUCTIVE_PATTERNS:
        if pattern.lower() in lowered:
            return "destructive_blocked", pattern
    for prefix in BASH_SMOKE_ALLOWLIST:
        if lowered.startswith(prefix.lower()):
            return "allowlisted_smoke", prefix
    return "not_allowlisted", None


# --- Executors (live adapters) ----------------------------------------------


class ToolExecutionError(RuntimeError):
    """Raised by an executor when a live call fails in a structured way."""


def _http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: Any = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any] | str]:
    """Minimal urllib JSON request wrapper.

    Returns `(status_code, parsed_or_text)`. Failures raise
    `ToolExecutionError` so callers can sign a single error line.
    """
    data = None
    final_headers = {
        "Accept": "application/json",
        "User-Agent": HTTP_USER_AGENT,
    }
    if headers:
        final_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=final_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 4xx/5xx — read the body so the signed error names the upstream code.
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise ToolExecutionError(
            f"HTTP {exc.code} from {method} {url}: {body_text[:200]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ToolExecutionError(
            f"network error from {method} {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise ToolExecutionError(
            f"timeout after {timeout}s on {method} {url}"
        ) from exc
    if not payload:
        return status, ""
    try:
        return status, json.loads(payload)
    except json.JSONDecodeError:
        return status, payload


def _asana_headers() -> dict[str, str]:
    token = os.environ.get("ASANA_ACCESS_TOKEN")
    if not token:
        raise ToolExecutionError("ASANA_ACCESS_TOKEN env var is missing")
    return {"Authorization": f"Bearer {token}"}


def _github_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get(
        "GITHUB_PERSONAL_ACCESS_TOKEN"
    )
    if not token:
        raise ToolExecutionError(
            "GITHUB_TOKEN (or GITHUB_PERSONAL_ACCESS_TOKEN) env var is missing"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _exec_asana_get_task(args: Mapping[str, Any]) -> dict[str, Any]:
    task_id = args["task_id"]
    opt = args.get("opt_fields")
    url = f"https://app.asana.com/api/1.0/tasks/{urllib.parse.quote(str(task_id))}"
    if opt:
        url = f"{url}?opt_fields={urllib.parse.quote(str(opt))}"
    status, body = _http_request("GET", url, headers=_asana_headers())
    return {"status": status, "body": body}


def _exec_asana_search_tasks(args: Mapping[str, Any]) -> dict[str, Any]:
    text = args["text"]
    workspace = args.get("workspace_gid") or os.environ.get(ASANA_WORKSPACE_ENV)
    if not workspace:
        raise ToolExecutionError(
            f"workspace_gid arg or {ASANA_WORKSPACE_ENV} env required"
        )
    limit = int(args.get("limit", 20))
    qs = urllib.parse.urlencode({"text": text, "limit": limit})
    url = (
        f"https://app.asana.com/api/1.0/workspaces/"
        f"{urllib.parse.quote(str(workspace))}/tasks/search?{qs}"
    )
    status, body = _http_request("GET", url, headers=_asana_headers())
    return {"status": status, "body": body}


def _exec_asana_add_comment(args: Mapping[str, Any]) -> dict[str, Any]:
    task_id = args["task_id"]
    text = args["text"]
    url = (
        f"https://app.asana.com/api/1.0/tasks/"
        f"{urllib.parse.quote(str(task_id))}/stories"
    )
    payload = {"data": {"text": text}}
    status, body = _http_request("POST", url, headers=_asana_headers(), body=payload)
    return {"status": status, "body": body}


def _exec_asana_update_task_status(args: Mapping[str, Any]) -> dict[str, Any]:
    task_id = args["task_id"]
    completed = bool(args["completed"])
    url = f"https://app.asana.com/api/1.0/tasks/{urllib.parse.quote(str(task_id))}"
    payload = {"data": {"completed": completed}}
    status, body = _http_request("PUT", url, headers=_asana_headers(), body=payload)
    return {"status": status, "body": body}


def _exec_github_get_repo(args: Mapping[str, Any]) -> dict[str, Any]:
    owner = args["owner"]
    repo = args["repo"]
    url = f"https://api.github.com/repos/{owner}/{repo}"
    status, body = _http_request("GET", url, headers=_github_headers())
    return {"status": status, "body": body}


def _exec_github_get_file(args: Mapping[str, Any]) -> dict[str, Any]:
    owner = args["owner"]
    repo = args["repo"]
    path = args["path"]
    ref = args.get("ref")
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{urllib.parse.quote(path)}"
    )
    if ref:
        url = f"{url}?ref={urllib.parse.quote(str(ref))}"
    status, body = _http_request("GET", url, headers=_github_headers())
    return {"status": status, "body": body}


def _exec_github_list_prs(args: Mapping[str, Any]) -> dict[str, Any]:
    owner = args["owner"]
    repo = args["repo"]
    state = args.get("state", "open")
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state={state}"
    status, body = _http_request("GET", url, headers=_github_headers())
    return {"status": status, "body": body}


def _exec_github_create_branch(args: Mapping[str, Any]) -> dict[str, Any]:
    owner = args["owner"]
    repo = args["repo"]
    branch = args["branch"]
    from_sha = args["from_sha"]
    url = f"https://api.github.com/repos/{owner}/{repo}/git/refs"
    payload = {"ref": f"refs/heads/{branch}", "sha": from_sha}
    status, body = _http_request(
        "POST", url, headers=_github_headers(), body=payload
    )
    return {"status": status, "body": body}


def _exec_github_open_pr(args: Mapping[str, Any]) -> dict[str, Any]:
    owner = args["owner"]
    repo = args["repo"]
    payload = {
        "head": args["head"],
        "base": args["base"],
        "title": args["title"],
    }
    if "body" in args:
        payload["body"] = args["body"]
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    status, body = _http_request(
        "POST", url, headers=_github_headers(), body=payload
    )
    return {"status": status, "body": body}


def _exec_filesystem_read_file(args: Mapping[str, Any]) -> dict[str, Any]:
    allowed, reason, resolved = _filesystem_path_allowed(args["path"])
    if not allowed or resolved is None:
        raise ToolExecutionError(reason)
    if not resolved.exists() or not resolved.is_file():
        raise ToolExecutionError(f"file does not exist: {args['path']}")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    return {"path": str(resolved), "bytes": len(text), "content": text}


def _exec_filesystem_list_dir(args: Mapping[str, Any]) -> dict[str, Any]:
    allowed, reason, resolved = _filesystem_path_allowed(args["path"])
    if not allowed or resolved is None:
        raise ToolExecutionError(reason)
    if not resolved.exists() or not resolved.is_dir():
        raise ToolExecutionError(f"not a directory: {args['path']}")
    entries = [
        {"name": e.name, "is_dir": e.is_dir()}
        for e in sorted(resolved.iterdir(), key=lambda p: p.name)
    ]
    return {"path": str(resolved), "entries": entries}


def _exec_filesystem_write_file(args: Mapping[str, Any]) -> dict[str, Any]:
    allowed, reason, resolved = _filesystem_path_allowed(args["path"])
    if not allowed or resolved is None:
        raise ToolExecutionError(reason)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    content = str(args["content"])
    resolved.write_text(content, encoding="utf-8")
    return {"path": str(resolved), "bytes": len(content)}


def _exec_bash_run_smoke(args: Mapping[str, Any]) -> dict[str, Any]:
    command = args["command"]
    classification, matched = _bash_classification(command)
    # Arg validation already rejected destructive and non-allowlisted commands
    # before we reach here. Re-checking is defense-in-depth: if the registry's
    # validation logic ever drifted, the executor refuses to spawn the shell.
    if classification != "allowlisted_smoke":
        raise ToolExecutionError(
            f"bash command not allowlisted (classification={classification}, "
            f"matched={matched})"
        )
    timeout = int(args.get("timeout", BASH_TIMEOUT_SECONDS))
    completed = subprocess.run(  # noqa: S603 - allowlisted command surface
        command,
        shell=True,  # noqa: S602 - allowlisted via _bash_classification
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


#: Live executor registry. Tools without an entry are treated as "live
#: wiring not in scope" — the hook layer still runs (permission, surface,
#: secrets, approval) and a signed `live_wire_deferred` blocker is returned
#: at the execute step. M4 wires the read surface plus the writes that
#: rest on a single HTTP call; multi-call git workflows (push_branch) are
#: explicitly human-approved-only and never reach the executor.
EXECUTOR_REGISTRY: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "asana.get_task": _exec_asana_get_task,
    "asana.search_tasks": _exec_asana_search_tasks,
    "asana.add_comment": _exec_asana_add_comment,
    "asana.update_task_status": _exec_asana_update_task_status,
    "github.get_repo": _exec_github_get_repo,
    "github.get_file": _exec_github_get_file,
    "github.list_prs": _exec_github_list_prs,
    "github.create_branch": _exec_github_create_branch,
    "github.open_pr": _exec_github_open_pr,
    "filesystem.read_file": _exec_filesystem_read_file,
    "filesystem.list_dir": _exec_filesystem_list_dir,
    "filesystem.write_file": _exec_filesystem_write_file,
    "bash.run_smoke": _exec_bash_run_smoke,
}


# --- Arg-validation hooks ----------------------------------------------------


def _validate_required_args(spec: ToolSpec, args: Mapping[str, Any]) -> None:
    missing = [k for k in spec.required_args if k not in args]
    if missing:
        raise ToolArgError(
            f"tool {spec.tool_name!r} missing required args: {missing}"
        )


def _validate_tool_args(spec: ToolSpec, args: Mapping[str, Any]) -> None:
    """Pre-execution arg checks beyond presence.

    For filesystem.* tools: ensures the path is rooted inside ORCHESTRA_ROOT
    and not deny-listed.
    For bash.run_smoke: ensures the command is on the allowlist; destructive
    tokens force-escalate to human-approved-only later in the hook chain
    via `classify_surface()` but are also rejected here so the signed
    blocker can name the matched pattern.
    """
    _validate_required_args(spec, args)
    if spec.tool_name.startswith("filesystem."):
        allowed, reason, _ = _filesystem_path_allowed(args["path"])
        if not allowed:
            raise ToolArgError(
                f"filesystem path refused for {spec.tool_name}: {reason}"
            )
    if spec.tool_name == "bash.run_smoke":
        classification, matched = _bash_classification(args["command"])
        if classification == "destructive_blocked":
            raise ToolArgError(
                f"bash command refused before execution: destructive token "
                f"{matched!r} matched"
            )
        if classification == "not_allowlisted":
            raise ToolArgError(
                "bash command refused before execution: not on the smoke "
                "allowlist"
            )


# --- Approval-gate action resolution ----------------------------------------


def _resolve_gate_action(spec: ToolSpec, args: Mapping[str, Any]) -> str:
    """Pick the action string passed to `approval_gates.check_approval_gate`.

    A `declared_action` on the spec lets `approval_gates.HUMAN_APPROVED_ACTIONS`
    catch known sensitive verbs (merge_to_main, force_push_main, etc.) and
    escalate the surface. For bash, we suffix the command's first token so the
    audit log shows what was attempted without persisting the full command.
    """
    base = spec.declared_action or spec.tool_name
    if spec.tool_name == "bash.run_smoke":
        command = str(args.get("command", "")).strip()
        first_token = command.split()[0] if command else ""
        return f"{base}:{first_token}" if first_token else base
    return base


# --- Redaction helpers -------------------------------------------------------


def _redact_args(args: Mapping[str, Any]) -> str:
    """Serialize args to JSON with redaction. Used in signed messages, in
    `tool_calls.args_json`, and in subagent-visible tool summaries."""
    try:
        payload = json.dumps(
            args, sort_keys=True, ensure_ascii=False, default=str
        )
    except TypeError:
        payload = json.dumps(
            {k: str(v) for k, v in args.items()},
            sort_keys=True,
            ensure_ascii=False,
        )
    return redact(payload)


def _summarize_result(result: Any, max_chars: int = 400) -> str:
    """Format a result body for the signed message and persisted summary."""
    if result is None:
        return ""
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
        except TypeError:
            text = str(result)
    redacted = redact(text)
    if len(redacted) > max_chars:
        return redacted[:max_chars] + "...[truncated]"
    return redacted


# --- The hook chain ----------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _result(
    *,
    tool_call_id: str,
    status: str,
    surface: str,
    signed: str,
    summary: str,
    redacted_args: str,
    agent: str,
    tool_name: str,
    server_name: str,
    started_at: str,
    error: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ToolCallResult:
    if status not in VALID_TOOL_STATUSES:
        raise ToolRegistryError(
            f"internal: unknown tool status {status!r}; valid {sorted(VALID_TOOL_STATUSES)}"
        )
    return ToolCallResult(
        tool_call_id=tool_call_id,
        status=status,
        action_surface=surface,
        signed_message=signed,
        result_summary=summary,
        redacted_args=redacted_args,
        agent=agent,
        tool_name=tool_name,
        server_name=server_name,
        started_at=started_at,
        completed_at=_utc_now_iso(),
        error=error,
        metadata=dict(metadata or {}),
    )


def _persist_tool_call(
    store: Any,
    result: ToolCallResult,
    *,
    request: ToolCallRequest,
) -> None:
    """Persist a tool-call row via the SessionStore (v2 schema).

    The store reference is typed as `Any` to avoid a hard import cycle; the
    runtime sets it via the supervisor. The persistence step is no-op when
    `store` is None.
    """
    if store is None:
        return
    if request.run_id is None or request.session_id is None:
        # The supervisor always supplies these; surface anomalies as errors
        # so we don't quietly drop a tool-call row.
        raise ToolRegistryError(
            "_persist_tool_call: run_id/session_id required when store is set"
        )
    store.record_tool_call(
        tool_call_id=result.tool_call_id,
        run_id=request.run_id,
        session_id=request.session_id,
        step_id=request.step_id,
        agent=result.agent,
        tool_name=result.tool_name,
        server_name=result.server_name,
        action_surface=result.action_surface,
        status=result.status,
        args_json=result.redacted_args,
        result_summary=result.result_summary,
        signed_message=result.signed_message,
        error=result.error,
        started_at=result.started_at,
        completed_at=result.completed_at,
        metadata=result.metadata,
    )


def execute_tool(
    agent_name: str,
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    step_id: int | None = None,
    store: Any | None = None,
    dry_run: bool = False,
) -> ToolCallResult:
    """Run one tool call through the supervisor-mediated hook chain.

    Hook order — every call goes through every step in sequence:

      1. canonicalize agent via `agent_factory._canonicalize`
      2. look up the tool spec; refuse unknown tools
      3. check the agent/tool permission matrix
      4. validate args (presence, FS path, bash classification)
      5. secrets_check on the redacted args payload
      6. classify_surface() then check_approval_gate()
      7. execute (live or dry-run synthetic)
      8. sign the result/blocker line with the acting agent
      9. redact summary, persist to `tool_calls`

    Every halt path returns a `ToolCallResult` with a signed message and a
    status drawn from `VALID_TOOL_STATUSES`. The function does not raise on
    domain-level halts; it only raises `ToolRegistryError` / `UnknownAgentError`
    for *type-level* mistakes (unknown agent, unknown tool) that callers
    must surface immediately.
    """
    args = dict(args or {})
    started_at = _utc_now_iso()
    tool_call_id = str(uuid4())

    # Step 1 — agent canonicalization (raises UnknownAgentError on bad input).
    canonical_agent = _canonicalize(agent_name)

    # Step 2 — tool spec lookup.
    spec = get_tool(tool_name)

    # Step 3 — permission matrix.
    if not is_agent_allowed(canonical_agent, spec.tool_name):
        signed = sign_action(
            "Atlas",
            f"Tool blocked: agent {canonical_agent} is not authorized to use "
            f"{spec.tool_name}. Permission matrix denial.",
        )
        request = ToolCallRequest(
            agent=canonical_agent,
            tool_name=spec.tool_name,
            args=args,
            run_id=run_id,
            session_id=session_id,
            step_id=step_id,
        )
        redacted_args = _redact_args(args)
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_BLOCKED,
            surface=spec.action_surface,
            signed=signed,
            summary="permission matrix denial",
            redacted_args=redacted_args,
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            metadata={"reason": "unauthorized_agent_tool"},
        )
        _persist_tool_call(store, result, request=request)
        return result

    request = ToolCallRequest(
        agent=canonical_agent,
        tool_name=spec.tool_name,
        args=args,
        run_id=run_id,
        session_id=session_id,
        step_id=step_id,
    )
    redacted_args = _redact_args(args)

    # Step 4 — arg validation. Failure produces a signed blocker named after
    # the violated rule (FS path traversal, deny pattern, bash deny).
    try:
        _validate_tool_args(spec, args)
    except ToolArgError as exc:
        signed = sign_action(
            "Atlas",
            f"Tool blocked: {spec.tool_name} arg validation refused — {exc}",
        )
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_BLOCKED,
            surface=spec.action_surface,
            signed=signed,
            summary=str(exc),
            redacted_args=redacted_args,
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            metadata={"reason": "arg_validation"},
        )
        _persist_tool_call(store, result, request=request)
        return result

    # Step 5 — secrets check on the redacted args payload AND the result
    # summary placeholder (no result yet, so this is purely an input check).
    # The redacted form is what gets persisted; the original args are
    # inspected for secret-shaped tokens that would have leaked into a live
    # call had we proceeded.
    raw_payload = json.dumps(
        args, sort_keys=True, ensure_ascii=False, default=str
    )
    secrets_kinds = find_secrets(raw_payload)
    if secrets_kinds:
        secret_result = check_for_secrets(raw_payload, actor="Atlas")
        signed = sign_action(
            "Atlas",
            f"Tool blocked: {spec.tool_name} args contain {', '.join(secrets_kinds)}. "
            "Payload not forwarded to executor; not persisted in raw form.",
        )
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_BLOCKED,
            surface=spec.action_surface,
            signed=signed,
            summary=f"args secrets refused: {', '.join(secret_result.matches)}",
            redacted_args=redact(raw_payload),
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            metadata={
                "reason": "args_secrets_block",
                "secret_kinds": list(secret_result.matches),
            },
        )
        _persist_tool_call(store, result, request=request)
        return result

    # Step 6 — classify the action surface and run the approval gate.
    # `classify_surface()` may upgrade a guarded action (e.g. `bash:rm`)
    # to `human-approved-only` based on the action's name; the gate then
    # returns a `pending-human-approval` decision.
    action = _resolve_gate_action(spec, args)
    resolved_surface = classify_surface(action, spec.action_surface)
    gate = check_approval_gate(
        action,
        surface=resolved_surface,
        actor=canonical_agent,
        target=spec.tool_name,
        risk=f"surface={resolved_surface}",
        rollback="reversal-not-applicable" if resolved_surface == SAFE else "see-runbook",
    )

    if gate.decision == "pending-human-approval":
        signed = sign_action(
            "Atlas",
            f"Tool pending Garrett approval: {canonical_agent} requested "
            f"{spec.tool_name} (action={action}, surface={resolved_surface}). "
            "Live execution withheld until the gate resolves.",
        )
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_PENDING_HUMAN_APPROVAL,
            surface=resolved_surface,
            signed=signed,
            summary=gate.reason,
            redacted_args=redacted_args,
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            metadata={
                "reason": "human_approval_required",
                "action": action,
                "gate_decision": gate.decision,
            },
        )
        _persist_tool_call(store, result, request=request)
        return result

    if not gate.allowed:
        signed = sign_action(
            "Atlas",
            f"Tool blocked: approval gate refused {action} for "
            f"{canonical_agent}/{spec.tool_name}: {gate.reason}",
        )
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_BLOCKED,
            surface=resolved_surface,
            signed=signed,
            summary=gate.reason,
            redacted_args=redacted_args,
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            metadata={"reason": "approval_gate_denied", "action": action},
        )
        _persist_tool_call(store, result, request=request)
        return result

    # Step 7 — dry-run short-circuit. A dry-run returns a synthetic ok result
    # with a redacted-arg summary so the supervisor's dry-run plan path can
    # exercise the full hook chain end-to-end without touching live services.
    if dry_run:
        signed = sign_action(
            canonical_agent,
            f"Tool dry-run ok: {spec.tool_name} (surface={resolved_surface}). "
            f"No live execution; synthetic result returned.",
        )
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_OK,
            surface=resolved_surface,
            signed=signed,
            summary=f"dry-run synthetic ok; args={redacted_args}",
            redacted_args=redacted_args,
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            metadata={"dry_run": True, "action": action},
        )
        _persist_tool_call(store, result, request=request)
        return result

    # Step 8 — live execution. Tools without a wired executor land here
    # with a signed `live_wire_deferred` blocker so the hook chain still
    # produces an auditable row; M5 can fill in the missing executors.
    executor = EXECUTOR_REGISTRY.get(spec.tool_name)
    if executor is None:
        signed = sign_action(
            "Atlas",
            f"Tool blocked: {spec.tool_name} has no live executor wired in "
            "M4; deferred to a later milestone.",
        )
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_BLOCKED,
            surface=resolved_surface,
            signed=signed,
            summary="live_wire_deferred",
            redacted_args=redacted_args,
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            metadata={"reason": "live_wire_deferred", "action": action},
        )
        _persist_tool_call(store, result, request=request)
        return result

    try:
        raw_result = executor(args)
    except ToolExecutionError as exc:
        signed = sign_action(
            "Atlas",
            f"Tool errored: {spec.tool_name} executor failed — {exc}",
        )
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_ERRORED,
            surface=resolved_surface,
            signed=signed,
            summary=str(exc),
            redacted_args=redacted_args,
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            error=f"{type(exc).__name__}: {exc}",
            metadata={"reason": "executor_failure", "action": action},
        )
        _persist_tool_call(store, result, request=request)
        return result
    except Exception as exc:  # noqa: BLE001 - last-resort: signed error, no raw traceback
        signed = sign_action(
            "Atlas",
            f"Tool errored: {spec.tool_name} executor raised "
            f"{type(exc).__name__}: {exc}",
        )
        result = _result(
            tool_call_id=tool_call_id,
            status=TOOL_STATUS_ERRORED,
            surface=resolved_surface,
            signed=signed,
            summary=f"{type(exc).__name__}: {exc}",
            redacted_args=redacted_args,
            agent=canonical_agent,
            tool_name=spec.tool_name,
            server_name=spec.server_name,
            started_at=started_at,
            error=f"{type(exc).__name__}: {exc}",
            metadata={"reason": "executor_exception", "action": action},
        )
        _persist_tool_call(store, result, request=request)
        return result

    summary = _summarize_result(raw_result)
    signed = sign_action(
        canonical_agent,
        f"Tool ok: {spec.tool_name} (surface={resolved_surface}). "
        f"Result summary: {summary[:200]}",
    )
    result = _result(
        tool_call_id=tool_call_id,
        status=TOOL_STATUS_OK,
        surface=resolved_surface,
        signed=signed,
        summary=summary,
        redacted_args=redacted_args,
        agent=canonical_agent,
        tool_name=spec.tool_name,
        server_name=spec.server_name,
        started_at=started_at,
        metadata={"action": action},
    )
    _persist_tool_call(store, result, request=request)
    return result


# --- Dry-run CLI -------------------------------------------------------------


def _dry_run() -> int:
    """Run the registry's dry-run scenarios.

    Each scenario exercises one specific property of the hook chain; failures
    surface as `Cody`-signed FAIL lines and a non-zero exit code. The CLI
    runs with no env credentials needed — every check exercises the policy
    layer, not live providers.
    """
    load_env_file()
    failures: list[str] = []
    passes: list[str] = []

    def record(case: str, ok: bool, detail: str) -> None:
        target = passes if ok else failures
        target.append(f"{case}: {detail}")
        print(
            sign_action(
                "Cody",
                f"tool_registry dry-run {'pass' if ok else 'FAIL'} — {case}: {detail}",
            )
        )

    # 1. Registry shape: every spec must validate at module load (already
    #    enforced by ToolSpec.__post_init__); list_tools surfaces them all.
    try:
        tools = list_tools()
        record(
            "registry-shape",
            len(tools) == len(TOOL_REGISTRY) and len(tools) >= 14,
            f"{len(tools)} tools registered: {[t.tool_name for t in tools]}",
        )
    except Exception as exc:  # noqa: BLE001
        record("registry-shape", False, f"{type(exc).__name__}: {exc}")

    # 2. Permission matrix is the exact set from the directive.
    expected = {
        "Atlas": 5,
        "Cody": 14,
        "Scribe": 7,
        "Scout": 8,
    }
    for agent, count in expected.items():
        tools_for = allowed_tools_for(agent)
        record(
            f"matrix-{agent.lower()}-count",
            len(tools_for) == count,
            f"{agent}: {len(tools_for)} tools ({list(tools_for)})",
        )

    # 3. MCP config loader fails closed on missing dir.
    with tempfile.TemporaryDirectory(prefix="tool-reg-test-") as tmp:
        bogus = Path(tmp) / "does-not-exist"
        try:
            load_mcp_configs(bogus)
            record("mcp-config-missing-dir", False, "did NOT raise on missing dir")
        except McpConfigError as exc:
            record(
                "mcp-config-missing-dir",
                "missing or not a directory" in str(exc),
                f"raised: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            record(
                "mcp-config-missing-dir",
                False,
                f"unexpected {type(exc).__name__}: {exc}",
            )

    # 4. Real MCP config loads with secret-presence flags only.
    try:
        configs = load_mcp_configs()
        names = sorted(configs.keys())
        github_cfg = configs.get("github")
        github_org_ok = (
            github_cfg is not None
            and github_cfg.scope.get("default_org") == GITHUB_ORG_CORRECTION[1]
        )
        record(
            "mcp-config-loads",
            "asana" in names
            and "github" in names
            and "filesystem" in names
            and "supabase" in names
            and github_org_ok,
            f"loaded {names}, github.default_org="
            f"{github_cfg.scope.get('default_org') if github_cfg else None}",
        )
        # Supabase is the disabled fixture; confirm we surface that.
        sb = configs.get("supabase")
        record(
            "mcp-config-disabled-supabase",
            sb is not None and sb.enabled is False,
            f"supabase.enabled={sb.enabled if sb else None}",
        )
        # No raw secret values in the resolved view.
        for cfg in configs.values():
            for var, present in cfg.resolved_env.items():
                if isinstance(present, bool):
                    continue
                record(
                    "mcp-config-no-raw-secrets",
                    False,
                    f"non-bool env presence for {var}",
                )
                break
        record("mcp-config-no-raw-secrets", True, "all env presences are booleans")
    except Exception as exc:  # noqa: BLE001
        record("mcp-config-loads", False, f"{type(exc).__name__}: {exc}")

    # 5. ${ENV_VAR} placeholder resolver redacts by default.
    out, presences = _resolve_env_in_string(
        "token=${FAKE_VAR_THAT_IS_NOT_SET} and root=${ORCHESTRA_ROOT}",
        env={"ORCHESTRA_ROOT": "/tmp/orchestra"},
    )
    record(
        "env-resolver-redacts",
        "[ENV:FAKE_VAR_THAT_IS_NOT_SET=missing]" in out
        and "[ENV:ORCHESTRA_ROOT=present]" in out
        and presences == (
            ("FAKE_VAR_THAT_IS_NOT_SET", False),
            ("ORCHESTRA_ROOT", True),
        ),
        f"out={out}; presences={presences}",
    )

    # 6. Atlas read tool: allowed.
    result = execute_tool(
        "Atlas",
        "asana.get_task",
        {"task_id": "1215386977640867"},
        dry_run=True,
    )
    record(
        "atlas-read-allowed",
        result.status == TOOL_STATUS_OK
        and result.signed_message.startswith("[Atlas · "),
        f"status={result.status}, signed={result.signed_message[:80]}",
    )

    # 7. Atlas write attempt: blocked by permission matrix.
    result = execute_tool(
        "Atlas",
        "asana.add_comment",
        {"task_id": "x", "text": "y"},
        dry_run=True,
    )
    record(
        "atlas-write-blocked",
        result.status == TOOL_STATUS_BLOCKED
        and result.metadata.get("reason") == "unauthorized_agent_tool"
        and result.signed_message.startswith("[Atlas · "),
        f"status={result.status}, reason={result.metadata.get('reason')}",
    )

    # 8. Cody full surface: filesystem write allowed (guarded).
    with tempfile.TemporaryDirectory(prefix="orchestra-fs-test-") as tmp:
        os.environ["ORCHESTRA_ROOT"] = tmp
        try:
            result = execute_tool(
                "Cody",
                "filesystem.write_file",
                {"path": f"{tmp}/scratch.txt", "content": "ok"},
                dry_run=True,
            )
            record(
                "cody-fs-write-allowed",
                result.status == TOOL_STATUS_OK
                and result.action_surface == GUARDED
                and result.signed_message.startswith("[Cody · "),
                f"status={result.status}, surface={result.action_surface}",
            )
            # 9. Filesystem deny: .env path.
            result = execute_tool(
                "Cody",
                "filesystem.read_file",
                {"path": f"{tmp}/.env"},
                dry_run=True,
            )
            record(
                "cody-fs-deny-env",
                result.status == TOOL_STATUS_BLOCKED
                and "deny pattern" in result.result_summary,
                f"status={result.status}, summary={result.result_summary[:120]}",
            )
            # 10. Filesystem deny: path traversal.
            result = execute_tool(
                "Cody",
                "filesystem.read_file",
                {"path": "/etc/passwd"},
                dry_run=True,
            )
            record(
                "cody-fs-deny-traversal",
                result.status == TOOL_STATUS_BLOCKED
                and "path traversal" in result.result_summary,
                f"status={result.status}, summary={result.result_summary[:120]}",
            )
            # 11. Filesystem deny: DB extension.
            result = execute_tool(
                "Cody",
                "filesystem.read_file",
                {"path": f"{tmp}/memory/sessions.db"},
                dry_run=True,
            )
            record(
                "cody-fs-deny-db",
                result.status == TOOL_STATUS_BLOCKED
                and ("deny pattern" in result.result_summary
                     or "denied extension" in result.result_summary),
                f"status={result.status}, summary={result.result_summary[:120]}",
            )
        finally:
            os.environ.pop("ORCHESTRA_ROOT", None)

    # 12. Scribe cannot use GitHub.
    result = execute_tool(
        "Scribe",
        "github.get_repo",
        {"owner": "ClarityOps-Apps", "repo": "agent-orchestra"},
        dry_run=True,
    )
    record(
        "scribe-no-github",
        result.status == TOOL_STATUS_BLOCKED
        and result.metadata.get("reason") == "unauthorized_agent_tool",
        f"status={result.status}, reason={result.metadata.get('reason')}",
    )

    # 13. Scribe cannot run bash.
    result = execute_tool(
        "Scribe",
        "bash.run_smoke",
        {"command": "python -m supervisor --dry-run"},
        dry_run=True,
    )
    record(
        "scribe-no-bash",
        result.status == TOOL_STATUS_BLOCKED
        and result.metadata.get("reason") == "unauthorized_agent_tool",
        f"status={result.status}, reason={result.metadata.get('reason')}",
    )

    # 14. Scout bash allowlisted smoke: allowed (dry-run synthetic).
    result = execute_tool(
        "Scout",
        "bash.run_smoke",
        {"command": "python -m supervisor --dry-run"},
        dry_run=True,
    )
    record(
        "scout-bash-allowlisted",
        result.status == TOOL_STATUS_OK
        and result.action_surface == GUARDED,
        f"status={result.status}, surface={result.action_surface}",
    )

    # 15. Scout bash destructive: blocked before execution.
    result = execute_tool(
        "Scout",
        "bash.run_smoke",
        {"command": "rm -rf /tmp/anything"},
        dry_run=True,
    )
    record(
        "scout-bash-destructive-blocked",
        result.status == TOOL_STATUS_BLOCKED
        and "destructive token" in result.result_summary,
        f"status={result.status}, summary={result.result_summary[:120]}",
    )

    # 16. Scout bash not allowlisted: blocked.
    result = execute_tool(
        "Scout",
        "bash.run_smoke",
        {"command": "echo not-allowlisted"},
        dry_run=True,
    )
    record(
        "scout-bash-not-allowlisted",
        result.status == TOOL_STATUS_BLOCKED
        and "not on the smoke allowlist" in result.result_summary,
        f"status={result.status}, summary={result.result_summary[:120]}",
    )

    # 17. Secrets in args: blocked before execution and not persisted raw.
    synthetic_token = "sk-proj-AAAAAAAAAAAAAAAAAAAA"
    result = execute_tool(
        "Cody",
        "asana.add_comment",
        {"task_id": "123", "text": f"please use {synthetic_token}"},
        dry_run=True,
    )
    ok = (
        result.status == TOOL_STATUS_BLOCKED
        and result.metadata.get("reason") == "args_secrets_block"
        and synthetic_token not in result.redacted_args
        and synthetic_token not in result.signed_message
    )
    record(
        "args-secrets-block",
        ok,
        f"status={result.status}, reason={result.metadata.get('reason')}, "
        f"raw_token_in_persistence={synthetic_token in result.redacted_args}",
    )

    # 18. Human-approved-only tool (github.push_branch) creates a pending gate.
    result = execute_tool(
        "Cody",
        "github.push_branch",
        {"branch": "main"},
        dry_run=True,
    )
    record(
        "human-approved-gate-pending",
        result.status == TOOL_STATUS_PENDING_HUMAN_APPROVAL
        and result.action_surface == HUMAN_APPROVED_ONLY,
        f"status={result.status}, surface={result.action_surface}",
    )

    # 19. Unknown tool.
    try:
        execute_tool("Atlas", "bogus.thing", {}, dry_run=True)
        record("unknown-tool", False, "did NOT raise")
    except UnknownToolError as exc:
        record("unknown-tool", "Unknown tool" in str(exc), f"raised: {exc}")
    except Exception as exc:  # noqa: BLE001
        record("unknown-tool", False, f"unexpected {type(exc).__name__}: {exc}")

    # 20. Unknown agent.
    try:
        execute_tool("Mystery", "asana.get_task", {"task_id": "1"}, dry_run=True)
        record("unknown-agent", False, "did NOT raise")
    except UnknownAgentError as exc:
        record("unknown-agent", "Unknown agent" in str(exc), f"raised: {exc}")
    except Exception as exc:  # noqa: BLE001
        record("unknown-agent", False, f"unexpected {type(exc).__name__}: {exc}")

    # 21. Required args missing.
    result = execute_tool("Cody", "asana.get_task", {}, dry_run=True)
    record(
        "missing-required-args",
        result.status == TOOL_STATUS_BLOCKED
        and "missing required args" in result.result_summary,
        f"status={result.status}, summary={result.result_summary[:120]}",
    )

    # 22. Persisted tool_calls row — full round-trip through SessionStore v2.
    with tempfile.TemporaryDirectory(prefix="orchestra-tools-store-") as tmp:
        # Local import so this module stays importable without a v2 schema.
        from session_store import SessionStore  # noqa: PLC0415

        db = Path(tmp) / "sessions.db"
        store = SessionStore(db)
        store.ensure_schema()

        # Seed a session row to satisfy the FK.
        seed_run = str(uuid4())
        seed_session = str(uuid4())
        store.record_session(
            run_id=seed_run,
            session_id=seed_session,
            directive_summary="tool_registry self-test seed",
            status="executing",
            max_steps=3,
            dry_run=True,
            created_at=datetime.now(UTC),
        )

        result = execute_tool(
            "Cody",
            "asana.get_task",
            {"task_id": "1215386977640867"},
            run_id=seed_run,
            session_id=seed_session,
            step_id=1,
            store=store,
            dry_run=True,
        )
        rows = store.load_tool_calls(seed_run)
        ok = (
            result.status == TOOL_STATUS_OK
            and len(rows) == 1
            and rows[0]["tool_call_id"] == result.tool_call_id
            and rows[0]["agent"] == "Cody"
            and rows[0]["tool_name"] == "asana.get_task"
            and rows[0]["status"] == TOOL_STATUS_OK
        )
        record(
            "persisted-tool-call-row",
            ok,
            f"row tool_call_id={rows[0]['tool_call_id'] if rows else None}",
        )

    for case in passes:
        pass
    for case in failures:
        pass

    return 0 if not failures else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tool_registry",
        description="Tool registry + execute_tool() hook chain for M4 4.7.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the registry's policy-layer scenarios. No live provider calls.",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Emit one signed line per tool with the agent matrix.",
    )
    parser.add_argument(
        "--list-matrix",
        action="store_true",
        help="Emit one signed line per agent with the allowed_tools list.",
    )
    return parser


def _list_tools_cli() -> int:
    for spec in list_tools():
        agents = sorted(
            a for a, tools in AGENT_TOOL_MATRIX.items() if spec.tool_name in tools
        )
        print(
            sign_action(
                "Cody",
                f"tool: {spec.tool_name} server={spec.server_name} "
                f"surface={spec.action_surface} agents={agents} "
                f"required_env={list(spec.required_env)}",
            )
        )
    return 0


def _list_matrix_cli() -> int:
    for agent in CANONICAL_AGENT_NAMES:
        tools = allowed_tools_for(agent)
        print(
            sign_action(
                agent,
                f"allowed_tools={list(tools)}",
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        return _dry_run()
    if args.list_tools:
        return _list_tools_cli()
    if args.list_matrix:
        return _list_matrix_cli()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
