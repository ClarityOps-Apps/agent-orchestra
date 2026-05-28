# Agent Orchestra — Week 1 Implementation Guide

Author: Claude (Cowork)
For: Garrett
Date: May 27, 2026
Status: v1.1 — aligned to adopted Operating Model

> **Read `00-OPERATING-MODEL.md` first.** This guide assumes you've adopted the team (Atlas, Cody, Sentinel, Scout, Scribe, Release Captain, UX Reviewer), the governance rules, and the phased rollout. This file is *how* we build Phase 1 — not *who* or *why*.

This is the day-by-day playbook to get Phase 1 of Agent Orchestra running by end of week. You do the directing. Your existing coding agents do the building. Each day has a verification checkpoint *you* can run without writing code.

## Phase 1 roster (this week)

Per `00-OPERATING-MODEL.md` §10:

- **Atlas** — Architect / Orchestrator (combined)
- **Cody** — Implementation Engineer
- **Scribe** — Documentation / Memory / Asana
- **Scout (thin)** — QA / Smoke Tests (smoke-script-only for Phase 1; expands in Phase 3)

Not yet in Phase 1: Sentinel, full Scout, UX Reviewer, Release Captain. Phase 2 adds Sentinel + the deterministic hooks layer for safety. For Phase 1, hooks are limited to the minimum needed to enforce the Identity-Signing Rule and the Human Decision Gate.

## Stack decision (locked)

- **Orchestration:** Claude Agent SDK (Python)
- **Integration layer:** MCP servers — Asana, GitHub, filesystem, and Supabase as needed
- **Models:** Mixed-provider per Garrett's empirical preference. Atlas → **Codex 5.5 High via OpenAI API** (your best thinker). Cody → **Claude (Opus 4.6 or Sonnet 4.6) via Claude API in the Agent SDK** (your best coder). Scribe → **Claude Sonnet 4.6**. Scout → **Claude Sonnet 4.6**.
- **Runtime:** Skip-to-VPS from Day 1. DigitalOcean NYC3, 2GB / 2vCPU droplet (Ubuntu 24.04), $12/mo + $2.40/mo backups = $14.40/mo. Your laptop is the client, not the host.
- **Durable record:** Asana per Plan of Record Rule.
- **Working memory:** Local SQLite via Agent SDK sessions for Phase 1. Add Postgres in Phase 2 if needed.
- **Secrets:** `.env` file locally, environment variables on the VPS. **Never commit secrets.**

## Project layout

```
runtime/
├── atlas/
│   ├── system_prompt.md         # YOU write this (per Q3 of Master Plan §8)
│   └── atlas.py                 # Wires the prompt into the SDK; agents author this
├── agents/
│   ├── cody/
│   │   ├── system_prompt.md     # I draft from the JDs; you refine
│   │   └── cody.py
│   ├── scribe/
│   │   ├── system_prompt.md
│   │   └── scribe.py
│   └── scout/                   # Thin version — smoke scripts only
│       ├── system_prompt.md
│       └── scout.py
├── hooks/                       # Phase 1: identity signing + approval gates only
│   ├── identity_signing.py      # Enforces Identity-Signing Rule
│   ├── approval_gates.py        # Enforces Human Decision Gate for Human-approved-only actions
│   ├── secrets_check.py         # Blocks secrets in logs/commits/comments
│   └── lifecycle.py             # Agent start/stop logging
├── mcp/
│   ├── asana.json
│   ├── github.json
│   ├── filesystem.json
│   └── supabase.json
├── memory/
│   ├── sessions.db              # Agent SDK session state
│   └── decisions/               # Markdown log of every gated decision
├── packets/                     # Minimum Handoff Packets per §7 of Operating Model
│   └── template.md
├── .env.example                 # Template — never commit the real .env
├── README.md
└── orchestra.py                 # Entry point — `python orchestra.py "<directive>"`
```

The two things you'll personally edit a lot:
- `atlas/system_prompt.md` — Atlas's job description, written in your voice
- `00-OPERATING-MODEL.md` — the governance rules that get baked into every agent's prompt

Everything else is scaffolded by your coding agents.

---

## Day 1 (Wednesday) — Decisions & scaffolding

### Your work (15–30 minutes)

- Confirm Phase 1 roster (Atlas, Cody, Scribe, thin Scout) — already locked unless you want to change it.
- Answer the three open questions in `01-MASTER-PLAN.md` §8: cloud-vs-local for the week, first real project, which existing agent plays which Phase 1 role.

