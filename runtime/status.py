"""Runtime health-check CLI for Agent Orchestra (M4 task 4.9).

A tiny, stdlib-only status surface that the supervisor / CLI / future
observability layer can call to confirm the runtime is alive and the
local environment is shaped correctly.

Two CLI shapes per Atlas's 4.9 spec:

  ``python -m status --json``
      Print a JSON-serializable health payload and exit 0 on healthy.

  ``python -m status --self-test``
      Print one signed Atlas PASS line and exit 0 on healthy.

Boundaries (per the 4.9 directive):
  - stdlib only except a local hook import for the signed self-test line.
  - No provider imports, no network, no secrets, no DB writes.

The autonomous demo run produced a candidate file via Cody's
``filesystem.write_file`` tool call (run_id 8064e6a4-…). Cody's
Anthropic-generated content diverged from Atlas's spec (subcommand
``health-check`` instead of ``--json`` / ``--self-test`` flags, no
``get_status()``, broken 1-space indentation that raised IndentationError
on import). This file is the spec-compliant rewrite. The runtime/tool
layer succeeded — the autonomous output's quality is the 4.9 finding to
surface, not a runtime defect.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hooks.identity_signing import sign_action


SERVICE_NAME = "agent-orchestra-runtime"
RUNTIME_ROOT = Path(__file__).resolve().parent


def _utc_now_iso() -> str:
    """Return a compact UTC ISO timestamp suitable for receipts."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_orchestra_root() -> dict[str, Any]:
    """Confirm we can resolve the runtime root."""
    return {
        "name": "runtime_root_resolvable",
        "ok": RUNTIME_ROOT.exists() and RUNTIME_ROOT.is_dir(),
        "detail": str(RUNTIME_ROOT),
    }


def _check_python_version() -> dict[str, Any]:
    """Confirm Python is recent enough for the runtime."""
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 10)
    return {
        "name": "python_version",
        "ok": ok,
        "detail": f"{platform.python_version()} (minimum 3.10)",
    }


def _check_status_importable() -> dict[str, Any]:
    """Confirm this module itself imported cleanly (always True if we ran)."""
    return {
        "name": "status_module_importable",
        "ok": True,
        "detail": "status.py imported and executing",
    }


CHECKS = (
    _check_orchestra_root,
    _check_python_version,
    _check_status_importable,
)


def get_status() -> dict[str, Any]:
    """Build the runtime health payload.

    Returns a JSON-serializable dict per Atlas's 4.9 spec with at least:
    ``service``, ``ok``, ``runtime_root``, ``python_version``,
    ``checked_at_utc``, and a ``checks`` array describing each probe.

    No network, no secrets, no DB writes — all checks are local and
    deterministic.
    """
    check_results = [check() for check in CHECKS]
    overall_ok = all(c["ok"] for c in check_results)
    return {
        "service": SERVICE_NAME,
        "ok": overall_ok,
        "runtime_root": str(RUNTIME_ROOT),
        "python_version": platform.python_version(),
        "checked_at_utc": _utc_now_iso(),
        "checks": check_results,
    }


def _run_json() -> int:
    """``--json`` path: print the payload, exit 0 on healthy."""
    payload = get_status()
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


def _run_self_test() -> int:
    """``--self-test`` path: print one signed Atlas PASS line, exit 0 on healthy.

    The signed line matches the hooks-layer signature shape used everywhere
    else in the runtime, so an operator can grep ``[Atlas · `` across logs
    to find this check alongside other lifecycle events.
    """
    payload = get_status()
    if payload["ok"]:
        check_summary = ", ".join(f"{c['name']}=ok" for c in payload["checks"])
        print(
            sign_action(
                "Atlas",
                f"status self-test PASS — service={payload['service']}, "
                f"python={payload['python_version']}, "
                f"runtime_root={payload['runtime_root']}, "
                f"checks=[{check_summary}].",
            )
        )
        return 0
    failed = [c["name"] for c in payload["checks"] if not c["ok"]]
    print(
        sign_action(
            "Atlas",
            f"status self-test FAIL — service={payload['service']}, "
            f"failed_checks={failed}.",
        )
    )
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="status",
        description=(
            "Local runtime health-check for Agent Orchestra. "
            "Stdlib-only; no network, no secrets, no DB."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the health payload as JSON and exit 0 on healthy.",
    )
    parser.add_argument(
        "--self-test",
        dest="self_test",
        action="store_true",
        help="Print one signed Atlas PASS line and exit 0 on healthy.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return _run_self_test()
    if args.json:
        return _run_json()
    # Default: emit usage and exit non-zero so a bare invocation doesn't
    # silently succeed.
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
