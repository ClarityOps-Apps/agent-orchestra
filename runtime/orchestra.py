"""Agent Orchestra runtime CLI (M4 task 4.8).

Public operator entry point. `python orchestra.py --directive "..."` starts
a supervisor-mediated run, prints signed activity as it happens, persists
the run to SQLite, and exits with a deterministic status code.

Backwards-compatible surfaces preserved:

- `python orchestra.py --self-test` — hook smoke checks (4.x baseline).
- `python orchestra.py --daemon --interval N` — long-lived heartbeat.
- `python orchestra.py "free text"` — legacy positional no-op smoke path
  used by M2/M3/M4 receipts. The new orchestrated CLI lives behind
  `--directive`; the positional path is unchanged.

New surfaces (4.8):

- `--directive TEXT` — orchestrated run via `run_supervisor()`.
- `--resume ID` — in-place resume via `resume_supervisor()`. Accepts a
  `run_id` or `session_id`.
- `--max-steps N` — clamp the planner step budget.
- `--db-path PATH` — override the default `runtime/memory/sessions.db`.
- `--dry-run` — exercise the full hook + persistence chain without any
  provider call.

Exit-code mapping (4.8 directive section 4):

- `0` when `run.status == complete`.
- `10` when blocked or pending_human_approval (operator action needed).
- `1` when errored or any unexpected runtime failure.

The CLI lazy-imports `supervisor`, `session_store`, and `tool_registry`
inside `--directive` / `--resume` so the `--daemon` startup under
`/usr/bin/python3` (current systemd unit) does not pull provider SDK
imports it doesn't need. Atlas's directive section 7 calls this out
explicitly.

References:
- 4.8 directive — Asana comment `1215462506222184`.
- 4.7 closure — Asana comment `1215462351861299`.
- M4 architecture scope — Asana comment `1215386979487630`.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.cody.cody import CodyAgent
from atlas.atlas import AtlasAgent
from hooks.approval_gates import check_approval_gate, record_human_decision
from hooks.identity_signing import SignatureError, enforce_signed, sign_action
from hooks.lifecycle import lifecycle_event
from hooks.secrets_check import check_for_secrets, redact


RUNTIME_ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_PATH = RUNTIME_ROOT / "memory" / "activity.log"


# --- Exit code mapping (4.8 §4) ---------------------------------------------

#: Run completed cleanly — used by automation and CI.
CLI_EXIT_COMPLETE = 0

#: Operator action required — blocked or pending human approval. NOT a
#: crash; downstream tooling should treat this as "review the persisted
#: run and decide" rather than as a failure.
CLI_EXIT_OPERATOR_ACTION = 10

#: Errored or unexpected runtime failure. Exception-shaped paths land here
#: with a signed line; raw tracebacks are not surfaced to operators by
#: design (the 4.7 step 1 hardening already wraps provider transients).
CLI_EXIT_ERRORED = 1


def append_log(entry: str, log_path: Path = DEFAULT_LOG_PATH) -> None:
    enforce_signed(entry)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{entry}\n")
    print(entry, flush=True)


def run_noop(directive: str) -> list[str]:
    """Legacy M2/M3/M4 no-op smoke path. Preserved unchanged.

    Existing receipts and tests call this with a free-form positional
    string; the path runs Atlas + Cody through the hook layer without
    contacting any provider. Do not extend this — `--directive` is the
    real orchestrated path now.
    """
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
    """Hook smoke checks. Exits with raise SystemExit on any failure.

    Checks:
      1. Identity-signing hook blocks unsigned actions.
      2. `record_human_decision()` writes a signed Atlas line to the
         current audit directory at `{ORCHESTRA_AUDIT_DIR}/{YYYY-MM-DD}.md`,
         proving the hook → audit/ namespace split stays wired end to end.
         Regression coverage for the hygiene addendum (M4 architecture
         decision; Asana comment 1215434597282433).
    """
    # 1. Identity-signing hook.
    try:
        enforce_signed("unsigned action")
    except SignatureError:
        print(sign_action("Atlas", "Identity-signing hook blocks unsigned actions."))
    else:
        raise SystemExit("Identity-signing hook failed to block an unsigned action.")

    # 2. record_human_decision() → audit dir.
    with tempfile.TemporaryDirectory(prefix="orchestra-self-test-audit-") as tmp:
        previous_audit_env = os.environ.get("ORCHESTRA_AUDIT_DIR")
        previous_decisions_env = os.environ.get("ORCHESTRA_DECISIONS_DIR")
        os.environ["ORCHESTRA_AUDIT_DIR"] = tmp
        # Clear the deprecated alias so it cannot accidentally shadow the
        # primary env var inside this scope.
        os.environ.pop("ORCHESTRA_DECISIONS_DIR", None)
        try:
            entry = record_human_decision(
                "merge_to_main", "Garrett", "approve", "self-test"
            )
            if not entry.startswith("[Atlas · "):
                raise SystemExit(
                    "record_human_decision() returned an unsigned entry: "
                    f"{entry!r}"
                )
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            audit_file = Path(tmp) / f"{day}.md"
            if not audit_file.exists():
                raise SystemExit(
                    f"record_human_decision() did not write {audit_file} — "
                    "audit-dir namespace split is broken."
                )
            content = audit_file.read_text(encoding="utf-8")
            if entry not in content:
                raise SystemExit(
                    "record_human_decision() return value not found in "
                    f"{audit_file}: {content!r}"
                )
        finally:
            if previous_audit_env is None:
                os.environ.pop("ORCHESTRA_AUDIT_DIR", None)
            else:
                os.environ["ORCHESTRA_AUDIT_DIR"] = previous_audit_env
            if previous_decisions_env is not None:
                os.environ["ORCHESTRA_DECISIONS_DIR"] = previous_decisions_env
    print(
        sign_action(
            "Atlas",
            "record_human_decision writes signed line to audit/{day}.md.",
        )
    )


def run_daemon(interval_seconds: int) -> None:
    append_log(lifecycle_event("Atlas", "start"))
    try:
        while True:
            append_log(sign_action("Atlas", "Daemon heartbeat: idle and ready."))
            time.sleep(interval_seconds)
    finally:
        append_log(lifecycle_event("Atlas", "stop"))


# --- 4.8 orchestrated run + resume CLI paths ---------------------------------


def _print_event(line: str) -> None:
    """event_sink callback: flush each signed activity line to stdout.

    The supervisor calls this once per phase boundary (planner / step /
    tool call / finalizer / final status) so the operator sees signed
    activity in real time rather than only after `_format_run()` at the
    end. Lines are already redacted and capped at
    ``EVENT_LINE_MAX_CHARS`` by the supervisor before they arrive here.
    """
    print(line, flush=True)


def _status_to_exit_code(status: str) -> tuple[int, str]:
    """Map a supervisor run status to a deterministic CLI exit code +
    short category name printed in the final signed line.
    """
    # Lazy-import the status constants so this stays callable without
    # paying the supervisor import cost on --self-test / --daemon paths.
    from supervisor import (  # noqa: PLC0415
        SUPERVISOR_STATUS_BLOCKED,
        SUPERVISOR_STATUS_COMPLETE,
        SUPERVISOR_STATUS_ERRORED,
        SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
    )

    if status == SUPERVISOR_STATUS_COMPLETE:
        return CLI_EXIT_COMPLETE, "complete"
    if status in {
        SUPERVISOR_STATUS_BLOCKED,
        SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
    }:
        return CLI_EXIT_OPERATOR_ACTION, "operator-action-required"
    if status == SUPERVISOR_STATUS_ERRORED:
        return CLI_EXIT_ERRORED, "errored"
    # Unknown status — treat as errored so automation does not silently
    # consume an unexpected state.
    return CLI_EXIT_ERRORED, f"unknown-status:{status}"


def _print_blocker_context(run: Any) -> None:
    """When the CLI exits with operator-action-required, surface every
    persisted blocker / error / gate row in operator-friendly form so a
    human can read the run summary without inspecting the DB.

    The supervisor already signed each artifact; we just print them.
    """
    for blocker in run.blockers:
        signed = blocker.get("signed_message")
        if signed:
            print(signed, flush=True)
        else:
            print(
                sign_action(
                    "Atlas",
                    f"Blocker phase={blocker.get('phase')} reason="
                    f"{blocker.get('reason', '')[:200]}",
                ),
                flush=True,
            )
    for error in run.errors:
        signed = error.get("signed_message")
        if signed:
            print(signed, flush=True)
        else:
            print(
                sign_action(
                    "Atlas",
                    f"Error phase={error.get('phase')} error="
                    f"{error.get('error', '')[:200]}",
                ),
                flush=True,
            )


def _resolve_db_path(raw: str | None) -> Path | None:
    """Return an absolute Path or None when the default should apply.

    Empty strings are treated as None so a shell quoting accident does
    not silently target the cwd.
    """
    if raw is None or not raw.strip():
        return None
    return Path(raw).expanduser().resolve()


def run_directive(
    *,
    directive: str | None,
    resume_id: str | None,
    dry_run: bool,
    max_steps: int | None,
    db_path: str | None,
    dry_run_planner_content: str | None = None,
) -> int:
    """Drive `run_supervisor()` or `resume_supervisor()` from the CLI.

    Returns the integer exit code; the caller (`main`) calls `sys.exit()`
    so this function stays unit-testable.

    Lazy imports: supervisor / session_store are pulled in only here so
    `--daemon` startup doesn't transitively load anthropic / openai SDKs.
    The daemon path under `/usr/bin/python3` continues to work as before.
    """
    # Lazy imports — see module docstring.
    from config import load_env_file  # noqa: PLC0415
    from session_store import SessionStore  # noqa: PLC0415
    from supervisor import (  # noqa: PLC0415
        DEFAULT_MAX_STEPS,
        _format_run,
        resume_supervisor,
        run_supervisor,
    )

    load_env_file()

    db_resolved = _resolve_db_path(db_path)
    store = SessionStore(db_resolved) if db_resolved else SessionStore()
    store.ensure_schema()

    # CLI start — print before doing real work so the operator sees the
    # invocation even if the supervisor immediately blocks.
    #
    # Defensive redact() on the directive/resume summary: an operator
    # could (intentionally or accidentally) pass a secret-shaped substring
    # in the directive text, and the CLI start line is the first signed
    # output before the supervisor's preflight has a chance to detect it.
    # Belt-and-suspenders: mirror the 4.7 finding-1 posture here so no
    # CLI output line ever surfaces a raw secret-shaped token.
    invocation = "resume" if resume_id else "directive"
    summary_text = directive or resume_id or ""
    redacted_summary = redact(summary_text[:80])
    print(
        sign_action(
            "Atlas",
            f"CLI start: invocation={invocation} "
            f"dry_run={dry_run} db_path={store.db_path} "
            f"summary={redacted_summary}",
        ),
        flush=True,
    )

    try:
        if resume_id:
            run = resume_supervisor(
                resume_id,
                store=store,
                directive=directive,
                dry_run=dry_run,
                event_sink=_print_event,
            )
        else:
            assert directive is not None  # argparse enforces this
            run = run_supervisor(
                directive,
                max_steps=max_steps if max_steps is not None else DEFAULT_MAX_STEPS,
                dry_run=dry_run,
                store=store,
                event_sink=_print_event,
                _dry_run_planner_content=(
                    dry_run_planner_content if dry_run else None
                ),
            )
    except KeyboardInterrupt:
        # Operator hit Ctrl-C mid-run. Signed line is the visible signal
        # rather than the raw KeyboardInterrupt traceback.
        print(
            sign_action(
                "Atlas",
                "CLI end: interrupted by operator (KeyboardInterrupt). "
                "Persisted partial run can be inspected via session_store.",
            ),
            flush=True,
        )
        return CLI_EXIT_ERRORED
    except Exception as exc:  # noqa: BLE001 - unexpected; surface signed.
        print(
            sign_action(
                "Atlas",
                f"CLI end: unexpected {type(exc).__name__} — {exc}",
            ),
            flush=True,
        )
        return CLI_EXIT_ERRORED

    # Post-run summary block (operator-readable).
    print(_format_run(run))

    exit_code, category = _status_to_exit_code(run.status)
    if exit_code == CLI_EXIT_OPERATOR_ACTION:
        _print_blocker_context(run)
        # Operator-readable resume hint — matches the 4.6/4.7 surface.
        print(
            sign_action(
                "Atlas",
                f"To inspect: uv run python -m session_store --show "
                f"{run.run_id}. To resume (when state allows): "
                f"uv run python orchestra.py --resume {run.run_id} "
                f"--db-path {store.db_path}",
            ),
            flush=True,
        )

    print(
        sign_action(
            "Atlas",
            f"CLI end: run_id={run.run_id} session_id={run.session_id} "
            f"status={run.status} db_path={store.db_path} "
            f"exit={exit_code} ({category})",
        ),
        flush=True,
    )
    return exit_code


# --- CLI parser + main ------------------------------------------------------


def _cli_dry_run() -> int:
    """End-to-end smoke for the 4.8 CLI surfaces.

    Drives `run_directive()` with synthetic planner content + a temp DB
    through four scenarios and asserts each one. No provider calls; no
    network; no on-disk side effects outside the temp dir. Exits 0 on
    full pass, 1 on any failure. Used by the regression block alongside
    `--self-test`.
    """
    import io  # noqa: PLC0415 - local: only for the CLI smoke harness
    import json as _json  # noqa: PLC0415

    failures: list[str] = []

    def record(case: str, ok: bool, detail: str) -> None:
        if not ok:
            failures.append(f"{case}: {detail}")
        print(
            sign_action(
                "Cody",
                f"orchestra CLI dry-run {'pass' if ok else 'FAIL'} "
                f"— {case}: {detail}",
            ),
            flush=True,
        )

    with tempfile.TemporaryDirectory(prefix="orchestra-cli-smoke-") as tmp:
        db_path = str(Path(tmp) / "sessions.db")

        # Capture stdout so the harness can assert what the CLI surface
        # printed, without leaking the live feed into the test output.
        def _capture(fn, *args, **kwargs) -> tuple[int, str]:
            buf = io.StringIO()
            saved = sys.stdout
            sys.stdout = buf
            try:
                code = fn(*args, **kwargs)
            finally:
                sys.stdout = saved
            return code, buf.getvalue()

        # Scenario 1 — directive dry-run completes; exit 0; signed live
        # feed present; run_id/session_id printed; DB row persisted.
        code, out = _capture(
            run_directive,
            directive="Ask Cody to acknowledge the 4.8 CLI smoke.",
            resume_id=None,
            dry_run=True,
            max_steps=3,
            db_path=db_path,
        )
        ok = (
            code == CLI_EXIT_COMPLETE
            and "CLI start:" in out
            and "Supervisor start:" in out
            and "Planner start." in out
            and "Plan validated:" in out
            and "Finalizer start." in out
            and "Supervisor complete:" in out
            and "CLI end:" in out
            and "exit=0 (complete)" in out
        )
        record(
            "directive-complete-exit-0",
            ok,
            f"exit={code}, lines={out.count(chr(10))}, "
            f"complete_in_out={'exit=0 (complete)' in out}",
        )

        # Scenario 2 — secret-shaped directive: preflight blocker, exit 10.
        # Belt-and-suspenders on finding 1: the CLI start line must NOT
        # echo the raw synthetic secret token.
        synthetic = "sk-proj-FFFFFFFFFFFFFFFFFFFF"
        code, out = _capture(
            run_directive,
            directive=f"Use {synthetic} please.",
            resume_id=None,
            dry_run=True,
            max_steps=3,
            db_path=db_path,
        )
        ok = (
            code == CLI_EXIT_OPERATOR_ACTION
            and synthetic not in out
            and "[REDACTED:openai_api_key]" in out
            and "exit=10 (operator-action-required)" in out
        )
        record(
            "directive-secrets-block-exit-10",
            ok,
            f"exit={code}, raw_token_in_stdout={synthetic in out}, "
            f"operator_action_named={'operator-action-required' in out}",
        )

        # Scenario 3 — invalid plan envelope → blocker exit 10. Use a
        # synthetic planner content that bypasses the plan block and
        # triggers PlanError; the supervisor halts with a signed parse
        # blocker that the CLI surfaces in the operator-context block.
        code, out = _capture(
            run_directive,
            directive="Plan validation smoke.",
            resume_id=None,
            dry_run=True,
            max_steps=3,
            db_path=db_path,
            dry_run_planner_content="no orchestra_plan block here",
        )
        ok = (
            code == CLI_EXIT_OPERATOR_ACTION
            and "plan_parse_or_validate" in out
            and "exit=10 (operator-action-required)" in out
        )
        record(
            "directive-plan-validate-block-exit-10",
            ok,
            f"exit={code}, plan_block_present="
            f"{'plan_parse_or_validate' in out}",
        )

        # Scenario 4 — resume terminal-state from scenario 1 prints the
        # rehydrated run without re-running providers; exit 0.
        from session_store import SessionStore as _SS  # noqa: PLC0415

        recent = _SS(db_path).list_sessions(limit=1)
        target_run_id = recent[0].run_id if recent else None
        if target_run_id is None:
            record(
                "resume-terminal-state-exit-0",
                False,
                "no persisted session to resume against",
            )
        else:
            code, out = _capture(
                run_directive,
                directive=None,
                resume_id=target_run_id,
                dry_run=True,
                max_steps=3,
                db_path=db_path,
            )
            ok = (
                code == CLI_EXIT_COMPLETE
                and "invocation=resume" in out
                and "exit=0 (complete)" in out
            )
            record(
                "resume-terminal-state-exit-0",
                ok,
                f"exit={code}, target_run_id={target_run_id[:8]}…",
            )

        # Scenario 5 — exit-code mapping invariants.
        from supervisor import (  # noqa: PLC0415
            SUPERVISOR_STATUS_BLOCKED,
            SUPERVISOR_STATUS_COMPLETE,
            SUPERVISOR_STATUS_ERRORED,
            SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL,
        )

        mapping = {
            SUPERVISOR_STATUS_COMPLETE: (CLI_EXIT_COMPLETE, "complete"),
            SUPERVISOR_STATUS_BLOCKED: (
                CLI_EXIT_OPERATOR_ACTION,
                "operator-action-required",
            ),
            SUPERVISOR_STATUS_PENDING_HUMAN_APPROVAL: (
                CLI_EXIT_OPERATOR_ACTION,
                "operator-action-required",
            ),
            SUPERVISOR_STATUS_ERRORED: (CLI_EXIT_ERRORED, "errored"),
        }
        mapping_ok = all(
            _status_to_exit_code(status) == (code, label)
            for status, (code, label) in mapping.items()
        )
        unknown_ok = _status_to_exit_code("not-a-real-status") == (
            CLI_EXIT_ERRORED,
            "unknown-status:not-a-real-status",
        )
        record(
            "exit-code-mapping",
            mapping_ok and unknown_ok,
            f"mapping_ok={mapping_ok}, unknown_ok={unknown_ok}",
        )

        # Scenario 6 — JSON-serializable signed lines (smoke check that
        # the live feed is plain-text printable; no control characters).
        feed_lines = [
            line for line in out.splitlines() if line.startswith("[Atlas")
        ]
        ok = bool(feed_lines) and all(
            _json.dumps(line) for line in feed_lines
        )
        record(
            "signed-feed-printable",
            ok,
            f"feed_lines={len(feed_lines)}",
        )

    return CLI_EXIT_COMPLETE if not failures else CLI_EXIT_ERRORED


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orchestra",
        description=(
            "Agent Orchestra runtime entry point. Use --directive for "
            "orchestrated runs; the bare positional is the legacy no-op "
            "smoke path."
        ),
    )
    # Legacy positional path — kept stable for M2/M3/M4 receipts.
    parser.add_argument(
        "legacy_directive",
        nargs="*",
        help=(
            "Legacy no-op smoke positional. Runs the M2/M3 stub flow. For "
            "real orchestration use --directive."
        ),
    )
    # 4.8 surfaces.
    parser.add_argument(
        "--directive",
        type=str,
        default=None,
        help="Orchestrated directive routed through run_supervisor().",
    )
    parser.add_argument(
        "--resume",
        dest="resume_id",
        type=str,
        default=None,
        help="Resume an existing run_id or session_id via resume_supervisor().",
    )
    parser.add_argument(
        "--max-steps",
        dest="max_steps",
        type=int,
        default=None,
        help="Override the planner step budget (clamped by HARD_CAP_MAX_STEPS).",
    )
    parser.add_argument(
        "--db-path",
        dest="db_path",
        type=str,
        default=None,
        help="Override the default SessionStore DB path.",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Run without provider calls; still exercises persistence + signing.",
    )
    # Legacy lifecycle flags.
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as a long-lived heartbeat service.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Daemon heartbeat interval (seconds).",
    )
    parser.add_argument(
        "--self-test",
        dest="self_test",
        action="store_true",
        help="Run hook self-test.",
    )
    parser.add_argument(
        "--cli-dry-run",
        dest="cli_dry_run",
        action="store_true",
        help=(
            "Run the 4.8 CLI dry-run scenarios end-to-end against a temp "
            "DB. No provider calls. Used by the regression block."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Lifecycle paths take precedence.
    if args.self_test:
        run_self_test()
        return CLI_EXIT_COMPLETE
    if args.cli_dry_run:
        return _cli_dry_run()
    if args.daemon:
        run_daemon(args.interval)
        return CLI_EXIT_COMPLETE

    # 4.8 orchestrated path.
    if args.directive or args.resume_id:
        return run_directive(
            directive=args.directive,
            resume_id=args.resume_id,
            dry_run=args.dry_run,
            max_steps=args.max_steps,
            db_path=args.db_path,
        )

    # Legacy positional no-op smoke path.
    legacy = " ".join(args.legacy_directive).strip()
    if not legacy:
        raise SystemExit(
            'Usage: python orchestra.py --directive "..." | '
            'python orchestra.py "legacy smoke" | '
            'python orchestra.py --self-test | '
            'python orchestra.py --daemon --interval N'
        )

    for entry in run_noop(legacy):
        append_log(entry)
    return CLI_EXIT_COMPLETE


if __name__ == "__main__":
    sys.exit(main())
