# Agent Orchestra — Standard Operating Procedures

Author: Claude (Cowork)
For: Garrett
Date: May 27, 2026
Status: v1 — draft for review

> **Source of truth on team and rules:** `00-OPERATING-MODEL.md`. This file is the procedural layer — *when X happens, do Y*. Each SOP names a trigger, an owner, steps, receipts, and escalation rules.

SOP format (consistent across all of them):
- **Trigger** — what makes this SOP fire
- **Owner** — which agent runs it
- **Steps** — numbered, executable
- **Receipts** — what gets logged where
- **Escalation** — when to bail out and route up

---

## Operating SOPs (recurring activities)

### SOP-01. Daily Operations

**Trigger.** Every working day, at the start.
**Owner.** Atlas, with Scribe.

**Steps.**
1. Scribe pulls overnight activity from Asana, GitHub, deploy logs.
2. Scribe posts a "Morning Brief" in Asana: open work, blockers, gated approvals waiting on Garrett, anything that broke or alerted.
3. Atlas reviews the brief, sets the day's priorities, and either delegates to agents or queues directives for Garrett's confirmation.
4. Garrett opens the Morning Brief, confirms or adjusts priorities in plain English.
5. Atlas dispatches the day's work.

**Receipts.** Morning Brief posted in Asana, signed by Scribe and acknowledged by Atlas.
**Escalation.** If overnight activity contains a P1 incident or a safety incident, jump immediately to SOP-12 (Incident Response).

---

### SOP-02. Weekly Retro

**Trigger.** Every Monday morning (or first working day of the week).
**Owner.** Scribe drafts; Atlas annotates; Garrett decides.

**Steps.**
1. Scribe synthesizes the week's `runtime/memory/decisions/` log into themes — repeated friction, near-misses, surprises, wins.
2. Atlas annotates with hypotheses: *why* did each theme happen, *what* would change it.
3. Garrett reviews the synthesis, decides what to change (which prompts to edit, which SOPs to update, which guardrails to harden).
4. Scribe writes the changes into the relevant docs and updates `00-OPERATING-MODEL.md` if rules change.
5. The next day's work runs against the updated model.

**Receipts.** Retro doc filed in `runtime/memory/retros/YYYY-WW.md`.
**Escalation.** If retro surfaces a safety pattern, Sentinel runs SOP-11 (Guardrail Violation Investigation) on the pattern.

---

### SOP-03. Friction Log Triage

**Trigger.** Garrett adds an entry to `runtime/FRICTION-LOG.md`, or weekly during retro.
**Owner.** Atlas + Scribe.

**Steps.**
1. Atlas reads the friction entry and classifies it: prompt issue, rule gap, SOP gap, infrastructure issue, or scope/expectation mismatch.
2. Scribe logs the classification in `runtime/memory/decisions/`.
3. For prompt issues: Atlas drafts a system_prompt.md edit, Garrett approves.
4. For rule/SOP gaps: change is queued for the next Weekly Retro (SOP-02).
5. For infrastructure issues: Atlas routes to Cody to fix; PR-checkpoint procedure applies.

**Receipts.** Each friction entry has a tagged disposition in the log.
**Escalation.** Friction entries marked "safety" go directly to Sentinel without waiting for retro.

---

## Checkpoint SOPs (from §6 of Operating Model)

### SOP-04. Scope Checkpoint

**Trigger.** Before any non-trivial build starts.
**Owner.** Atlas.

**Steps.**
1. Atlas reads Garrett's directive (or the Asana task) end-to-end.
2. Atlas writes: objective, in-scope, out-of-scope, acceptance criteria, agent who owns the next action.
3. Atlas posts the scope statement in the relevant Asana task, signed.
4. Scribe logs the scope as Plan of Record per the Plan of Record Rule.
5. Atlas delegates to Cody (or whichever agent owns the next action).

**Receipts.** Scope statement in Asana, signed by Atlas, time-stamped.
**Escalation.** If Atlas can't write the scope after reading the directive, escalate to Garrett for clarification — do not guess.

---

### SOP-05. Architecture Checkpoint

**Trigger.** Before implementation of any change that affects data model, integration boundaries, or has cross-component impact.
**Owner.** Atlas.

