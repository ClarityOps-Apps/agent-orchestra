# Agent Orchestra — Phase 1 Scope of Work

Author: Claude (Cowork, as Atlas stand-in)
For: Garrett
Date: May 27, 2026
Status: v1 — Asana-ready spec

> **Purpose.** This document is the canonical scope of work for Phase 1 of Agent Orchestra. It maps directly onto an Asana project structure: each section here becomes an Asana section; each task here becomes an Asana task; each subtask here becomes an Asana subtask. Built per the Atlas operating standard adopted from Goal Chains 360.

**Read first:** `00-OPERATING-MODEL.md`, `01-MASTER-PLAN.md`, `02-WEEK-1-IMPLEMENTATION.md`, `05-AGENT-JOB-DESCRIPTIONS.md`, `06-STANDARD-OPERATING-PROCEDURES.md`.

---

## Phase 1 Deliverable

A fully-running Agent Orchestra **platform** — the orchestrator runtime *and* the agentic team running inside it — deployed on the DigitalOcean VPS. The team signs every action, respects the three-tier action surface, routes work through the Minimum Handoff Packet, and stops at the merge gate awaiting Garrett's approval. The platform itself is the Phase 1 product; the first project the team works on comes in Phase 2 planning.

## Milestones

| ID | Milestone | When |
|---|---|---|
| **M1** | Accounts & access ready | End of pre-flight (before Day 1) |
| **M2** | Skeleton alive on VPS | End of Day 1 |
| **M3** | MCP wired + identity signing live | End of Day 2 |
| **M4** | First real run end-to-end with merge gate | End of Day 3 |
| **M5** | 24/7 operation hardened | End of Day 4 |
| **M6** | Phase 1 validated | End of Week 1 |

---

## Section 0 — Pre-flight (before Day 1)

**Owner: Garrett.** All tasks must complete before Day 1 kicks off. **Closes M1.**

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| 0.1 Sign up for DigitalOcean | Garrett | Account active, billing method on file | — |
| 0.2 Provision Ubuntu 24.04 droplet, 2GB/2vCPU, NYC3 | Garrett | Droplet running, public IP captured, backups enabled | 0.1 |
| 0.3 Generate and save SSH key | Garrett | Key pair generated, public key added to droplet, SSH access verified | 0.2 |
| 0.4 Generate OpenAI API key (for Atlas) | Garrett | Key generated, saved to password manager, billing limit set | — |
| 0.5 Generate Anthropic API key (for Cody/Scribe/Scout) | Garrett | Key generated, saved to password manager, billing limit set | — |
| 0.6 Decide GitHub account/org for Agent Orchestra repo | Garrett | Account/org confirmed, ability to create private repo verified | — |
| 0.7 Confirm Asana workspace and create Agent Orchestra project | Atlas-stand-in (Claude/Cowork) | Project exists with all sections and tasks from this spec populated | 0.6 |

---

## Section 1 — Runtime Scaffolding (Day 1, Wednesday)

**Owner: Cody (with Atlas review).** **Closes M2.**

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| 1.1 Initialize Python 3.11+ project with `uv` (fallback `poetry`) | Cody | `pyproject.toml` valid; `uv sync` or `poetry install` succeeds | M1 |
| 1.2 Create project layout per `02-WEEK-1-IMPLEMENTATION.md` | Cody | Folders and empty files match spec: `atlas/`, `agents/cody|scribe|scout/`, `hooks/`, `mcp/`, `memory/`, `packets/`, `orchestra.py` | 1.1 |
| 1.3 Create empty `system_prompt.md` for each agent | Cody | Files exist; Atlas's is left blank (Garrett writes it Day 2); Cody/Scribe/Scout have starter drafts from `05-AGENT-JOB-DESCRIPTIONS.md` | 1.2 |
| 1.4 Implement `hooks/identity_signing.py` real | Cody | Hook enforces signature block on every agent action; unsigned actions blocked with clear error | 1.2 |
| 1.5 Implement `hooks/lifecycle.py` real | Cody | Agent start/stop events logged with timestamp + agent name | 1.2 |
| 1.6 Stub `hooks/approval_gates.py` and `hooks/secrets_check.py` | Cody | Stubs return pass for all; will be implemented Day 2 | 1.2 |
| 1.7 Wire `orchestra.py` for no-op end-to-end test | Cody | `python orchestra.py "hello team"` produces signed log entries from Atlas and Cody | 1.3, 1.4, 1.5, 1.6 |
| 1.8 Deploy to VPS, set up systemd | Cody | `systemctl status orchestra` shows running; auto-restart on simulated crash | 1.7, M1 |
| 1.9 **Verification checkpoint — skeleton alive** | Garrett | Garrett runs `python orchestra.py "hello team"` from his laptop, sees signed log entries from Atlas and Cody | 1.7, 1.8 |

