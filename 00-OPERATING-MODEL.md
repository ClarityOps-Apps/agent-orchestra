# Agentic Software Delivery Operating Model

A proposed agent orchestration model for autonomous software delivery with human checkpoints, guardrails, and durable accountability.

Prepared for Garrett Delph · Drafted by Atlas · May 27, 2026
Adopted version with push-backs folded in by Claude (Cowork) · May 27, 2026
Source: `AGENT TEAM/Agentic Software Delivery Operating Model.docx`

> **Operating principle:** Autonomous execution, explicit accountability, and human approval only at irreversible or business-significant gates.

---

## Adoption notes

This document is the canonical operating model for Agent Orchestra. It is the adopted version of Atlas's draft. The following changes were folded in from a review pass:

1. **Atlas split path** noted in §10 — Atlas remains combined as Architect/Orchestrator for Phases 1–2; splits into separate Orchestrator + Architect by Phase 3 if load warrants.
2. **Enforcement Architecture** added as §9 — Sentinel is the reasoning layer; a deterministic hooks layer sits underneath for hard rules that no agent (including Atlas) can override.
3. **Thin Scout added to Phase 1** in §10 — even a smoke-script-only QA agent on Day 1 catches the worst self-graded misses given Garrett's "militant about QA" stance.
4. **Safety-Block Authority Rule** added to §3 — when Sentinel and Atlas disagree on a safety call, Sentinel's block stands and the disagreement escalates to Garrett.
5. **Identity-Signing Rule** added to §3 — every agent action (comments, commits, handoffs, Asana updates) is signed with the agent's name and UTC timestamp, restoring the pattern from the original Gemini transcript.

`03-AGENT-TEAM-AND-GUARDRAILS.md` is superseded by this document.

---

## 1. Executive Summary

The right agent orchestration model is not a large unsupervised swarm. It is a small, disciplined delivery team with named roles, named decision rights, and named gates. The human should stop being the courier between agents, but should remain the business owner for product intent, customer-facing approvals, and irreversible decisions.

The recommended structure gives Atlas orchestration authority, gives Cody implementation responsibility, adds safety and QA agents around the delivery path, and uses Asana, GitHub, and project documentation as durable memory.

---

## 2. Agentic Team

| Agent | Role | Primary Responsibility |
|---|---|---|
| **Atlas** | Architect / Orchestrator | Owns product interpretation, architecture, sequencing, tradeoff calls, agent routing, and final technical approval. |
| **Cody** | Implementation Engineer | Writes code, opens PRs, runs local gates, posts implementation receipts, and raises blockers against Atlas specs. |
| **Sentinel** | Safety / Data Boundary Agent | Watches secrets, Supabase project refs, RLS, destructive commands, customer boundaries, and release gates. Can block unsafe work. |
| **Scout** | QA / Smoke Test Agent | Runs Replit QA, smoke scripts, regression checks, screenshot checks, and acceptance testing against the actual user workflow. |
| **Scribe** | Documentation / Memory / Asana Agent | Keeps Asana, architecture docs, memory files, release notes, and handoff receipts accurate and current. |
| **Release Captain** | GitHub / Supabase / Deployment Agent | Handles merge sequencing, post-merge checks, QA migration workflow, deployment receipts, and release checklist discipline. |
| **UX Reviewer** | Interface / Product Polish Agent | Reviews visual behavior, interaction clarity, empty states, contrast, layout, loading states, and user confusion. |

**Note on Atlas:** Combining Architect and Orchestrator into a single agent is appropriate at Phase 1–2 scale. By Phase 3, Atlas's context load and the latent conflict of interest (designing work and approving its implementation) are expected to surface. The phased rollout in §10 reserves the option to split Atlas into separate Orchestrator and Architect agents if needed.

---

## 3. Core Governance Rules

