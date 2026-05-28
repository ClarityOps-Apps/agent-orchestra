# PROM — The Plan of Record Operating Model

*An Asana-native playbook for multi-agent + human teams.*

Version 1.0 · May 2026
Authored by Clarity Ops · Distilled from production use across multiple agentic delivery teams

---

## Quick start

**What PROM is.** A principle-based operating model for teams where AI agents and humans collaborate on real work — software delivery, research, operations, anything with sequenced decisions and durable artifacts. PROM uses Asana as the durable plan of record, defines clean role boundaries, and enforces a small set of non-negotiable rules so agents stop using humans as message couriers without ever crossing the lines that should require human judgment.

**Who PROM is for.** Founders, team leads, and operators running 2+ AI agents alongside human collaborators on a real project that has stakes — production systems, customer data, paying users, or anything where "the wrong action" has real cost.

**What you get from this playbook.** A reusable operating model: roles, rules, checkpoints, an Asana project template, and a workbook to adapt PROM to your team in under an hour.

**What PROM is not.** It is not an orchestration framework (you still need one, e.g., a thin Python orchestrator, the Claude Agent SDK, LangGraph, etc.). It is not a replacement for tool-specific security and safety practices. It does not eliminate the need for human judgment — it concentrates the human's attention on the moments that actually require it.

---

## 1. The premise

When two or more agents work on the same project, the most common failure mode is *coordination drift*. Each agent has its own conversation, its own memory, its own implicit assumptions about what's been decided. The human becomes a courier — copying messages from one agent's chat into another's, restating decisions, repairing context. This scales badly. By the third agent it's untenable.

The root cause is not the agents. The root cause is the absence of a durable, shared plan of record. Without one:

- Decisions get made in chat windows that no one else can search.
- Each agent builds work on a private summary of what they think was decided.
- Status drifts from reality.
- The human spends most of their time on translation, not judgment.

PROM solves this by establishing a single durable surface — an Asana project — as the canonical record of *what is being decided*, *what is being built*, *what has been verified*, and *what is complete*. Agents read from it. Agents write to it. Humans review it. When chat, code, and the plan disagree, **the plan wins** — or, more precisely, the work is not done until all three agree.

That is the whole premise. Everything else in PROM follows from that.

---

## 2. The three surfaces

PROM aligns three durable surfaces. Work is not complete until they agree.

| Surface | Role | What lives here |
|---|---|---|
| **Asana** | Plan of Record | Plans, decisions, scope, acceptance criteria, blockers, review comments, completion summaries, milestones |
| **Code repository** | Implementation Record | Code, commits, pull requests, tests, migrations |
| **Repo documentation** | Durable Operating Record | Architecture decisions, runbooks, onboarding guides, operating rules that apply across projects |

If a decision lives only in chat, it does not exist yet. Post it to Asana. If a change has been merged but Asana still says "in progress," the work is not done. Update Asana. If Asana describes a behavior that the code doesn't actually implement, the code or the doc is wrong — reconcile before the work is considered complete.

This three-surface alignment is the foundation of PROM. Every rule and procedure in this playbook serves it.

---

## 3. The roles

PROM uses generic role names that you map to your specific agents and team members. A minimal team has four roles. A complete team has up to nine. Most teams settle around five or six.

### Minimum viable team (start here)

| Role | Mission | Maps to (your name here) |
|---|---|---|
| **Director** | Owns product intent, customer-facing decisions, irreversible approvals. The human. | Founder, PM, business owner |
| **Architect** | Translates Director's intent into scope, sequence, acceptance criteria. Routes work. Reviews implementation in substance. | Senior agent (often a strong reasoning model) |
| **Implementer** | Builds what the Architect specified. Opens PRs. Posts implementation evidence. | Coding agent (often a code-tuned model) |
| **Documenter** | Maintains Asana hygiene. Records decisions and receipts. Synthesizes patterns for retros. | Documentation agent (often a cheaper, hygienic model) |

### Expanded team (add as scale demands)