---

## Section 2 — MCP & Integration Wiring (Day 2, Thursday)

**Owner: Cody (with Atlas review). Garrett owns Atlas's system_prompt.md.** **Closes M3.**

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| 2.1 Write Atlas's `system_prompt.md` | Garrett | Role definition expanded per `05-AGENT-JOB-DESCRIPTIONS.md`; covers communication register, autonomy expectations, hot-button issues | M2 |
| 2.2 Configure Asana MCP server | Cody | `mcp/asana.json` valid; secrets in `.env`; Atlas can read Asana tasks from the Agent Orchestra project | 2.1 |
| 2.3 Configure GitHub MCP server | Cody | `mcp/github.json` valid; Cody can read repo state, open PRs against test repo | 2.1 |
| 2.4 Configure filesystem MCP server | Cody | `mcp/filesystem.json` valid; agents can read/write within project root only | 2.1 |
| 2.5 Implement `hooks/approval_gates.py` real | Cody | Pre-tool-use hook intercepts Human-approved-only actions per `00-OPERATING-MODEL.md` §4; routes approval request through Atlas to Garrett | 2.1 |
| 2.6 Implement `hooks/secrets_check.py` real | Cody | Pre-tool-use hook blocks any tool call containing recognized secret patterns; logs the block | 2.1 |
| 2.7 Cody and Scribe `system_prompt.md` finalized | Atlas-stand-in (Claude/Cowork), Garrett reviews | Drafts produced from JDs; Garrett edits and approves | 2.1 |
| 2.8 Scout (thin) `system_prompt.md` finalized | Atlas-stand-in (Claude/Cowork), Garrett reviews | Smoke-script-only scope; Garrett edits and approves | 2.1 |
| 2.9 Integration test: Atlas reads Asana → Cody posts signed comment | Cody | Cody posts a signed comment in the Agent Orchestra Asana project on a designated test task | 2.2, 2.5, 2.7 |
| 2.10 **Verification checkpoint — MCP wired** | Garrett | Garrett asks Atlas (via CLI or directive interface): "Read the most recent task in our Asana project and post a comment from Cody that says Cody is online." Confirms the comment lands in Asana, signed. | 2.9 |

---

## Section 3 — Agent Implementation & First Real Run (Day 3, Friday)

