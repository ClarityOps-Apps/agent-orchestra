"""Anthropic provider adapter for Cody / Scribe / Scout (M4 task 4.2).

Mirrors the OpenAIProvider shape from task 4.1 (`runtime/llm/openai_provider.py`):
a small adapter that loads a locked system prompt and exposes
`send(messages, tools=None) -> AgentResponse`. Atlas continues to use the
OpenAI adapter; this module owns the three Anthropic-backed agents.

Per M4 architecture scope (Asana comment 1215386979487630): provider adapter
pattern, model names env-only, no entanglement with the supervisor loop
(task 4.5) or the structured message envelope (task 4.4).

Run from `runtime/`:

  cd runtime
  uv run python -m llm.anthropic_provider --dry-run          # validate all 3 agents
  uv run python -m llm.anthropic_provider --ping cody        # one live Cody ping
  uv run python -m llm.anthropic_provider --ping scribe      # one live Scribe ping
  uv run python -m llm.anthropic_provider --ping scout       # one live Scout ping

Required env: ANTHROPIC_API_KEY, CODY_MODEL, SCRIBE_MODEL, SCOUT_MODEL.
Tokens are never echoed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import (
    RUNTIME_ROOT,
    MissingEnvError,
    load_env_file,
    optional_env,
    require_env,
)
from hooks.identity_signing import sign_action
from llm.types import AgentResponse, Message


AGENT_PROMPTS: dict[str, Path] = {
    "Cody": RUNTIME_ROOT / "agents" / "cody" / "system_prompt.md",
    "Scribe": RUNTIME_ROOT / "agents" / "scribe" / "system_prompt.md",
    "Scout": RUNTIME_ROOT / "agents" / "scout" / "system_prompt.md",
}

AGENT_MODEL_ENV: dict[str, str] = {
    "Cody": "CODY_MODEL",
    "Scribe": "SCRIBE_MODEL",
    "Scout": "SCOUT_MODEL",
}

CANONICAL_AGENT_NAMES: tuple[str, ...] = tuple(AGENT_PROMPTS.keys())

DEFAULT_PING_MAX_TOKENS = 256


def _canonicalize(name: str) -> str:
    """Accept lowercased / mixed-case agent names and return the canonical form."""
    for canonical in CANONICAL_AGENT_NAMES:
        if name.lower() == canonical.lower():
            return canonical
    raise ValueError(
        f"Unknown agent {name!r}. Valid agents: {', '.join(CANONICAL_AGENT_NAMES)}."
    )


class AnthropicProvider:
    """Adapter for Cody / Scribe / Scout (Anthropic). Atlas uses the OpenAI adapter."""

    def __init__(
        self,
        *,
        agent_name: str,
        api_key: str | None = None,
        model: str | None = None,
        system_prompt_path: Path | None = None,
        max_tokens: int = DEFAULT_PING_MAX_TOKENS,
    ) -> None:
        load_env_file()
        canonical = _canonicalize(agent_name)
        self.agent_name = canonical
        self._api_key = api_key or require_env("ANTHROPIC_API_KEY")
        self._model = model or require_env(AGENT_MODEL_ENV[canonical])
        path = system_prompt_path or AGENT_PROMPTS[canonical]
        if not path.exists():
            raise FileNotFoundError(
                f"{canonical} system prompt missing: {path}"
            )
        self._system_prompt = path.read_text(encoding="utf-8")
        self._max_tokens = max_tokens
        # SDK import is deferred so dry-run can run on machines without the SDK.
        from anthropic import Anthropic  # type: ignore  # noqa: PLC0415

        self._client = Anthropic(api_key=self._api_key)

    @property
    def model(self) -> str:
        return self._model

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def send(
        self,
        messages: list[Message],
        *,
        tools: list | None = None,  # noqa: ARG002 - placeholder for task 4.7 tool wiring
    ) -> AgentResponse:
        """Send a messages request to Anthropic and return a validated AgentResponse.

        Anthropic's API splits the system prompt from the chat messages, so any
        `Message` with `role="system"` is dropped here (the provider owns its
        locked system prompt; callers should not be passing additional system
        messages in 4.2).
        """
        payload: list[dict[str, str]] = []
        for message in messages:
            if message.role == "system":
                # Anthropic uses a dedicated `system=` field; ignore inline system messages.
                continue
            payload.append({"role": message.role, "content": message.content})

        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=self._system_prompt,
            messages=payload,
        )

        # `response.content` is a list of typed content blocks (text, tool_use, ...).
        # For 4.2 the ping path uses text-only output; concatenate text blocks.
        content_parts: list[str] = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                content_parts.append(text)
        content = "".join(content_parts)

        usage_obj = getattr(response, "usage", None)
        usage: dict[str, object] = {}
        if usage_obj is not None:
            # Anthropic's usage is a small struct; serialise the public fields.
            for field_name in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                value = getattr(usage_obj, field_name, None)
                if value is not None:
                    usage[field_name] = value

        return AgentResponse(
            agent=self.agent_name,
            model=self._model,
            content=content,
            finish_reason=getattr(response, "stop_reason", None),
            usage=usage,
        )


def ping(agent_name: str) -> str:
    """Make a single signed ping through the Anthropic provider for the given agent.

    Returns the signed response text. The identity-signing format would be
    enforced by the hooks layer downstream; we wrap unsigned model output in a
    signed envelope so the receipt always lands in `[Name · UTC] …` shape.
    """
    canonical = _canonicalize(agent_name)
    provider = AnthropicProvider(agent_name=canonical)
    response = provider.send(
        [
            Message(
                role="user",
                content=(
                    f"{canonical}, this is the M4 task 4.2 Anthropic provider ping. "
                    "Reply with one short sentence acknowledging the ping. "
                    f"Sign your reply in the `[{canonical} · YYYY-MM-DDTHH:MMZ]` format."
                ),
            )
        ]
    )
    text = response.content.strip()
    if not text:
        return sign_action(
            canonical,
            "Anthropic provider ping returned empty content.",
        )
    if not text.startswith(f"[{canonical} · "):
        text = sign_action(canonical, text)
    return text


def dry_run() -> int:
    """Validate config + prompts + SDK importability for all three agents.

    Prints one signed line per agent summarising the validation, then a final
    line for the SDK check. Exits 0 if everything is ready for live pings,
    1 otherwise. Used by CI / local checks without credentials.
    """
    load_env_file()
    overall_ok = True

    # Shared SDK check (one import covers all three providers).
    try:
        import anthropic  # noqa: F401, PLC0415
        sdk_ok = True
    except ImportError as exc:
        sdk_ok = False
        print(sign_action("Cody", f"Dry-run blocker: anthropic SDK not importable: {exc}"))

    for canonical in CANONICAL_AGENT_NAMES:
        blockers: list[str] = []
        prompt_path = AGENT_PROMPTS[canonical]
        if not prompt_path.exists():
            blockers.append(f"system prompt missing at {prompt_path}")
        else:
            text = prompt_path.read_text(encoding="utf-8")
            if "Version: v1" not in text:
                blockers.append(
                    f"{canonical} prompt missing expected `Version: v1` marker."
                )
        for key in ("ANTHROPIC_API_KEY", AGENT_MODEL_ENV[canonical]):
            if not optional_env(key):
                blockers.append(f"Missing env `{key}` (live ping requires this).")
        if not sdk_ok:
            blockers.append("anthropic SDK not importable.")
        if blockers:
            overall_ok = False
            print(sign_action(canonical, "Dry-run blockers: " + "; ".join(blockers)))
        else:
            print(
                sign_action(
                    canonical,
                    f"Dry-run passes: prompt loads, anthropic SDK importable, "
                    f"ANTHROPIC_API_KEY and {AGENT_MODEL_ENV[canonical]} present.",
                )
            )

    return 0 if overall_ok else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm.anthropic_provider",
        description="Anthropic provider adapter for Cody / Scribe / Scout (M4 task 4.2).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config + prompts + SDK for all three agents without an API call.",
    )
    parser.add_argument(
        "--ping",
        choices=[name.lower() for name in CANONICAL_AGENT_NAMES],
        help="Make one live ping for the named agent and print the signed response.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run()
    if args.ping:
        target = _canonicalize(args.ping)
        try:
            print(ping(target))
            return 0
        except MissingEnvError as exc:
            print(sign_action(target, f"Ping blocked — {exc}"))
            return 2
        except ImportError as exc:
            print(
                sign_action(target, f"Ping blocked — anthropic SDK not installed: {exc}")
            )
            return 2
        except FileNotFoundError as exc:
            print(sign_action(target, f"Ping blocked — {exc}"))
            return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