| Role | Mission | When to add |
|---|---|---|
| **Reviewer (Quality)** | Independently verifies that what was built does what was specified. Blocks merges on quality. | When self-testing by Implementer is no longer sufficient |
| **Safety** | Reasons about risk, environment boundaries, customer-data exposure, irreversible operations. Has block authority on safety calls. | When the team touches production data, customer environments, or anything with cost-of-being-wrong > "annoying" |
| **Releaser** | Owns the act of shipping. Merge sequencing, deploys, post-merge verification. | When release cadence is high enough that conflating "build" and "ship" causes mistakes |
| **Designer** | Reviews user-experience layer — clarity, microcopy, states, polish. | When the work has a customer-facing UI |
| **Integrator** | Owns connections to external systems, APIs, integrations, credentials. | When the number of integrated systems is high enough that ownership ambiguity causes drops |

### How to choose roles

Two heuristics:

1. **Start with the minimum.** Director + Architect + Implementer + Documenter. Run a real piece of work end-to-end. See what hurts.
2. **Add a role when its absence is causing a recurring failure**, not when it would be theoretically nice. Premature roles are bureaucracy; reactive roles are responses to real friction.

---

## 4. The principles

PROM's principles are non-negotiable rules that every agent on the team must follow. They are baked into agent system prompts. The deterministic ones are hard-enforced by code (hooks); the judgment-based ones are enforced by the Safety agent (when present) and audited at retro.

### 4.1 Plan of Record Rule
Asana is the durable coordination layer. Every major decision, approval, blocker, and receipt is logged there. Chat is useful for nudges and quick coordination; it is not the record.

### 4.2 Three Surfaces Must Agree
Work is not done until Asana, the code repository, and the durable docs all reflect the same reality. If they disagree, the work is incomplete.

### 4.3 Identity-Signing Rule
Every action — comments, commits, handoffs, status changes, PR descriptions — is signed with the acting agent's name and a UTC timestamp.

Example: `[Architect · 2026-05-27T14:32Z] Posted scope statement for Task 1.7. Awaiting Implementer.`

Anonymous or unsigned actions are rejected.

### 4.4 Spec Before Build Rule
Ambiguous work requires a scope statement from the Architect (or approval gate) before the Implementer begins. Small bug fixes still need acceptance criteria — they can just be one line.

### 4.5 Review Before Merge Rule
Implementation can move fast, but no PR merges to main without substantive Architect approval and, when warranted, Quality verification and Safety clearance.

### 4.6 Evidence Receipt Rule
No work is considered complete without evidence: commit SHA, PR link, test results, migration state (where relevant), boundary statement (which environments were touched), and remaining gates.

### 4.7 No Silent Work Rule
Any agent doing meaningful work reports what changed, what was verified, what was not touched, and what remains open. Silent retries, silent tool swaps, silent scope expansion — all forbidden.

### 4.8 Asana Decision Fetch Rule
When the Architect (or any agent) posts a decision in Asana — architecture, scope, acceptance criteria, merge readiness — downstream agents must fetch and read the actual Asana comment before acting. They must report back the comment ID (or first line) they are building from. If they cannot identify the decision comment with confidence, they pause and ask rather than guessing.

This rule exists because chat-relayed summaries of Asana decisions lose fidelity. The decision is in Asana; downstream work should be built from the actual Asana text.

### 4.9 Safety-Block Authority Rule
When the Safety agent (when present) and any other agent — including the Architect — disagree on a safety call, **the Safety block stands**. The disagreement is escalated to the Director with a one-paragraph summary of the disagreement, the trade-offs, and each agent's recommendation. The Director adjudicates.

### 4.10 Implementer-Pending Message Rule
Whenever the Architect identifies pending work for the Implementer, the Architect produces a copy/paste-ready message in the same response, including: relevant Asana/PR/doc links, exact requested action, acceptance criterion, and expected receipt. This is required even when the substance has already been posted to Asana — because Implementers (currently) often cannot auto-listen to Asana and need an explicit chat or message channel notification to act.

### 4.11 Completion Standard
A task is complete only when:
- The relevant PR is merged to main (if code-bearing)
- Post-merge verification has passed
- Asana has a closure comment with merge SHA and verification evidence
- Parent task and subtasks are marked complete
- Any affected durable docs are updated
- No unauthorized environment, secret, or customer-data action occurred

### 4.12 SME Synthesis Rule
Decisions requiring technical, operational, or domain expertise are synthesized by the appropriate specialist agent before reaching the Director. The specialist presents a recommendation including: the recommended option, alternatives considered, trade-offs, reasoning, cost of being wrong, and a clear ask. The Director's role is to judge the recommendation, not synthesize the answer. Raw technical questions are never routed to the Director without specialist synthesis first.

