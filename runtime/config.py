"""Runtime configuration helpers — env-only, never echoes values.

Used by the LLM provider adapters (M4 task 4.1+) to load `.env` if present
and read required/optional environment variables. Missing values fail closed
with a clear error so callers can produce a signed blocker rather than a
silent default.
"""

from __future__ import annotations

import os
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = RUNTIME_ROOT / ".env"


class MissingEnvError(RuntimeError):
    """Raised when a required environment variable is not set."""


def load_env_file(path: Path | None = None, override: bool = False) -> Path | None:
    """Merge KEY=VALUE pairs from a `.env` file into `os.environ`.

    Values are never echoed. A missing file is not an error (allows
    env-only deployments such as systemd `EnvironmentFile=`).

    Pre-existing `os.environ` entries are respected only if their current
    value is non-empty. An empty pre-existing value (`KEY=`) is treated as
    "not set" so that a real value from the `.env` file can populate it.
    Without this, an `export KEY=` line in a parent shell rc would silently
    suppress the matching `.env` value — discovered while validating 4.2
    Anthropic credentials.

    Args:
        path: Path to the dotenv file. Defaults to `runtime/.env`.
        override: When True, replace pre-existing `os.environ` values
            regardless of whether they are empty.

    Returns:
        The path that was loaded, or `None` if no file was found.
    """
    target = path or DEFAULT_ENV_PATH
    if not target.exists():
        return None
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip optional surrounding quotes; do NOT log the resulting value.
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not key:
            continue
        if not override and os.environ.get(key):
            # Only skip if there's a truthy pre-existing value; an empty
            # pre-existing env var should be treated as unset.
            continue
        os.environ[key] = value
    return target


def require_env(name: str) -> str:
    """Return the value of an environment variable; raise MissingEnvError if unset or empty."""
    value = os.environ.get(name)
    if not value:
        raise MissingEnvError(f"Missing required env var: {name}")
    return value


def optional_env(name: str, default: str | None = None) -> str | None:
    """Return the value of an environment variable, or `default` if unset/empty."""
    value = os.environ.get(name)
    return value if value else default
