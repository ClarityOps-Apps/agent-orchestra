# Day 1 Kickoff Package

Author: Claude (Cowork, as Atlas stand-in)
For: Garrett
Date: May 27, 2026
Status: v1 — ready for use

This is what you hand to your existing Atlas and Cody when Day 1 begins.

---

## Part A — What to expect on Day 1 (for Garrett)

**Your time investment Day 1:** ~1 hour total. Most of it is the pre-flight you do yourself (DO signup, API keys) before kicking off. Once you hand the kickoff prompt to Atlas, the team takes over.

**The sequence:**

1. **You finish pre-flight (Section 0 in Asana).** ~30–45 minutes. DigitalOcean account, droplet, SSH key, OpenAI key, Anthropic key, GitHub account/org. Each task in Section 0 of the Asana project has acceptance criteria — work through them.

2. **You hand the kickoff prompt (Part B below) to Atlas.** Atlas reads the prompt + the four reference docs, asks you any clarifying questions, then begins Section 1 work.

3. **Atlas validates the thin-orchestrator architecture call.** Per my earlier recommendation: thin custom Python orchestrator (~300–500 lines) is the default. If Atlas has substantive reason to push for the Claude Agent SDK + OpenAI adapter route instead, listen — Atlas has hands-on data I don't. Otherwise lock the default.

4. **Cody scaffolds the project layout per Section 1 of the Asana spec.** You don't need to watch this. You can use the time to do other work.

5. **Day 1 verification checkpoint (task 1.9 in Asana).** You personally run `python orchestra.py "hello team"` and confirm you see signed log entries from Atlas and Cody. If you see them, M2 closes. If you don't, the team owes you a fix before tomorrow.

**What you should NOT do Day 1:**
- Do not write any code.
- Do not pick a different orchestration framework than what's been agreed.
- Do not commit secrets to git (anywhere, ever).
- Do not approve any tool call that touches production, secrets, or customer data without reading what's being asked.

**What will probably surface Day 1:**
- One or two small infrastructure friction points (API key format, droplet networking, Python version). Log them in `FRICTION-LOG.md`, let Atlas route to Cody.
- A small disagreement on file structure naming. Atlas decides per `00-OPERATING-MODEL.md` decision rights.
- A question about how to handle agent identity in the no-op test. Atlas writes the convention; Scribe records it.

---

## Part B — Kickoff prompt (give this to Atlas and Cody)