---

## 5. The six-checkpoint rhythm

PROM moves work through six checkpoints. Each checkpoint produces a durable artifact in Asana. Skipping a checkpoint is a deferred cost — it always surfaces later, more expensively.

| # | Checkpoint | Who | Output |
|---|---|---|---|
| 1 | **Architecture checkpoint** | Architect | Scope statement: objective, in-scope, out-of-scope, acceptance criteria, owner of next action |
| 2 | **Implementation checkpoint** | Implementer | PR opened with Evidence Receipt (commit SHA, tests, migration state, boundary statement, remaining gates) |
| 3 | **Review checkpoint** | Architect (+ Safety, Reviewer when present) | Substantive review: APPROVED / APPROVED WITH CONDITIONS / REQUEST CHANGES, with specific actionable feedback |
| 4 | **Director smoke checkpoint** *(when warranted)* | Director | User-facing or customer-impact verification of the actual behavior |
| 5 | **Merge checkpoint** | Architect or Releaser | Merge after all preceding checkpoints pass; recorded with merge SHA |
| 6 | **Post-merge checkpoint** | Implementer (or Releaser) | Pull main, run post-merge verification, post closure summary in Asana, mark task complete |

Checkpoint 4 (Director smoke) is conditional. It applies when the work has a user-facing or customer-impact dimension that an agent cannot fully validate. For purely internal or technical work, Architect approval is sufficient at Checkpoint 3.

---

## 6. The three-tier action surface

Every action an agent might take is classified into one of three tiers. The tier determines who must approve it and how.

| Tier | Examples | Operating rule |
|---|---|---|
| **Safe** | Local repo work, drafts, internal analysis, comments on tasks, internal-only documents | Agents may act autonomously within their stated decision rights |
| **Guarded** | Code merges to main, internal QA migrations, internal deployments, opening external-facing PRs | Agents may proceed after required checks, receipts, and role-appropriate review — but every action is logged and reviewable |
| **Human-approved only** | Production deploys, customer-data writes, secrets rotation, destructive operations, irreversible changes, external sends | Director approval required *before* action. Agent must present exact command, target, risk, and rollback posture |

This tiering is deliberately small. More tiers add bureaucracy without adding clarity. Three is sufficient for almost every team.

**Adapting the action surface to your team.** Map each of your real-world actions to a tier. Document the mapping. Review it monthly during retros — actions that have proven safe over many uses can be promoted (Human-approved → Guarded → Safe); actions that have caused incidents can be demoted.

---

## 7. The Minimum Handoff Packet

Every handoff between agents (Architect → Implementer, Implementer → Reviewer, Reviewer → Director, etc.) includes the following fields. This eliminates the most common cause of rework: ambiguous handoffs.

- **Objective and user-facing outcome.** What is being achieved, and what experience does the user end up with?
- **In scope.** What is included in this unit of work.
- **Out of scope.** What is explicitly *not* included — both to prevent scope creep and to surface deferrals.
- **Files, docs, Asana tasks, and PRs to read.** Direct links or IDs, not summaries.
- **Architecture decisions and product constraints.** What's already been decided that affects this work.
- **Data/environment boundary statement.** What environment is involved (local, internal QA, customer-hosted, production), and what is forbidden to touch.
- **Acceptance criteria.** How "done" will be judged.
- **Required tests and smoke path.** Specific tests to run, specific user flows to verify.
- **Receipt requirements.** What evidence must be posted upon completion.
- **Known risks and escalation triggers.** What might go wrong; when to stop and ask.
- **Signature line.** Sending agent, receiving agent, UTC timestamp.

A handoff missing any of these fields is rejected and returned. This sounds bureaucratic; in practice, after one or two cycles, agents internalize the format and produce it natively.

---

## 8. The Asana Decision-Fetch Rule (implementation)

This is PROM's most Asana-specific rule. The principle (read the durable record before acting) is universal; the implementation pattern is built on Asana's primitives.

### How it works

1. When the Architect (or any agent) makes a binding decision — architecture, scope, acceptance criteria, merge readiness — they post it as a comment on the relevant Asana task. The comment text includes a stable prefix the Implementer can recognize, e.g., `Architect decision:` or `Scope statement:`.

