"""Agent factory for Agent Orchestra (M4 task 4.3).

Narrow composition layer over the existing provider adapters:

- `create_agent(name)` returns an `Agent` bundling the constructed provider
  with declarative metadata (provider family, model env key, prompt path,
  tool placeholder, hook-policy placeholder).
- `get_spec(name)` returns just the `AgentSpec` (no env required) for
  consumers that need declarative info without constructing the provider.
- `list_specs()` enumerates all four configured agents (Atlas, Cody,
  Scribe, Scout).
- CLI: `uv run python -m llm.agent_factory --dry-run` and
  `uv run python -m llm.agent_factory --list-agents`.

Per the M4 architecture scope (Asana comment 1215386979487630, architecture
decisions 1, 2, 3, 10): provider adapter pattern, env-only model config,
the factory is the boundary where config differs by agent, fail closed on
unknown agents / missing env / missing prompts / missing SDKs.

This module does NOT implement:
- `agent.send(target, message)` — task 4.4 owns the message-passing protocol.
- supervisor orchestration — task 4.5.
- SQLite session persistence — task 4.6.
- MCP tool execution — task 4.7.
- REST API endpoints — task 4.11.

Names are canonicalized case-insensitively; unknown names raise
`UnknownAgentError`.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import (
    load_env_file,
    optional_env,
)
from hooks.identity_signing import sign_action
from llm.anthropic_provider import (
    AGENT_MODEL_ENV as ANTHROPIC_AGENT_MODEL_ENV,
)
from llm.anthropic_provider import (
    AGENT_PROMPTS as ANTHROPIC_AGENT_PROMPTS,
)
from llm.anthropic_provider import AnthropicProvider
from llm.openai_provider import ATLAS_PROMPT_PATH, OpenAIProvider


PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"

VALID_PROVIDER_FAMILIES: frozenset[str] = frozenset(
    {PROVIDER_OPENAI, PROVIDER_ANTHROPIC}
)


class UnknownAgentError(ValueError):
    """Raised when an agent name does not resolve to a configured agent."""


@dataclass(frozen=True)
class AgentSpec:
    """Declarative metadata for a configured agent. No env required to build.

    Downstream tasks consume `allowed_tools` (4.7) and `hook_policy` (4.5)
    via the factory rather than embedding agent-specific logic in their own
    code. The placeholders here are intentional and minimal.
    """

    name: str  # canonical case
    provider_family: str  # PROVIDER_OPENAI or PROVIDER_ANTHROPIC
    api_key_env: str
    model_env: str
    prompt_path: Path
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    hook_policy: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider_family not in VALID_PROVIDER_FAMILIES:
            raise ValueError(
                f"AgentSpec for {self.name!r} has unknown provider_family={self.provider_family!r}"
            )


# Single declarative source of truth: who-is-which-provider, with which env
# keys and which locked prompt. The Anthropic provider's own AGENT_PROMPTS
# / AGENT_MODEL_ENV tables are reused (not duplicated) to keep one source
# of truth across the package.
AGENT_SPECS: dict[str, AgentSpec] = {
    "Atlas": AgentSpec(
        name="Atlas",
        provider_family=PROVIDER_OPENAI,
        api_key_env="OPENAI_API_KEY",
        model_env="ATLAS_MODEL",
        prompt_path=ATLAS_PROMPT_PATH,
    ),
    "Cody": AgentSpec(
        name="Cody",
        provider_family=PROVIDER_ANTHROPIC,
        api_key_env="ANTHROPIC_API_KEY",
        model_env=ANTHROPIC_AGENT_MODEL_ENV["Cody"],
        prompt_path=ANTHROPIC_AGENT_PROMPTS["Cody"],
    ),
    "Scribe": AgentSpec(
        name="Scribe",
        provider_family=PROVIDER_ANTHROPIC,
        api_key_env="ANTHROPIC_API_KEY",
        model_env=ANTHROPIC_AGENT_MODEL_ENV["Scribe"],
        prompt_path=ANTHROPIC_AGENT_PROMPTS["Scribe"],
    ),
    "Scout": AgentSpec(
        name="Scout",
        provider_family=PROVIDER_ANTHROPIC,
        api_key_env="ANTHROPIC_API_KEY",
        model_env=ANTHROPIC_AGENT_MODEL_ENV["Scout"],
        prompt_path=ANTHROPIC_AGENT_PROMPTS["Scout"],
    ),
}

CANONICAL_AGENT_NAMES: tuple[str, ...] = tuple(AGENT_SPECS.keys())


def _canonicalize(name: str) -> str:
    """Return the canonical agent name for a case-insensitive input."""
    if not isinstance(name, str):
        raise UnknownAgentError(
            f"agent name must be a string, got {type(name).__name__}"
        )
    target = name.lower()
    for canonical in CANONICAL_AGENT_NAMES:
        if canonical.lower() == target:
            return canonical
    raise UnknownAgentError(
        f"Unknown agent {name!r}. Valid agents: {', '.join(CANONICAL_AGENT_NAMES)}."
    )


def get_spec(name: str) -> AgentSpec:
    """Return the declarative `AgentSpec` for an agent. No env required."""
    canonical = _canonicalize(name)
    return AGENT_SPECS[canonical]


def list_specs() -> list[AgentSpec]:
    """Return all configured `AgentSpec`s in canonical order."""
    return list(AGENT_SPECS.values())


@dataclass(frozen=True)
class Agent:
    """A constructed agent: declarative spec + ready-to-use provider adapter.

    The factory's job ends at construction. `send()` is task 4.4.
    """

    spec: AgentSpec
    provider: Any  # OpenAIProvider | AnthropicProvider; structural for 4.4

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def provider_family(self) -> str:
        return self.spec.provider_family

    @property
    def model(self) -> str:
        return self.provider.model

    @property
    def system_prompt(self) -> str:
        return self.provider.system_prompt

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return self.spec.allowed_tools

    @property
    def hook_policy(self) -> dict[str, Any]:
        return self.spec.hook_policy


def create_agent(name: str) -> Agent:
    """Construct an `Agent` for one of Atlas, Cody, Scribe, Scout.

    Resolves provider and prompt by name (case-insensitive). Fails closed
    with `UnknownAgentError` for unknown names. Provider construction may
    raise `MissingEnvError` or `FileNotFoundError` from the underlying
    adapter when env / prompt paths are not set; callers should turn
    those into signed blockers if they want a single-line failure mode.
    """
    spec = get_spec(name)
    if spec.provider_family == PROVIDER_OPENAI:
        provider: Any = OpenAIProvider(
            system_prompt_path=spec.prompt_path,
            agent_name=spec.name,
        )
    elif spec.provider_family == PROVIDER_ANTHROPIC:
        provider = AnthropicProvider(
            agent_name=spec.name,
            system_prompt_path=spec.prompt_path,
        )
    else:  # pragma: no cover - guarded by AgentSpec.__post_init__
        raise UnknownAgentError(
            f"Spec for {spec.name!r} has unknown provider_family={spec.provider_family!r}"
        )
    return Agent(spec=spec, provider=provider)


def _check_sdk_importability() -> dict[str, bool]:
    """Return per-family SDK availability without raising."""
    sdk_ok = {PROVIDER_OPENAI: False, PROVIDER_ANTHROPIC: False}
    try:
        import openai  # noqa: F401, PLC0415

        sdk_ok[PROVIDER_OPENAI] = True
    except ImportError:
        pass
    try:
        import anthropic  # noqa: F401, PLC0415

        sdk_ok[PROVIDER_ANTHROPIC] = True
    except ImportError:
        pass
    return sdk_ok


def dry_run() -> int:
    """Validate all four agents' specs without constructing providers.

    Per-agent checks (no API call):
      - prompt file exists and contains the `Version: v1` marker.
      - API key env present.
      - model env present.
      - provider family's SDK importable.

    Emits one signed line per agent under that agent's own signature.
    Exits 0 if all four agents are ready, 1 otherwise. Providers are NOT
    constructed here, so a missing env produces a blocker line rather
    than an exception — useful for CI / local checks without secrets.
    """
    load_env_file()
    sdk_ok = _check_sdk_importability()
    overall_ok = True

    for spec in list_specs():
        blockers: list[str] = []
        # Prompt
        if not spec.prompt_path.exists():
            blockers.append(f"prompt missing at {spec.prompt_path}")
        else:
            text = spec.prompt_path.read_text(encoding="utf-8")
            if "Version: v1" not in text:
                blockers.append("prompt missing `Version: v1` marker")
        # Env
        if not optional_env(spec.api_key_env):
            blockers.append(f"missing env `{spec.api_key_env}`")
        if not optional_env(spec.model_env):
            blockers.append(f"missing env `{spec.model_env}`")
        # SDK
        if not sdk_ok[spec.provider_family]:
            blockers.append(f"{spec.provider_family} SDK not importable")

        if blockers:
            overall_ok = False
            print(
                sign_action(
                    spec.name, "Factory dry-run blockers: " + "; ".join(blockers)
                )
            )
        else:
            print(
                sign_action(
                    spec.name,
                    f"Factory dry-run passes: provider={spec.provider_family}, "
                    f"model_env={spec.model_env}, prompt={spec.prompt_path.name} loads.",
                )
            )

    return 0 if overall_ok else 1


def list_agents() -> int:
    """Emit one signed spec line per agent. No env / SDK checks."""
    for spec in list_specs():
        tools_repr = list(spec.allowed_tools)
        policy_repr = spec.hook_policy if spec.hook_policy else {}
        print(
            sign_action(
                spec.name,
                f"Spec: provider={spec.provider_family}, "
                f"api_key_env={spec.api_key_env}, "
                f"model_env={spec.model_env}, "
                f"prompt={spec.prompt_path.name}, "
                f"allowed_tools={tools_repr}, "
                f"hook_policy={policy_repr}.",
            )
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm.agent_factory",
        description="Agent factory for Agent Orchestra (M4 task 4.3).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate all four agents' config + prompts + SDK without constructing providers.",
    )
    parser.add_argument(
        "--list-agents",
        action="store_true",
        help="Emit one signed spec line per agent (no env or SDK checks).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.list_agents:
        return list_agents()
    if args.dry_run:
        return dry_run()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