**Steps.**
1. Atlas drafts an Architecture Decision Record (ADR) in the project's ADR directory.
2. ADR covers: context, options considered, decision, trade-offs, consequences.
3. Atlas posts the ADR link in the Asana task and tags Sentinel for safety review.
4. Sentinel runs SOP-06 (Safety Checkpoint).
5. After Sentinel clearance, Atlas hands the spec to Cody.

**Receipts.** ADR filed, linked in Asana, signed by Atlas. Sentinel clearance signed.
**Escalation.** If the decision has business implications (cost, customer impact, scope change), Atlas requests Garrett approval before locking the ADR.

---

### SOP-06. Safety Checkpoint

**Trigger.** Before any work touching: database, deployment, secrets, customer data, RLS, or destructive operations.
**Owner.** Sentinel.

**Steps.**
1. Sentinel reads the proposed action, the target environment, and the rollback posture.
2. Sentinel classifies the action per the three-tier action surface (Safe / Guarded / Human-approved only).
3. Sentinel checks for: secrets exposure, RLS gaps, customer-boundary crossings, irreversible operations, missing rollback plans.
4. Sentinel posts a verdict: APPROVED, APPROVED WITH CONDITIONS, or BLOCKED.
5. If BLOCKED: per the Safety-Block Authority Rule, the block stands. Sentinel writes a one-paragraph escalation summary, Atlas routes to Garrett.

**Receipts.** Safety verdict in the Asana task, signed by Sentinel, time-stamped.
**Escalation.** Disagreement between Sentinel and Atlas → Garrett (per §8 Conflict Resolution).

---

### SOP-07. PR Checkpoint

**Trigger.** Cody opens a PR.
**Owner.** Cody (preparation), then Atlas + Scout + Sentinel (review).

**Steps.**
1. Cody opens PR with full Evidence Receipt: commit SHA, PR link, test results, migration state, boundary statement, remaining gates.
2. Atlas reviews substance — does the code match the spec?
3. Scout runs smoke checks / full QA per phase. Posts receipt.
4. Sentinel reviews safety implications.
5. UX Reviewer reviews experience layer (Phase 3+).
6. All approvals signed in Asana and on the PR.
7. Merge gate held until Garrett's approval (Phase 1–3) or Release Captain executes (Phase 4).

**Receipts.** PR description with all receipts; Asana comment with merge-state summary signed by Atlas.
**Escalation.** Any block (Sentinel, Scout) holds. Two-agent disagreement → §8 Conflict Resolution.

---

### SOP-08. QA Checkpoint

**Trigger.** Code is merged to main.
**Owner.** Scout, with Release Captain in Phase 4.

**Steps.**
1. Environment Sync Rule: confirm QA env is synced to GitHub main.
2. Scout runs the full smoke path against QA.
3. Scout exercises the user workflow that the change affects.
4. Scout posts a QA Pass / QA Fail verdict in Asana.
5. On QA Fail: Atlas reopens the work; Cody fixes; back to SOP-07.

**Receipts.** QA verdict in Asana, signed by Scout, with screenshot or log evidence.
**Escalation.** Repeated QA Fail on the same change after 2 fix cycles → escalate to Garrett.

---

### SOP-09. Release Checkpoint

**Trigger.** Customer-facing or production deploy is ready.
**Owner.** Release Captain (Phase 4) or Atlas (until Phase 4).

**Steps.**
1. Per Customer Migration Gate Rule, request explicit Garrett approval.
2. Present: exact command, target environment, risk summary, rollback posture, release checklist.
3. On Garrett approval, execute per the release checklist playbook.
4. Run post-deploy verification (health checks, smoke, customer-facing spot check).
5. Post release receipt in Asana with all evidence.

**Receipts.** Release receipt: commit SHA, target, time, command executed, verification results, rollback availability window.
**Escalation.** Any failed post-deploy verification → SOP-10 (Rollback) immediately.

---

## Reactive SOPs (incidents and exceptions)

### SOP-10. Rollback Procedure

**Trigger.** Post-deploy verification fails, or Garrett pulls the cord.
**Owner.** Release Captain (Phase 4), or whichever agent executed the deploy.

**Steps.**
1. Halt any in-flight work touching the affected component.
2. Execute the documented rollback for the change.
3. Verify the rolled-back state matches pre-deploy state.
4. Post rollback receipt in Asana, signed.
5. Open a P1 incident (SOP-12) to find root cause.