| Rule | What It Does |
|---|---|
| **Plan of Record Rule** | Asana is the durable coordination layer. Every major decision, approval, blocker, and receipt is logged there. |
| **Human Decision Gate** | The human approves customer database pushes, production deployments, destructive operations, scope changes, and irreversible data changes. |
| **Agent-to-Agent Handoff Rule** | Every handoff includes objective, scope, non-goals, docs to read, acceptance criteria, tests, boundaries, and references. (See §8 Minimum Handoff Packet.) |
| **No Silent Work Rule** | Any agent doing meaningful work reports what changed, what was verified, what was not touched, and what remains open. |
| **Safety Boundary Rule** | Every action is classified by surface: safe, guarded, or human-approved only. (See §4.) |
| **Spec Before Build Rule** | Ambiguous features require an Atlas spec or approval before coding. Small bug fixes still need acceptance criteria. |
| **Review Before Merge Rule** | Implementation can move quickly, but Atlas or a review agent approves the PR in substance before merge. |
| **Evidence Receipt Rule** | No work is done without evidence: commit SHA, PR link, test results, migration state, boundary statement, and remaining gates. |
| **Environment Sync Rule** | Before Replit QA or new dev work, the QA environment is synced to GitHub main. |
| **Customer Migration Gate Rule** | JLOOP / customer database changes require explicit human approval and the release checklist playbook. |
| **Identity-Signing Rule** *(added)* | Every agent action — comments, commits, handoffs, Asana updates, PR descriptions — is signed with the agent's name and UTC timestamp. Example: `[Cody · 2026-05-27T14:32Z] Implemented webhook handler per Atlas spec. PR #142.` Anonymous or unsigned actions are rejected by the hooks layer. |
| **Safety-Block Authority Rule** *(added)* | When Sentinel and Atlas disagree on a safety call, Sentinel's block stands. Atlas cannot override Sentinel on safety. The disagreement is escalated to Garrett with a one-paragraph summary of the disagreement, the trade-offs, and each agent's recommendation. |
| **SME Synthesis Rule** *(added)* | Decisions requiring technical, operational, or domain expertise are synthesized by the appropriate SME agent before reaching Garrett. The SME presents a one-paragraph recommendation including: the recommended option, alternatives considered, trade-offs, reasoning, cost of being wrong, and a clear ask. Garrett's role is to judge the recommendation, not synthesize the answer. Raw technical questions are never routed to Garrett without SME synthesis first. |
| **Routing Disclosure Rule** *(added)* | When producing messages or directives that may need to be relayed to another recipient, the producer explicitly states the routing at the end of the response. Format: `Routing for this turn: → [recipient(s)] · [one-sentence reason]`. Recipients are named (Cody, Atlas, Scribe, Scout, Sentinel, Garrett, Both) or `None — FYI only`. This eliminates inference work for the human-in-the-loop courier and prevents missed handoffs. Required of every agent producing relay-worthy content, including Claude/Cowork when acting as Atlas stand-in. |
| **Completion Standard Rule** *(added)* | A task is complete only when ALL of the following hold: relevant PR merged to main (if code-bearing); post-merge verification passed; signed closure comment posted on the Asana task; task status flipped to `completed: true` in Asana via the appropriate MCP tool; parent task and all subtasks marked complete; affected durable docs updated; no unauthorized environment, secret, or customer-data action occurred during the work. **The closure comment alone does not satisfy this rule — the task status bit must also be flipped.** Posting one without the other is a Completion Standard violation, even if every other criterion is met. Scribe runs a daily hygiene sweep per SOP-18 to catch any drift between closure comments and task status. |

---

## 4. Action Surfaces and Approval Levels

| Level | Examples | Operating Rule |
|---|---|---|
| **Safe** | Local repo, docs, PR comments, Asana comments, internal analysis. | Agents may act autonomously inside existing operating rules. |
| **Guarded** | GitHub main, Replit QA, internal QA Supabase migrations. | Agents may proceed after required checks, receipts, and role-appropriate review. |
| **Human-approved only** | JLOOP Supabase, production deploys, secrets, destructive commands, irreversible data changes. | Human approval is required before action. The agent must present exact command, target, risk, and rollback posture. |

---

## 5. How the Agents Work Together

1. Garrett states product intent or identifies a problem.
2. Atlas translates intent into scope, architecture, sequence, and acceptance criteria.
3. Scribe records the plan of record in Asana and project documentation.
4. Cody implements from the spec and opens a PR with verification evidence.
5. Sentinel checks data, security, environment, migration, and customer-boundary risks.
6. Scout tests the workflow in QA and records what passes or fails.
7. UX Reviewer checks usability, visual polish, loading states, contrast, and clarity.
8. Atlas approves in substance or sends a precise fix list.
9. Release Captain merges, verifies post-merge gates, and confirms QA migration state when relevant.
10. Scribe posts the final receipt and remaining release gates.
11. Garrett steps in only for product decisions, QA acceptance, and guarded production/customer approvals.

