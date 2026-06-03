"""OpenAI provider adapter for Atlas (M4 task 4.1).

Adapter shape matches the M4 architecture scope (Asana comment
1215386979487630): a small `LLMProvider`-style class that loads a system
prompt and exposes `send(messages, tools=None) -> AgentResponse`. The
supervisor loop, agent factory, and message-passing protocol are NOT wired
here — those are 4.3, 4.4, and 4.5.

Run from `runtime/`:

  cd runtime
  python -m llm.openai_provider --dry-run   # no API call
  python -m llm.openai_provider --ping      # one real Atlas ping

Configuration is environment-only. Required: OPENAI_API_KEY, ATLAS_MODEL.
Atlas's system prompt is loaded from `runtime/atlas/system_prompt.md` and
never modified. Tokens are never echoed.
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


ATLAS_PROMPT_PATH = RUNTIME_ROOT / "atlas" / "system_prompt.md"
DEFAULT_AGENT_NAME = "Atlas"


class OpenAIProvider:
    """Adapter for Atlas (OpenAI). Other agents use the Anthropic adapter from task 4.2."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        system_prompt_path: Path = ATLAS_PROMPT_PATH,
        agent_name: str = DEFAULT_AGENT_NAME,
    ) -> None:
        load_env_file()
        self.agent_name = agent_name
        self._api_key = api_key or require_env("OPENAI_API_KEY")
        self._model = model or require_env("ATLAS_MODEL")
        if not system_prompt_path.exists():
            raise FileNotFoundError(
                f"Atlas system prompt missing: {system_prompt_path}"
            )
        self._system_prompt = system_prompt_path.read_text(encoding="utf-8")
        # SDK import is deferred so dry-run paths can work on machines that
        # have not yet installed openai. The agent factory (task 4.3) is the
        # earliest point where the SDK must be importable in production.
        from openai import OpenAI  # type: ignore  # noqa: PLC0415

        self._client = OpenAI(api_key=self._api_key)

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
        """Send a chat-completion request and return a validated AgentResponse."""
        payload: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt}
        ]
        for message in messages:
            payload.append({"role": message.role, "content": message.content})

        response = self._client.chat.completions.create(
            model=self._model,
            messages=payload,
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        usage_obj = getattr(response, "usage", None)
        usage = usage_obj.model_dump() if usage_obj is not None else {}
        return AgentResponse(
            agent=self.agent_name,
            model=self._model,
            content=content,
            finish_reason=choice.finish_reason,
            usage=usage,
        )


def ping() -> str:
    """Make a single signed Atlas ping through the OpenAI provider.

    Returns the signed response text. The identity-signing hook would block
    unsigned output downstream, so we wrap the raw provider content in a
    signed envelope if the model did not produce one itself.
    """
    provider = OpenAIProvider()
    response = provider.send(
        [
            Message(
                role="user",
                content=(
                    "Atlas, this is the M4 task 4.1 OpenAI provider ping. "
                    "Reply with one short sentence acknowledging the ping."
                ),
            )
        ]
    )
    text = response.content.strip()
    if not text:
        return sign_action(
            "Atlas",
            "OpenAI provider ping returned empty content; provider responded but the model produced no text.",
        )
    if not text.startswith("[Atlas · "):
        text = sign_action("Atlas", text)
    return text


def dry_run() -> int:
    """Validate config + prompt + SDK importability without making an API call.

    Prints a single signed Atlas line summarising the validation. Exits 0 on
    success, 1 on a blocker. Used by CI / local checks where the live ping
    credentials are not present.
    """
    load_env_file()
    blockers: list[str] = []

    if not ATLAS_PROMPT_PATH.exists():
        blockers.append(f"Atlas system prompt missing at {ATLAS_PROMPT_PATH}")
    else:
        text = ATLAS_PROMPT_PATH.read_text(encoding="utf-8")
        if "Version: v1" not in text:
            blockers.append(
                "Atlas system prompt missing expected `Version: v1` marker."
            )

    for key in ("OPENAI_API_KEY", "ATLAS_MODEL"):
        if not optional_env(key):
            blockers.append(
                f"Missing env `{key}` (live ping requires this; dry-run can still proceed)."
            )

    try:
        import openai  # noqa: F401, PLC0415
    except ImportError as exc:
        blockers.append(f"openai SDK not importable: {exc}")

    if blockers:
        print(sign_action("Atlas", "Dry-run blockers: " + "; ".join(blockers)))
        return 1
    print(
        sign_action(
            "Atlas",
            "Dry-run passes: Atlas prompt loads, openai SDK importable, OPENAI_API_KEY and ATLAS_MODEL present.",
        )
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm.openai_provider",
        description="OpenAI provider adapter for Atlas (M4 task 4.1).",
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="Make one live OpenAI call as Atlas and print the signed response.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config + prompt + SDK without making an API call.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run()
    if args.ping:
        try:
            print(ping())
            return 0
        except MissingEnvError as exc:
            print(sign_action("Atlas", f"Ping blocked — {exc}"))
            return 2
        except ImportError as exc:
            print(
                sign_action("Atlas", f"Ping blocked — openai SDK not installed: {exc}")
            )
            return 2
        except FileNotFoundError as exc:
            print(sign_action("Atlas", f"Ping blocked — {exc}"))
            return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