**Receipts.** Rollback receipt: trigger, time, command, verification.
**Escalation.** If rollback fails or is incomplete, escalate to Garrett immediately with the exact state.

---

### SOP-11. Guardrail Violation Handling

**Trigger.** An agent attempts an action blocked by a hook, or Sentinel flags a guardrail near-miss.
**Owner.** Sentinel + Scribe.

**Steps.**
1. Hook (or Sentinel) blocks the action with an error message and a reason.
2. The blocked action and reason are logged in `runtime/memory/decisions/YYYY-MM-DD.md`.
3. Sentinel reviews: was the violation a near-miss (rule worked) or a gap (rule needs updating)?
4. If gap: queue an update to `00-OPERATING-MODEL.md` for the next Weekly Retro.
5. If pattern: Sentinel can request immediate escalation to Garrett.

**Receipts.** Decision log entry with classification (near-miss / gap / pattern).
**Escalation.** Three near-misses in one week on the same rule = automatic escalation to Garrett.

---

### SOP-12. Incident Response

**Trigger.** Production breakage, customer-facing bug discovered post-release, safety incident, or PagerDuty / monitoring alert.
**Owner.** Atlas (incident commander), with Garrett notified immediately for P1/P2.

**Severity tiers.**
- **P1** — customer impact, data integrity, or revenue impact. Notify Garrett immediately.
- **P2** — significant degradation, no immediate data loss. Notify Garrett within 1 hour.
- **P3** — limited impact, workaround exists. Log and handle in normal flow.

**Steps.**
1. Atlas declares severity and notifies Garrett per tier.
2. Atlas halts in-flight non-critical work.
3. Sentinel reviews for ongoing safety risk; advises on containment.
4. If rollback is the right move, execute SOP-10.
5. If the fix is forward, follow SOP-04 → SOP-05 → SOP-07 on an expedited timeline.
6. Scribe maintains the live incident log in Asana with timestamped updates.
7. After resolution, write a blameless postmortem within 48 hours.

**Receipts.** Incident log in Asana. Postmortem in `runtime/memory/postmortems/YYYY-MM-DD-<slug>.md`.
**Escalation.** P1 always notifies Garrett. P1 unresolved after 4 hours → consider rolling back even if the team thinks it can fix forward.

---

### SOP-13. Escalation to Garrett

**Trigger.** Any agent needs Garrett's call.
**Owner.** Atlas (consolidates escalations from other agents).

**Steps.**
1. Agent identifies the escalation trigger (gated action, Sentinel block, Scout block, ambiguous spec, conflict resolution).
2. Agent writes a one-paragraph summary: what's being decided, the trade-offs, the recommendation, what happens if Garrett ignores it.
3. Atlas reviews the summary, consolidates if multiple escalations are pending, and presents to Garrett.
4. Garrett decides. Decision logged in Asana with rationale.

**Receipts.** Escalation log entry, signed, with Garrett's decision recorded.
**Escalation.** Cap: no agent escalates the same decision twice without new information.

---

## Onboarding SOPs

### SOP-14. Onboarding a New Agent

**Trigger.** Adding a new agent to the team (e.g., moving from Phase 1 → Phase 2 by adding Sentinel).
**Owner.** Atlas + Scribe + Garrett.

**Steps.**
1. Garrett confirms the agent's role belongs in the next phase.
2. Atlas drafts the agent's JD by copying the relevant section of `05-AGENT-JOB-DESCRIPTIONS.md`.
3. Scribe drafts the agent's `system_prompt.md` from the JD; Garrett edits.
4. Atlas defines the agent's decision rights, interfaces, and gates in `00-OPERATING-MODEL.md` if not already there.
5. Cody implements the agent runtime (`<agent>.py`) per the SDK pattern.
6. A shadow period: the new agent runs alongside the existing team but blocks nothing. Atlas reviews the agent's outputs daily.
7. After shadow period (recommended: 1 week), the agent goes live with full authority per its JD.

**Receipts.** Agent onboarding log: JD link, prompt link, shadow period dates, go-live date — all in Asana.
**Escalation.** If the agent produces unreliable output during shadow, extend shadow or revise prompt before go-live.

---

### SOP-15. Onboarding a New MCP Server