2. The Architect (or Director relaying on the Architect's behalf) sends a notification to the Implementer in the active communication channel (chat, message, or a webhook to the orchestrator). The notification includes:
   - The Asana task name and GID (globally unique ID).
   - The comment GID, if available, or the stable decision prefix.
   - A one-sentence summary of what is being asked.

3. The Implementer's first act is to fetch the Asana comment by GID (via Asana's API/MCP). The Implementer does *not* implement from the relay summary — that's an information-loss path.

4. Before writing any code, the Implementer reports back: the comment GID, the first line of the comment text (as proof of read), and a one-sentence interpretation of what they intend to do.

5. If the Implementer cannot identify the decision comment with confidence, they pause and ask. Guessing is forbidden.

### Why this is non-optional

Chat-relayed summaries lose fidelity. The Implementer's interpretation drifts from the Architect's intent. By the time the PR is reviewed, the gap surfaces as rework — sometimes significant rework. The Decision-Fetch Rule trades a few seconds at the start of every task for substantial rework avoidance at the end.

### Adapting to other tools

If your team uses Linear, the equivalent is fetching the specific Linear issue comment by ID. Notion: fetch the specific block. Jira: fetch the comment by ID. The rule is tool-agnostic; the implementation is whatever your PoR tool's primitive for "addressable comment" is.

---

## 9. Identity-signing in practice