### Coding agents' work

Hand them this guide along with `00-OPERATING-MODEL.md`. Kickoff prompt:

> "Read `00-OPERATING-MODEL.md`, `01-MASTER-PLAN.md`, and `02-WEEK-1-IMPLEMENTATION.md` in that order. Scaffold the project layout above. Use the Claude Agent SDK. Initialize Python 3.11+ with `uv` (or `poetry` if `uv` isn't available). Create empty system prompts for Atlas, Cody, Scribe, and a thin Scout — Garrett writes Atlas's prompt himself; you draft starters for Cody, Scribe, and Scout from their JDs in §2 of the Operating Model and the (forthcoming) `05-AGENT-JOB-DESCRIPTIONS.md`. Create the hooks layer with stubs for identity_signing, approval_gates, secrets_check, and lifecycle — implement identity_signing and lifecycle real on Day 1; approval_gates and secrets_check can be no-ops until Day 3. Wire `orchestra.py` for a no-op end-to-end test: Garrett issues a directive → Atlas logs the directive (signed) → Atlas delegates a stub task to Cody → Cody returns 'ok' (signed). Don't connect MCP servers yet — that's Day 2."

### Verification checkpoint

Run `python orchestra.py "hello team"`. You should see two log entries — one from Atlas, one from Cody — each carrying a signature block in the format `[Atlas · 2026-05-27T...Z]`. No real work yet. Skeleton is alive, signing rule is enforced.

---

## Day 2 (Thursday) — Atlas prompt, MCP wiring, base hooks

### Your work (~1 hour)

Write Atlas's `system_prompt.md`. This is the only prompt you author from scratch (per the hybrid authoring choice). Copy the role definition from `00-OPERATING-MODEL.md` §2 as your starting point and expand it with anything specific to your work:

- How Atlas should speak to you (plain English, lead with outcome, no jargon unless asked)
- How Atlas should speak to peers (dev-speak fine; sign every action)
- Atlas's reasoning style preferences (challenge bad ideas, ship slow ship right, militant QA)
- Your hot-button issues (e.g., "never assume; verify by reading Asana current state")

Don't try to make it perfect. It will iterate all week.

### Coding agents' work

> "Connect MCP servers for Asana, GitHub, filesystem, and Supabase. Configure in `mcp/*.json` with `.env` for credentials. Test that Atlas can read the most recent task from the AGENT ORCHESTRA Asana project and that Cody can post a signed comment back. Then implement `hooks/approval_gates.py` and `hooks/secrets_check.py` — even if Phase 1 only catches a handful of cases, the framework needs to be live so we can extend in Phase 2 without retrofitting. Per §3 of the Operating Model, approval_gates blocks any tool call classified as Human-approved-only and routes the approval request through Atlas to Garrett. For Phase 1, the gate list is: production deploys, JLOOP database writes, secret rotation, external sends. Other guarded actions can pass with logging."

### Verification checkpoint

In your terminal, ask Atlas: *"Read the most recent task in our Asana project and post a comment from Cody that says Cody is online."* In Asana you should see a comment signed `[Cody · <timestamp>] Cody is online.` That confirms MCP is wired, identity signing is working end-to-end, and Atlas can route a real action.

---

## Day 3 (Friday) — Real run end-to-end

### Your work (30 minutes)

Review the hook code your coding agents wrote — not auditing Python, just confirming *behavior* matches `00-OPERATING-MODEL.md` §3, §4, §9. Ask the agents: "Walk me through how the production-deploy approval gate works. Show me the code path and explain it in plain English."

Then write Scribe's and Scout's system prompts using the JDs as your starting point — or have agents draft and you edit (per your hybrid authoring choice).

### Coding agents' work

> "Pick a real, small task from the AGENT ORCHESTRA Asana backlog. Run it end-to-end through the team per `00-OPERATING-MODEL.md` §5: Atlas translates the task into scope + acceptance criteria, Scribe records the Plan of Record in Asana, Cody implements and opens a PR with verification evidence per the Evidence Receipt Rule, Scout runs smoke checks against the workflow, Atlas approves in substance. Stop at the merge gate so Garrett can approve manually. Every action signed per the Identity-Signing Rule. Use the Minimum Handoff Packet template for all handoffs."

### Verification checkpoint