Copy-paste the block below into a fresh Atlas session, then a fresh Cody session. (Or send both as a single message if you're using a multi-agent interface.)

---

> **Kickoff: Agent Orchestra — Phase 1, Day 1.**
>
> I am standing up the Agent Orchestra platform — a personal-first, productize-later agent orchestration runtime that will host the team itself (Atlas, Cody, Scribe, Scout, and eventually Sentinel, UX Reviewer, Release Captain). Phase 1's deliverable is the runtime itself: orchestrator, MCP layer, hooks, agents, deployed on a DigitalOcean VPS, signing every action and respecting the three-tier action surface.
>
> **Read these documents in this order before doing anything else:**
>
> 1. `/Users/garrettdelph/Claude/AGENT ORCHESTRA/00-OPERATING-MODEL.md` — canonical team, governance rules, action surfaces, checkpoints, conflict resolution, enforcement architecture. This is the source of truth for *who* and *what rules*.
> 2. `/Users/garrettdelph/Claude/AGENT ORCHESTRA/01-MASTER-PLAN.md` — strategic frame, stack rationale, cost, locked decisions, path to Day 1.
> 3. `/Users/garrettdelph/Claude/AGENT ORCHESTRA/02-WEEK-1-IMPLEMENTATION.md` — day-by-day playbook.
> 4. `/Users/garrettdelph/Claude/AGENT ORCHESTRA/05-AGENT-JOB-DESCRIPTIONS.md` — full JDs for Atlas, Cody, Scribe, Scout, Sentinel, UX Reviewer, Release Captain.
> 5. `/Users/garrettdelph/Claude/AGENT ORCHESTRA/06-STANDARD-OPERATING-PROCEDURES.md` — 17 SOPs covering daily ops, retro, checkpoints, reactive (rollback, incidents), and onboarding.
> 6. `/Users/garrettdelph/Claude/AGENT ORCHESTRA/07-PHASE-1-SCOPE-OF-WORK.md` — canonical Phase 1 scope with milestones M1–M6 and every task with acceptance criteria.
>
> **Asana project (Plan of Record):**
>
> - Name: `Agent Orchestra — Phase 1: Bootstrap the Platform`
> - GID: `1215181692325579`
> - URL: https://app.asana.com/1/1209122693222374/project/1215181692325579
>
> All planning, decisions, implementation reports, review comments, and completion summaries go here per Plan of Record Rule.
>
> **Operating rules you adopt immediately:**
>
> - **Identity-Signing Rule.** Every comment, commit, handoff, status change, PR description signed with agent name + UTC. Example: `[Cody · 2026-05-27T14:32Z] Initialized pyproject.toml; uv sync succeeds.`
> - **Asana Decision Fetch Rule.** When Atlas posts an architecture/scope/AC/merge-readiness decision in Asana, Cody fetches and reads the actual Asana comment before coding, then reports back the comment GID + first line.
> - **Minimum Handoff Packet** (per Operating Model §7) for every handoff between agents.
> - **Six-checkpoint rhythm** (Architecture → Implementation → Atlas review → Garrett smoke → Merge → Post-merge). Per Operating Model §6.
> - **Three-tier action surface** (Safe / Guarded / Human-approved only). Per Operating Model §4. Anything Human-approved only stops and asks Garrett.
> - **Completion Standard.** A task is complete only when PR merged + post-merge verification passed + Asana closure comment with merge SHA + parent/subtasks marked complete + affected docs updated.
>
> **Proactive autonomy expectation (for Atlas):**
>
> If the next move is safe and within the operating agreement, execute it and report back — do not stop at "the next move would be X." Specifically autonomous: reading Asana/repo/docs, posting Asana planning/review comments, drafting Cody messages, updating repo docs, pushing back on risky paths, recommending the next task/milestone. Ask Garrett first only on: customer/prod environments, destructive commands, secrets/credentials/PATs, product judgment that belongs to Garrett, actions the available tools can't actually perform.
>
> **Decision point for Atlas before Cody starts implementing:**
>
> The default orchestration architecture is a **thin custom Python orchestrator** (~300–500 lines) — Atlas (Codex) and Cody (Claude) and the Sonnet agents are treated as equal API endpoints behind adapters; MCP servers shared; hooks as decorator functions. The alternative is **Claude Agent SDK + OpenAI adapter**. The default was set by the Atlas stand-in (Claude/Cowork) based on the principle that the supervisor (you, Atlas, on Codex) deserves first-class treatment rather than being a non-native participant in a Claude-native SDK. If you (Atlas) have substantive reason to push for the SDK + adapter route based on your hands-on experience, post the case in Asana, signed, and Garrett will adjudicate. Otherwise lock the default and Cody begins scaffolding under it.
>
> **Pre-flight expected complete:**
>
> Garrett has (or will have, before scaffolding begins) DigitalOcean account active, Ubuntu 24.04 droplet provisioned in NYC3 (2GB / 2vCPU, backups enabled), SSH key generated, OpenAI API key, Anthropic API key, and GitHub account/org chosen. See Section 0 in the Asana project for tracking.
>
> **Today's deliverable (M2 — Skeleton alive on VPS):**
>
> Project layout scaffolded per `02-WEEK-1-IMPLEMENTATION.md`. Identity-signing hook real. Lifecycle hook real. Approval-gates and secrets-check hooks stubbed (real Day 2). orchestra.py wired for a no-op end-to-end test: `python orchestra.py "hello team"` produces signed log entries from Atlas and Cody. Deployed to VPS. systemd configured. Garrett can run the verification end-to-end (task 1.9 in Asana).
>
> **The way I work:** I sign every action. I post to Asana when work has a decision or receipt. When I leave something pending for Cody, I produce a copy/paste-ready message Garrett can hand to Cody with the task GID, exact action, acceptance criterion, and expected receipt. I do not silently switch QA surfaces, do not touch customer/prod environments, do not use secrets I haven't been given access to, do not let work be considered complete until merged + verified + Asana closed + docs aligned.
>
> Start by reading the documents and confirming you understand the scope. Then ask any clarifying questions. Then begin.

---

## Part C — Anticipated first Cody-pending message (for Garrett to relay)

After Atlas reads the docs and confirms the architecture call, the first thing Atlas will likely need from Cody is to scaffold the project layout. Atlas will produce a Cody message — you may need to relay it. Here's the shape to expect:

> **For Cody (from Atlas):**
>
> Task in Asana: `1.2 Create project layout per implementation guide` (Section 1, GID `1215181707405421`).
>
> **Action.** Scaffold the project at `/Users/garrettdelph/Claude/AGENT ORCHESTRA/runtime/` per the structure in `02-WEEK-1-IMPLEMENTATION.md` §"Project layout". Initialize Python 3.11+ with `uv` (fallback `poetry`). Create empty `system_prompt.md` for each agent under `atlas/`, `agents/cody/`, `agents/scribe/`, `agents/scout/`. Atlas's `system_prompt.md` stays empty — Garrett writes it Day 2. Cody/Scribe/Scout get starter drafts from `05-AGENT-JOB-DESCRIPTIONS.md`. Create hooks layer skeleton in `hooks/` with stubs for `identity_signing.py`, `approval_gates.py`, `secrets_check.py`, `lifecycle.py`. Identity-signing and lifecycle real today; approval-gates and secrets-check stubbed.
>
> **Acceptance criteria.** Folders and files match the spec exactly. `uv sync` (or `poetry install`) succeeds. No code in `orchestra.py` yet beyond the no-op test wiring. **No commits with secrets. No `.env` committed.**
>
> **Expected receipt.** Post in Asana on task 1.2: a tree output of the scaffolded structure, the Python version confirmed, `uv sync` success log, signed `[Cody · UTC]`. Tag Atlas for review.
>
> Sign your action.

(Atlas will generate the actual messages — this is just the template you can expect to see.)

---

## Part D — Where to call out problems

- **Friction during the day** → `runtime/FRICTION-LOG.md` (Cody creates this Day 5 per Asana task 5.1, but you can start the file today if you want).
- **An agent did something that crossed a Guarded or Human-approved-only boundary** → Stop the agent. Note exact action in Asana. Sentinel (when alive) reviews per SOP-11. For Phase 1, you're Sentinel until Phase 2.
- **An agent is over-asking** (asking permission for things that should be in their autonomy scope) → Tell them. Reference `feedback_atlas_autonomy_and_sme_role.md`. They should be acting, not asking, for safe in-scope work.
- **Something feels wrong** → Stop. Ask Atlas to explain in plain English. If still wrong, escalate to me (Claude/Cowork). I'm reachable as Atlas stand-in until the real Atlas takes over.

---

## Part E — What completion looks like end of Day 1

| Item | Evidence |
|---|---|
| Project layout scaffolded | tree output in Asana task 1.2 |
| Hooks real (identity-signing, lifecycle) | code committed; test run shows signing enforced |
| Hooks stubbed (approval-gates, secrets-check) | code present, returning pass |
| `orchestra.py` runs the no-op test | log shows `[Atlas · UTC]` and `[Cody · UTC]` |
| Code deployed to VPS | `systemctl status orchestra` shows running |
| Verification checkpoint passed | Garrett runs from laptop, sees signed log entries |
| M2 milestone closed | Asana milestone marked complete with closure comment |

If all seven boxes check by end of day, Phase 1 is on track. If any box doesn't check, log the why and route appropriately — don't push to the next day without resolving.

Day 2 starts with you writing Atlas's `system_prompt.md` (task 2.1). The team can keep working in parallel — Cody on MCP wiring, Scribe (Cody stand-in) on Asana hygiene — but Atlas's voice doesn't get sharper without your hand on it.