Identity-signing is the cheapest, highest-value rule in PROM. It costs nothing to implement (a string template in every agent's system prompt + a deterministic hook to reject unsigned actions) and it provides:

- **Accountability.** When something is wrong, you know which agent did it.
- **Audit trail.** A clear, time-ordered record of who said what when.
- **Trust.** Each agent develops a recognizable voice and reliability profile over time.

### Signature format

`[Agent Name · YYYY-MM-DDTHH:MM:SSZ] One-line summary of the action.`

Examples:
- `[Architect · 2026-05-27T14:32:18Z] Posted scope statement for Task 1.7. Awaiting Implementer.`
- `[Implementer · 2026-05-27T15:45:02Z] Opened PR #142. Tests passing. Awaiting Architect review.`
- `[Reviewer · 2026-05-27T16:12:55Z] PR #142 — quality block. Edge cases on empty input missing. Returning with notes.`

### Where signatures appear

- Every Asana comment.
- Every commit message (in the first line or footer).
- Every PR description and PR review comment.
- Every status change in Asana (typically as a comment paired with the status update).
- Every handoff message.

### Hook-enforced

The deterministic enforcement of identity-signing is implemented in the orchestrator as a pre-action hook: if an outgoing action lacks the signature pattern, the hook blocks it and returns a clear error. This is non-overridable — even the Architect (most senior agent) cannot bypass it.

---

## 10. Implementer-Pending Messages

Most Implementer agents today cannot auto-listen to Asana — posting a comment in Asana does not, by itself, notify the Implementer or cause them to read it. PROM addresses this with the Implementer-Pending Message rule.

### The rule

Whenever the Architect (or any agent) identifies work that the Implementer must do next, the same response produces a copy/paste-ready message for the Implementer. The Director (or orchestrator) can then forward that message verbatim to the Implementer's active channel.

### The format

```
Task in [PoR tool]: [Task name + GID]
Decision comment: [Comment GID or stable prefix]

Action.  [Exact requested action, including paths, files, commands.]

Acceptance criteria.  [What "done" looks like.]

Boundaries.  [What is forbidden — environments, files, actions.]

Expected receipt.  [What to post back on completion: PR link, test results, etc.]

Sign your action.
```

### Why the format matters

The Implementer's first response to a vague handoff is to ask clarifying questions, which costs a round trip. A handoff that includes acceptance criteria and boundaries up-front reduces clarifications by 50–80% in practice.

### Future evolution

When Implementer agents gain reliable auto-listening on PoR tools (Asana watch-channels, Linear webhooks, etc.), this rule becomes weaker — the Asana comment itself becomes the trigger. Until then, the explicit message is required.

---

## 11. Conflict resolution ladder

When agents disagree, PROM uses a defined ladder.

1. **Peer dialogue first.** The disagreeing agents state their positions in a shared Asana comment thread. Two rounds maximum.
2. **Reporting line decides on routine disagreements.** If the Architect and Implementer disagree on implementation approach, the Architect decides.
3. **Safety overrides shipping.** If Safety and any other agent disagree on a safety call, Safety's block stands per the Safety-Block Authority Rule.
4. **Quality overrides Architect on merge readiness.** If the Reviewer flags a quality failure and the Architect wants to ship anyway, the Reviewer's block stands. Escalates to Director.
5. **Director decides on cross-cutting trade-offs.** Any escalation to the Director includes a one-paragraph summary: the choice, the trade-offs, each agent's recommendation. Director's call is logged in Asana with rationale.

Every conflict resolution — peer-resolved or escalated — is logged in the relevant Asana task as a comment, signed, with the *why* included. No "we decided X" without rationale.

---

## 12. Completion standard

A task is complete in PROM only when *all* of the following are true:

- [ ] The relevant PR (if code-bearing) is merged to main.
- [ ] Post-merge verification has passed.
- [ ] The Asana task has a closure comment with the merge SHA and verification evidence.
- [ ] Parent task and all subtasks are marked complete.
- [ ] Any affected durable docs (repo docs, runbooks, ADRs) are updated.
- [ ] No unauthorized environment, secret, or customer-data action occurred during the work.

If any box is unchecked, the task is not complete. Closing the task without all six is a violation of the PROM operating model and is grounds for the Documenter to reopen the task.

---

## 13. The Asana project template

This section is the cookie-cutter — what you actually build in Asana to instantiate PROM for a new project.

### Section structure (default template)

```
📋 Reference & Standing Context
   ↳ Standing context — read before starting any task
       (Notes: links to your operating model, JDs, SOPs, scope-of-work)

Section 0 — Pre-flight
   ↳ M1 — [your first milestone] (resource_subtype: milestone)
   ↳ 0.1 ... 0.N — pre-flight tasks (account setup, API keys, environment provisioning)

Section 1 — [your first phase, e.g., "Scaffolding (Day 1)"]
   ↳ M2 — [milestone]
   ↳ 1.1 ... 1.N — tasks
   ↳ 1.N+1 — ✅ Verification checkpoint

Section 2 — [your second phase]
   ↳ M3 — [milestone]
   ↳ 2.1 ... 2.N — tasks
   ↳ 2.N+1 — ✅ Verification checkpoint

[... continue for each phase ...]

Section N+1 — Ad-hoc / Discovered Work (rolling buffer)
   ↳ Buffer for bugs, missing capabilities, design decisions, open questions
```

### Naming conventions

- **Task names** start with the task ID (e.g., `1.4 Implement identity-signing hook`).
- **Milestones** start with `M[number] — ` and use Asana's `milestone` resource subtype.
- **Verification checkpoints** start with `✅ Verification — ` and are owned by the Director.
- **Reference tasks** at the top of `📋 Reference & Standing Context` are pinned and never marked complete.

### Task description template

Every task description includes:

```
Owner: [agent role]
Acceptance criteria: [what done looks like]
Dependencies: [task IDs that must complete first]
Reference: [link to canonical doc section]
```

### Custom fields (recommended)

- **Priority**: Low / Medium / High (enum)
- **Phase**: which phase this task belongs to (enum, matches your section structure)
- **Acting Agent**: which agent owns the next action (enum: Director, Architect, Implementer, etc.)
- **Action Surface**: Safe / Guarded / Human-approved only (enum)
- **Verification Evidence**: link/text field for the receipt (text)

### Known Asana quirks when creating a PROM project via API/MCP

If you create the project programmatically (via Asana API or an MCP tool) with sections passed in bulk:

- **Section order is not preserved.** Sections may appear scrambled in the UI. You will need to drag-reorder them manually.
- **An "Untitled section" is auto-created at the top.** You will need to delete it manually.

Both quirks are 30-second fixes in the Asana UI. They are not blockers — just things to expect.

---

## 14. Onboarding workbook

A new team adopting PROM should complete this workbook in a single session — 60 to 90 minutes for a focused team.

### Step 1 — Name your roles (10 min)

Decide which roles you need:
- [ ] **Director** (required) — name: _______________ (this is the human / founder / lead)
- [ ] **Architect** (required) — name: _______________ (which agent or person)
- [ ] **Implementer** (required) — name: _______________
- [ ] **Documenter** (required) — name: _______________
- [ ] **Reviewer** — name: _______________ (or "deferred to Phase 2")
- [ ] **Safety** — name: _______________ (or "deferred to Phase 2")
- [ ] **Releaser** — name: _______________ (or "deferred")
- [ ] **Designer** — name: _______________ (or "deferred")
- [ ] **Integrator** — name: _______________ (or "deferred")

You can use generic role names ("Architect", "Implementer") or give your agents distinct names ("Atlas the Architect", "Cody the Implementer"). Distinct names work better for the identity-signing rule.

### Step 2 — Define your environments (10 min)

List every environment your team operates against, and classify by action surface:

| Environment | What it is | Tier (Safe / Guarded / Human-approved only) |
|---|---|---|
| Local development | | |
| Internal QA / staging | | |
| Production | | |
| Customer-hosted / per-tenant | | |
| Database — non-prod | | |
| Database — prod | | |
| Secrets storage | | |

Default: production and customer-hosted environments are **Human-approved only**. Internal QA is **Guarded**. Local is **Safe**.

### Step 3 — Set your gates (10 min)

List the actions that *always* require Director approval before execution:

- [ ] Merging code to main
- [ ] Deploying to production
- [ ] Sending external email or message
- [ ] Writing to customer data
- [ ] Rotating credentials or secrets
- [ ] Modifying another agent's decision rights
- [ ] Adding a new integration / MCP server
- [ ] Spending > $X in a single session (you pick X)
- [ ] [your custom gate here]

### Step 4 — Clone the Asana template (15 min)

Create a new Asana project. Build the section structure from §13 above. Customize section names to match your phases. Populate the standing-context task with links to your scope-of-work and operating model.

If you create the project programmatically: drag-reorder sections after creation and delete the auto-generated "Untitled section."

### Step 5 — Write your agent system prompts (20–30 min)

For each role, write a system prompt that includes:

- **Mission.** One paragraph: what this agent's job is.
- **Decision rights.** What it can decide on its own vs. what it must escalate.
- **PROM rules.** Reference §4 of this playbook; embed the principles directly.
- **Communication register.** Plain English to the Director; dev-speak to peers.
- **Identity-signing requirement.** Every action signed.

Use a starter template per role and refine over the first few weeks of real use.

### Step 6 — Dry-run a small task (15 min)

Pick a real, small task. Push it through the full six-checkpoint rhythm with your team. Observe where the friction is. Adjust prompts. Re-run.

You're operating under PROM after one successful dry-run cycle.

---

## 15. Adapting PROM

### For non-Asana tools

PROM's principles are tool-agnostic. Only the Decision-Fetch Rule's implementation is Asana-specific (it relies on Asana's addressable comments). Adaptation for other tools:

| Tool | Decision-fetch primitive | Notes |
|---|---|---|
| **Linear** | Issue comment fetched by ID | Works cleanly; Linear's API is similar to Asana's |
| **Jira** | Comment fetched by ID | Works; Jira's API is more verbose but capable |
| **Notion** | Block fetched by block ID | Works; Notion's database model gives you more schema flexibility |
| **Monday** | Update or item activity fetched by ID | Works; less mature API than Asana |
| **GitHub Projects** | Issue or project item comment by ID | Works for engineering-only teams; less rich than dedicated PM tools |
| **ClickUp** | Comment fetched by ID | Works |

In all cases the principle is the same: durable, addressable comments on durable, addressable plan items. Anything that gives you those two primitives can host PROM.

### For smaller teams (single agent + human)

You can run a reduced version of PROM with just Director + Architect (or Director + Implementer, depending on your work). The Plan of Record discipline, identity-signing, completion standard, and three-surface alignment all still apply — they're not contingent on multi-agent. You skip Inter-agent conflict resolution (no peers to disagree). You skip Implementer-Pending Message format (you and the agent talk directly).

The value of PROM is lower at single-agent scale, but the cost is also lower. If you expect to grow the team, starting with PROM hygiene from Day 1 saves rework later.

### For larger teams (many agents, multiple humans)

PROM extends cleanly. The Decision Routing pattern means most decisions reach the Director already synthesized; this scales well. Conflict resolution adds two new dimensions:

- **Inter-human disagreement.** Resolve outside PROM via your usual organizational decision-making — PROM doesn't try to be a corporate governance framework.
- **Cross-team agent disagreement.** Add an inter-team escalation path. Typically the Architects of each team confer; if unresolved, escalation to Directors.

### For non-software work

PROM was developed for software delivery but applies to any structured-work context where multiple agents collaborate with humans:

- **Research projects.** Plan of Record = research plan. Architect = research lead agent. Implementer = research execution agents. Reviewer = peer-review or fact-check agent. The artifact is a finding or report, not a PR.
- **Content production.** Plan = editorial calendar. Architect = editor agent. Implementer = writer agent. Reviewer = copy-edit / fact-check. Releaser = publisher.
- **Operations and ticket triage.** Plan = ops backlog. Architect = senior ops agent. Implementer = handling agent. Safety = compliance-check agent.

The principles don't change. The role mapping does.

---

## 16. Failure modes to watch for

In production use, these failure modes recur most often. Spot them early.

| Symptom | Likely cause | Fix |
|---|---|---|
| Agents asking the Director the same question repeatedly | Architect not synthesizing properly; SME Synthesis Rule being skipped | Reinforce in Architect system prompt; add explicit examples |
| Tasks getting marked complete before code is merged | Completion Standard being skipped | Hook-enforce; reopen offending tasks and reset Documenter's hygiene rules |
| Decisions getting made in chat, not Asana | Plan of Record Rule slipping under deadline pressure | Hook-enforce: agent posts to chat get auto-replied with "post to Asana first" |
| Implementer building from a relay summary, not the Asana comment | Decision-Fetch Rule not being internalized | Implementer must report back comment GID + first line; if missing, reject and return |
| Reviewer / Safety blocks being overridden by Architect | Safety-Block Authority Rule not being respected | Hook-enforce; log every override attempt and escalate |
| Sections in Asana drifting from reality | Documenter not enforcing hygiene; Asana hygiene SOP not being run | Add a daily Documenter sweep; rerun morning brief |
| Same friction surfacing every week with no fix | No retro happening, or retro happening without follow-through | Make weekly retro a hard checkpoint; assign owners to retro action items |

---

## 17. Glossary

| Term | Definition |
|---|---|
| **PROM** | Plan of Record Operating Model. This playbook. |
| **Plan of Record (PoR)** | The durable, canonical record of plans, decisions, and status. In PROM, this is Asana. |
| **Three Surfaces** | PoR + Code repository + Durable docs. Must agree for work to be complete. |
| **Action Surface** | The three tiers (Safe / Guarded / Human-approved only) governing how an action gets executed. |
| **Six-Checkpoint Rhythm** | The default flow: Architecture → Implementation → Review → Director Smoke → Merge → Post-merge. |
| **Minimum Handoff Packet** | The 11-field structure every inter-agent handoff includes. |
| **Decision-Fetch Rule** | Agents must read the actual Asana comment before acting on a decision, not a relay summary. |
| **Identity-Signing Rule** | Every action signed with agent name + UTC. |
| **Safety-Block Authority** | The Safety agent's block on safety calls cannot be overridden except by the Director. |
| **SME Synthesis Rule** | Specialist agents synthesize recommendations before the Director sees raw questions. |
| **Completion Standard** | The six conditions that must all be true for a task to be marked complete. |
| **Director** | The human in the loop. Owner of product intent, customer-facing decisions, irreversible approvals. |
| **Architect** | The senior agent. Owns scope, sequence, technical approval. |
| **Implementer** | The execution agent. Builds what the Architect specified. |
| **Documenter** | The hygiene agent. Maintains the Plan of Record, runs retros, captures decisions. |
| **Reviewer** | The quality agent. Independently verifies work matches spec. |
| **Safety** | The risk agent. Reasons about environment boundaries, customer-data exposure, irreversibility. |
| **Releaser** | The shipping agent. Owns merge sequencing, deploys, post-merge verification. |

---

## About this playbook

PROM was distilled from production use across multiple agentic software delivery teams operating under [Clarity Ops](https://clarityops.co) supervision. It is offered as a portable, reusable methodology for any team running multi-agent + human collaboration on real work.

This playbook is intended to be cloned, customized, and applied. If you'd like setup support, brand-customized variants, or training for your team, contact Clarity Ops.

---

*PROM v1.0 · May 2026 · Clarity Ops*