**Owner: Atlas + Cody + Scout (with Garrett verification at end).** **Closes M4.**

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| 3.1 Atlas reviews the hooks behavior end-to-end | Atlas-stand-in (Claude/Cowork) | Hooks behavior walked through in plain English; confirmed matches §3, §4, §9 of Operating Model | M3 |
| 3.2 Pick a real test task from the Agent Orchestra Asana backlog | Atlas-stand-in (Claude/Cowork), Garrett confirms | Task is real, scoped (one PR's worth, <200 lines), touches at least 3 agents, low blast radius | M3 |
| 3.3 Atlas writes the scope statement per SOP-04 | Atlas-stand-in (Claude/Cowork) | Scope statement in Asana with objective, in-scope, out-of-scope, acceptance criteria, owner; signed | 3.2 |
| 3.4 Scribe records Plan of Record in Asana | Cody (Scribe stand-in until Scribe is alive) | Asana task reflects the plan; status set; comment trail clean | 3.3 |
| 3.5 Cody implements the test task | Cody | PR opened on the Agent Orchestra repo with full Evidence Receipt (commit SHA, tests, migration state if any, boundary statement, remaining gates) | 3.3 |
| 3.6 Scout runs smoke checks against the test task | Scout | Smoke check receipts posted in Asana; pass/fail verdict signed | 3.5 |
| 3.7 Atlas reviews the PR in substance | Atlas-stand-in (Claude/Cowork) | Atlas posts APPROVED, APPROVED WITH CONDITIONS, or REQUEST CHANGES per SOP-07 | 3.5, 3.6 |
| 3.8 Approval gate hits Garrett for merge | System | Garrett receives a clear merge-approval prompt with one-paragraph summary; can approve or reject | 3.7 |
| 3.9 **Verification checkpoint — first real run** | Garrett | Garrett experiences the full team loop end-to-end and approves (or rejects) the merge | 3.8 |

---

## Section 4 — Hardening & Monitoring on VPS (Day 4, Saturday)

**Owner: Cody (with Atlas review).** **Closes M5.**

Note: VPS was provisioned and the runtime deployed during Day 1 per skip-to-VPS decision. Day 4 is hardening, not first deployment.

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| 4.1 Verify systemd auto-restart on simulated crash | Cody | Kill the orchestra process; verify restart within 60s; log confirms restart | M4 |
| 4.2 Confirm daily log digest email to Garrett works | Cody | Test email arrives; contents include yesterday's signed activity summary | M4 |
| 4.3 Implement heartbeat alerting (Atlas unresponsive >10 min) | Cody | Simulate Atlas stall; alert fires to Garrett within window | M4 |
| 4.4 Rotate any temporary credentials used during bring-up | Cody | All bring-up credentials rotated; new credentials in `.env` on VPS only | M4 |
| 4.5 Add any monitoring Garrett requested after reviewing Days 1–3 | Cody | Per Garrett's direction | 4.1, 4.2, 4.3 |
| 4.6 **Verification checkpoint — 24/7 operation** | Garrett | Garrett powers off laptop; sends a directive from phone; team responds. Next morning, team still running. | 4.1, 4.2, 4.3 |

---

## Section 5 — Live Use & Friction Log (Day 5, Sunday → ongoing)

**Owner: Garrett (using the team).** No milestone — rolling.

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| 5.1 Create `runtime/FRICTION-LOG.md` | Cody | File scaffolded with template (date, friction description, classification, disposition) | M5 |
| 5.2 Daily Morning Brief per SOP-01 | Atlas + Scribe (Cody as stand-in for Scribe initially) | Brief posted each morning summarizing yesterday's signed activity, blockers, pending approvals | M5 |
| 5.3 Live directives session | Garrett | Garrett uses the team to do real work; logs every friction point | M5 |
| 5.4 Daily decision log entries per SOP-11 | Scribe (or Cody stand-in) | Each gated decision logged in `runtime/memory/decisions/YYYY-MM-DD.md` | M5 |

---

## Section 6 — Phase 1 Retro & Validation (Days 6–7, Mon–Tue Week 2)

**Owner: Atlas + Scribe drafts; Garrett decides.** **Closes M6.**

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| 6.1 Scribe synthesizes the week's friction log + decision log into themes | Scribe (or Cody stand-in) | Themes document drafted: repeated friction, near-misses, surprises, wins | Section 5 |
| 6.2 Atlas annotates themes with hypotheses | Atlas-stand-in (Claude/Cowork) or real Atlas | Each theme has a "why" hypothesis and "what would change it" recommendation | 6.1 |
| 6.3 Garrett reviews retro and decides on changes | Garrett | Decisions logged: prompt edits, SOP edits, Operating Model edits, phase progression | 6.2 |
| 6.4 Evaluate Phase 2 trigger: should Sentinel + hooks layer be added now? | Atlas-stand-in (Claude/Cowork) or real Atlas | Recommendation with reasoning posted in Asana; Garrett decides | 6.3 |
| 6.5 Evaluate Routines as Phase 2+ complementary tool | Atlas-stand-in (Claude/Cowork) or real Atlas | List of candidate Routines jobs with cost/value analysis; Garrett decides | 6.3 |
| 6.6 Phase 1 deliverable verification | Atlas-stand-in (Claude/Cowork) or real Atlas | All M1–M5 verified complete; Operating Model rules visibly enforced; no unauthorized actions in the week's log | 6.1, 6.2 |
| 6.7 **Verification checkpoint — Phase 1 validated** | Garrett | Garrett confirms platform meets the success criteria in `01-MASTER-PLAN.md` §6 | 6.6 |

---

## Section 7 — Ad-hoc / Discovered Work (rolling buffer)

**Owner: rolling.** This section catches work surfaced during build that doesn't fit a planned task. Bugs found, missing capabilities discovered, design decisions captured, follow-up questions.

| Pattern | Notes |
|---|---|
| Bugs discovered during build | Logged here with reproducer + agent that found it; assigned per Decision Routing |
| Missing capabilities | Logged with use case; queued for Phase 2 or noted as accepted gap |
| Design decisions made mid-build | Logged with rationale (per SOP-17 if Operating Model changes) |
| Open questions for Garrett | Queued for next sync; not blocking unless tagged |

---

## Acceptance gates summary

Phase 1 is **complete** when:

1. All Section 0 tasks done. (M1)
2. All Section 1 tasks done and Day 1 verification checkpoint passed. (M2)
3. All Section 2 tasks done and Day 2 verification checkpoint passed. (M3)
4. All Section 3 tasks done and Day 3 verification checkpoint passed. (M4)
5. All Section 4 tasks done and Day 4 verification checkpoint passed. (M5)
6. Section 5 has at least 2 days of real use logged.
7. All Section 6 tasks done and Day 7 verification checkpoint passed. (M6)
8. Operating Model rules visibly enforced in the week's signed log — no anonymous actions, no unapproved Human-approved-only actions, no Sentinel-block bypasses.
9. Garrett confirms platform meets the success criteria in `01-MASTER-PLAN.md` §6.