---

## 6. Checkpoint Model

| Checkpoint | When | Purpose |
|---|---|---|
| **Scope checkpoint** | Before build | Confirms what is in scope, what is out of scope, and which agent owns the next action. |
| **Architecture checkpoint** | Before implementation | Confirms technical shape, data model, integration boundaries, and acceptance criteria. |
| **Safety checkpoint** | Before database/deployment work | Confirms target environment, secrets, destructive-risk posture, and customer boundary. |
| **PR checkpoint** | Before merge | Confirms code review, tests, migration behavior, UX, and receipts. |
| **QA checkpoint** | After merge to main | Confirms internal QA, Replit sync, smoke path, and human acceptance. |
| **Release checkpoint** | Before customer/prod | Requires human approval and operator-gated release checklist execution. |

---

## 7. Minimum Handoff Packet

Every implementation handoff includes the following fields so agents can move without using the human as a message courier:

- Objective and user-facing outcome.
- In scope and explicitly out of scope.
- Files, docs, Asana stories, and PRs to read.
- Architecture decisions and product constraints.
- Data/environment boundary statement.
- Acceptance criteria.
- Required tests and smoke path.
- Receipt requirements.
- Known risks and escalation triggers.
- **Signature line** *(added)* — sender agent name, recipient agent name, UTC timestamp.

---

## 8. Conflict Resolution *(new section)*

When two agents disagree, resolution follows a defined ladder:

1. **Peer dialogue first.** The two agents state their positions and reasoning in a shared Asana comment thread or the agreed inter-agent channel. Two rounds maximum.
2. **Reporting line decides on routine disagreements.** If Atlas and Cody disagree on implementation approach, Atlas decides (Cody reports to Atlas). If Atlas and Scribe disagree on documentation, Atlas decides.
3. **Safety overrides shipping.** If Sentinel and any other agent (including Atlas) disagree on a safety call, Sentinel's block stands per §3 Safety-Block Authority Rule. The decision is escalated to Garrett.
4. **QA overrides Atlas on merge readiness.** If Scout flags a workflow failure and Atlas wants to ship anyway, Scout's block stands. Escalates to Garrett.
5. **Garrett decides on cross-cutting trade-offs.** Any escalation to Garrett includes a one-paragraph summary: the choice, the trade-offs, each agent's recommendation. Garrett's call is logged in Asana with rationale.

Every conflict resolution — peer-resolved or escalated — is logged in the relevant Asana task as a comment, signed by the deciding agent (or Garrett), with the *why* included. No "we decided X" without rationale.

---

## 9. Enforcement Architecture: Agents + Hooks *(new section)*

Governance rules in §3 fall into three enforcement categories. Each is implemented differently. This separation matters because agents can be talked out of judgments; deterministic checks cannot.

| Category | Examples | Enforcement |
|---|---|---|
| **Hard-enforced by hooks** | Identity-Signing Rule; Human Decision Gate for §4 Human-approved-only actions; secrets must never appear in logs or commits; merge to main requires QA pass receipt | Code-level hook (pre-tool-use, post-tool-use). Blocks the action with an error message; logs the block; escalates to Atlas or Garrett as configured. No agent — not even Atlas — can override. |
| **Soft-enforced by Sentinel + system prompts** | Safety Boundary Rule (judgment calls within a tier); Spec Before Build Rule; Customer Migration Gate Rule; respect of agent decision rights | Sentinel reviews in real time; baked into each agent's system prompt; deviations flagged and routed per §8 Conflict Resolution. |
| **Audit-only** | No Silent Work Rule completeness; communication register (plain English to Garrett, dev-speak to peers); decisions log discipline | Logged for review; not blocked at action time; surfaced in weekly retro for pattern review. |

**Sentinel without hooks is incomplete.** A persuasive Cody under deadline pressure can talk a reasoning-based safety agent into letting something through. Hooks are the unargueable substrate. Sentinel is the contextual judgment layer that catches everything hooks can't anticipate.