A real PR exists on a real repo, opened by Cody, with smoke-check receipts from Scout, with Asana fully reflecting the Plan of Record per Scribe, and with the merge blocked pending your approval. You receive a clear approval prompt with a one-paragraph summary. **You decide.**

If you reach this checkpoint, you have a working Phase 1 system. Take a screenshot. The rest of the week is hardening.

---

## Day 4 (Saturday) — Hardening + monitoring on VPS

Note: the VPS was provisioned and deployed to in Day 1 (skip-to-VPS decision). Day 4 is about hardening, not first deployment.

### Your work (30 minutes)

Review the monitoring outputs from the first three days. Decide which signals you want surfaced more aggressively.

### Coding agents' work

> "Harden the runtime on the droplet: confirm systemd auto-restart works on simulated crash; verify the daily log digest reaches Garrett; verify the heartbeat alerts when Atlas is unresponsive for >10 minutes. Add any monitoring Garrett requested after reviewing the first three days. Rotate any temporary credentials used during initial bring-up."

### Verification checkpoint

Power off your laptop. Send a directive to the team via your phone or another device. The team should respond. Simulated crash should auto-recover within 60 seconds. Tomorrow morning, it should still be running.

---

## Day 5 (Sunday) — Live with it

### Your work (the rest of the week)

Use the team. Give real directives. Watch what works and what doesn't. Keep a running list in a new file: `runtime/FRICTION-LOG.md`. Every time you have to relay a message manually, every time an agent does something stupid, every time you hit a missing capability — log it.

This is the data for the Phase 2 decision.

### Coding agents' work

Standby. They fix what you flag. No proactive expansion this day — goal is stability under real use.

---

## Day 6–7 (Mon–Tue Week 2) — Retro & Phase 2 decision

Reread `FRICTION-LOG.md`. Decide:

1. **Is Phase 1 good enough for daily use?** If yes, we expand inside Phase 1 (refine prompts, add an SOP for a new scenario) and only move to Phase 2 when a safety case actually demands it.
2. **Has any friction surfaced that needs Sentinel + hooks layer to solve?** Examples: a near-miss on secrets, an Asana write that should have been blocked, a Cody PR Atlas approved that broke prod. If yes, Phase 2 starts.
3. **Has Atlas's load become a bottleneck?** If yes, the Atlas split into separate Orchestrator + Architect agents goes on the Phase 3 list.
4. **Evaluate Claude Code Routines as a Phase 2+ complementary tool.** Candidate jobs: Scribe's weekly Asana hygiene sweep, Scout's morning smoke check, doc-drift detection on the Agent Orchestra repo. Do NOT migrate core architecture to Routines — they remain a delegated worker pattern only.

We write the retro together.

---

## What you do NOT do this week

- Do not write Python.
- Do not pick LangGraph.
- Do not commit secrets.
- Do not add more than four agents on Day 1.
- Do not skip the verification checkpoints.
- Do not let the team merge code, deploy to prod, or touch JLOOP without your approval.

## What to hand your coding agents on Day 1

Copy-paste this:

> "I'm standing up Phase 1 of Agent Orchestra. Read these documents in this order: `00-OPERATING-MODEL.md` (canonical team and rules), `01-MASTER-PLAN.md` (strategic frame and stack rationale), `02-WEEK-1-IMPLEMENTATION.md` (day-by-day playbook), and the (forthcoming) `05-AGENT-JOB-DESCRIPTIONS.md`. Then ask me clarifying questions. Then start Day 1. Sign every comment, commit, handoff, and Asana update per the Identity-Signing Rule (§3 of Operating Model). Respect every governance rule in §3 and every action surface in §4. If you're about to take a Human-approved-only action, stop and ask me first."

That's the kickoff prompt.

## Cost expectation for Week 1

- VPS (Thursday on): ~$3 prorated for the week
- API tokens (Sonnet for most, occasional Opus for Atlas): **$15–60 for the week** at moderate usage
- Existing Claude Max subscription: unchanged
- Asana, GitHub, Supabase: unchanged

Total Week 1 incremental cost: **under $100**. Likely under $50.

## If something breaks at 2am

1. The team itself should self-heal via Atlas's retry logic for non-gated actions and Scribe logging the failure to Asana.
2. SSH to the droplet and check `journalctl -u orchestra`. The log is the truth.
3. If you can't reason about the log, screenshot it and paste it into a Claude Code session: *"What does this mean and how do I fix it?"* This is exactly what Claude Code is good at.

You should not be alone with a Python traceback at 2am. That's what your coding agents are for.