**Trigger.** A new tool is needed by the team (e.g., adding a new SaaS integration).
**Owner.** Atlas (judgment) + Cody (implementation) + Sentinel (trust review).

**Steps.**
1. Atlas confirms the need: what does the team need this MCP for that existing tools don't cover?
2. Sentinel reviews the MCP source: official vendor, community, or first-party? What's the trust boundary?
3. Per `00-OPERATING-MODEL.md` §4, adding a new MCP is a Human-approved-only action: Garrett approves.
4. Cody adds the MCP config in `runtime/mcp/<name>.json`, secrets in `.env`.
5. Cody writes a test exercising the MCP from each agent that needs access.
6. Scout runs smoke tests.
7. Atlas updates the agents' system prompts to know they can use the new tool.

**Receipts.** MCP onboarding entry in Asana: source, trust review, test results, agents granted access.
**Escalation.** If Sentinel cannot vouch for the MCP source, request Garrett to make the trust call explicitly.

---

### SOP-16. Onboarding Garrett Onto a New Agent's Output

Garrett needs to know how to consume a new agent's output before they go live.

**Trigger.** A new agent goes live (per SOP-14).
**Owner.** Atlas + Scribe.

**Steps.**
1. Scribe writes a one-page "How to read [Agent]'s output" doc.
2. The doc covers: where the agent posts (Asana, Slack, log), how to read its signature line, what its "approved / blocked / escalated" verdicts look like, what to do when it escalates.
3. Garrett reviews; doc is published in `runtime/docs/`.

**Receipts.** Doc filed; Garrett's acknowledgment logged.

---

## SOP-17. Editing the Operating Model Itself

The operating model is a living document. When it needs to change, follow this procedure to keep changes traceable.

**Trigger.** Weekly Retro identifies a rule, SOP, or phase change. Or Garrett requests an edit.
**Owner.** Scribe (drafting), Atlas (review), Garrett (approval).

**Steps.**
1. Scribe drafts the change in a PR-style Asana task: what's changing, what was the old behavior, what's the new behavior, what triggered the change.
2. Atlas reviews and either approves substance or requests revisions.
3. Garrett approves (this is a Human-approved-only action — the operating model is the contract for the entire team).
4. Scribe edits `00-OPERATING-MODEL.md` and updates the appendix change log.
5. Atlas re-issues system prompts to any agent whose behavior is affected, so the change propagates.

**Receipts.** Operating Model change log entry; signed Garrett approval; commit SHA of the doc edit.
**Escalation.** If a rule change has retroactive implications for in-flight work, halt that work and reroute per the new rule.

---

### SOP-18. Completion Standard Hygiene Sweep

**Trigger.** Every working day, during Scribe's Morning Brief preparation (per SOP-01). Also fires on demand if Garrett or Atlas flags suspected drift.
**Owner.** Scribe.

**Steps.**
1. Scribe queries Asana via MCP for all tasks in active projects where a signed closure comment is present in the comment trail (pattern: `[Agent · UTC] *closure*`, `*marked complete*`, or other Completion Standard wording) AND the task's `completed` flag is `false`.
2. For each drift detected, Scribe posts a signed comment on the task naming: the closure comment GID, the agent who posted it, the date, and the Completion Standard Rule violated.
3. Scribe surfaces the drift list in the day's Morning Brief with a count and per-task summary.
4. Each drifted task is assigned to the agent who posted the closure comment for resolution. That agent either flips the status bit (if the work is genuinely done) or posts a corrected receipt explaining why the task is not yet complete.
5. If the same agent drifts more than 3 times in a single working week, Scribe escalates to Atlas as a pattern. Atlas decides whether the agent's prompt needs refinement per SOP-17 (Editing the Operating Model Itself) or per a narrower prompt-edit procedure.
6. If drift exceeds 10 tasks workspace-wide in a single sweep, Scribe escalates to Garrett with a one-paragraph summary for an operating-model review.

**Receipts.** Drift report appended to the Morning Brief in Asana, signed by Scribe. Per-task drift comments posted on each affected task. Pattern escalations posted on M-active milestone with classification (single-agent vs. workspace-wide).
**Escalation.** Repeated drift from a single agent → Atlas (prompt refinement). Workspace-wide drift → Garrett (operating-model review). Same-agent drift on tasks involving customer environments, secrets, or destructive operations → Garrett immediately (P1 escalation regardless of count).