**Hooks without Sentinel is also incomplete.** Hooks can only enforce rules they're written to enforce. Sentinel catches novel risks, edge cases, and combinatorial risk patterns that no static rule anticipated.

Both. Always both.

---

## 10. Recommended Starting Blueprint (Phased Rollout)

Start small. The first version does not need every agent to be fully independent. It needs the responsibilities separated clearly enough that each tool or agent can be introduced without changing the operating model.

**Phase 1 — Coordination + thin QA.** Atlas, Cody, Scribe, *and a thin Scout (smoke-script-only).* This removes the human from routine handoff and documentation work, and ensures even Phase 1 has independent quality verification. *(Original Atlas draft did not include Scout until Phase 3; folded forward to honor Garrett's "militant about QA" stance.)*

**Phase 2 — Add Sentinel + deterministic hooks layer.** Sentinel governs database, secret, RLS, and customer-boundary judgments. Hooks enforce the hard rules from §9 underneath Sentinel. This is when the safety architecture becomes load-bearing.

**Phase 3 — Expand QA + UX. Optionally split Atlas.** Scout expands beyond smoke scripts to full QA orchestration. UX Reviewer added for interface-polish checks. If Atlas's context load or conflict-of-interest concerns have surfaced by this point, Atlas is split into separate Orchestrator and Architect agents.

**Phase 4 — Add Release Captain.** Release Captain takes over merge, migration, and release checklist operations. By this point the team has been operating long enough to know what release discipline actually requires.

**Proactive triggers (added in Phase 2 or later, evaluated at each retro).** Once the core team is stable, evaluate Claude Code Routines as a complementary delegated-worker pattern for specific Claude-side jobs that fit "single-agent, schedule- or event-triggered" — e.g., Scribe's weekly Asana hygiene sweep, Scout's morning smoke checks, doc-drift detection on the platform repo. Routines are a tool the orchestrator can dispatch to, **not** a replacement for the orchestrator itself. Atlas, Cody, and Sentinel always live inside the orchestrated runtime where they have access to the deterministic hooks layer and the full operating model.

---

## 11. Bottom Line

The goal is not to remove the human from judgment. The goal is to remove the human from clerical coordination. Agents should be autonomous in execution, explicit in accountability, and conservative at every boundary where customer data, production systems, or irreversible changes are involved.

---

## Appendix: What changed from Atlas's original draft

| Section | Change | Why |
|---|---|---|
| §2 Agentic Team | Added footnote on Atlas split path | Single-agent architect+orchestrator carries context-load and conflict-of-interest risk at scale |
| §3 Core Governance Rules | Added Identity-Signing Rule | Restores per-action signing pattern from the original Gemini transcript |
| §3 Core Governance Rules | Added Safety-Block Authority Rule | Defines what happens when Sentinel and Atlas disagree on safety |
| §3 Core Governance Rules | Added SME Synthesis Rule | Garrett never receives raw technical questions; SMEs synthesize recommendations he judges |
| §3 Core Governance Rules | Added Routing Disclosure Rule | When messages need relaying, the producer explicitly tags recipient and reason at the end of the response — eliminates inference work for Garrett and prevents missed handoffs. Added 2026-05-29 mid-Phase-1 build per Garrett's explicit ask. |
| §3 Core Governance Rules | Added Completion Standard Rule | Formalizes the "closure comment AND status-flip together" requirement after a near-miss during M3 close where the comment landed but the task status wasn't flipped. Pairs with SOP-18 (Scribe hygiene sweep) for ongoing enforcement. Added 2026-06-01 immediately post-M3 close. |
| §7 Minimum Handoff Packet | Added signature line | Consistency with Identity-Signing Rule |
| §8 Conflict Resolution | New section | Atlas draft had no defined mechanism for inter-agent disagreement |
| §9 Enforcement Architecture | New section | Distinguishes hard-enforced (hooks) from soft-enforced (Sentinel) from audit-only; closes the "Sentinel can be persuaded" gap |
| §10 Phased Rollout | Thin Scout added to Phase 1 | Garrett's "militant about QA" stance is incompatible with no QA agent in Phase 1 |
| §10 Phased Rollout | Atlas split path noted in Phase 3 | Reserves option without forcing it |
