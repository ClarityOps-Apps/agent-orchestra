from __future__ import annotations

import argparse
import time
from pathlib import Path

from agents.cody.cody import CodyAgent
from atlas.atlas import AtlasAgent
from hooks.approval_gates import check_approval_gate
from hooks.identity_signing import SignatureError, enforce_signed, sign_action
from hooks.lifecycle import lifecycle_event
from hooks.secrets_check import check_for_secrets


RUNTIME_ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_PATH = RUNTIME_ROOT / "memory" / "activity.log"


def append_log(entry: str, log_path: Path = DEFAULT_LOG_PATH) -> None:
    enforce_signed(entry)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{entry}\n")
    print(entry, flush=True)


def run_noop(directive: str) -> list[str]:
    entries: list[str] = []
    atlas = AtlasAgent()
    cody = CodyAgent()

    entries.append(lifecycle_event("Atlas", "start"))
    entries.append(atlas.receive_directive(directive))

    approval = check_approval_gate(action="delegate_stub_task", surface="safe")
    secrets = check_for_secrets(directive)
    if not approval.allowed:
        entries.append(sign_action("Atlas", f"Blocked action: {approval.reason}"))
        return entries
    if not secrets.allowed:
        entries.append(sign_action("Atlas", f"Blocked directive: {secrets.reason}"))
        return entries

    entries.append(cody.run_stub_task("Acknowledge no-op Day 1 skeleton test."))
    entries.append(lifecycle_event("Cody", "stop"))
    entries.append(lifecycle_event("Atlas", "stop"))
    return entries


def run_self_test() -> None:
    try:
        enforce_signed("unsigned action")
    except SignatureError:
        print(sign_action("Atlas", "Identity-signing hook blocks unsigned actions."))
        return
    raise SystemExit("Identity-signing hook failed to block an unsigned action.")


def run_daemon(interval_seconds: int) -> None:
    append_log(lifecycle_event("Atlas", "start"))
    try:
        while True:
            append_log(sign_action("Atlas", "Daemon heartbeat: idle and ready."))
            time.sleep(interval_seconds)
    finally:
        append_log(lifecycle_event("Atlas", "stop"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent Orchestra runtime entry point.")
    parser.add_argument("directive", nargs="*", help="Directive to route through Atlas.")
    parser.add_argument("--daemon", action="store_true", help="Run as a long-lived service.")
    parser.add_argument("--interval", type=int, default=300, help="Daemon heartbeat interval.")
    parser.add_argument("--self-test", action="store_true", help="Run hook self-test.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return
    if args.daemon:
        run_daemon(args.interval)
        return

    directive = " ".join(args.directive).strip()
    if not directive:
        raise SystemExit('Usage: python orchestra.py "hello team"')

    for entry in run_noop(directive):
        append_log(entry)


if __name__ == "__main__":
    main()
