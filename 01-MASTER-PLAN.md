# Agent Orchestra — Master Plan

Author: Claude (Cowork)
For: Garrett
Date: May 27, 2026
Status: v1.1 — updated to align with adopted Operating Model

> **Read order:** This document is the strategic frame. For the team, rules, and governance, see `00-OPERATING-MODEL.md` — that is the canonical source of truth for *who* and *what rules*. This file covers *why this stack*, *cost*, and *staged path*.

---

## 1. What you asked for

You want a single platform where a *team* of agents — not just two — can work together on your behalf, with bespoke job descriptions and decision rights, real human-in-the-loop checkpoints, and guardrails that all agents must adhere to. You want it running 24/7. You want it to feel like managing a team, not relaying messages between two coworkers. And you want it portable — personal use first, productizable later.

That's a sound product brief. The question is what to build, what to buy, and what order to do it in.

## 2. Evaluation of the advisor's recommendation

You asked me to challenge, test, and prove the advice from your meeting with Gemini. Here's the honest read.

### What Gemini got right

The directional advice is correct. Multi-agent orchestration is the right category. The supervisor-agent pattern (one agent acting as traffic cop) is a real, well-understood design. Human-in-the-loop gates are a valid and necessary control. Cloud hosting beats running off your laptop if you want 24/7 operation. Direct APIs are cheaper than subscriptions at low-to-moderate volume. Delegating the technical buildout to your existing coding agents is the right division of labor.

LangGraph itself is a serious, production-grade framework. It is not a bad answer. It's just probably not the *first* answer for you specifically.

### Where Gemini's recommendation fell short

**It never asked what's broken.** You described a working setup. Before recommending a multi-week build and a new subscription stack, the right diagnostic question is: *what specifically is the friction Codex and Claude Code can't solve today?* Gemini skipped that and jumped to the heaviest tool available.

**It defaulted to LangGraph without weighing simpler options.** Better-fit-for-your-profile alternatives that never came up: the Claude Agent SDK (Anthropic's own multi-agent framework, MCP-native, much shallower learning curve, you're already in this ecosystem), no-code orchestrators like n8n or Lindy, and the OpenAI Agents SDK. LangGraph is the right answer if you're a Python engineering team. For a solo founder who explicitly said he doesn't write code, it's a heavy first bet.

**It oversold "plain English."** LangGraph is a Python library. Studio is a visualizer on top of code you still own. Adding nodes, debugging stuck graphs, modifying conditional edges — that's Python work. You will own a Python codebase. Gemini half-admitted this then softened it.

**It conflated your existing products with API endpoints.** This is the most important miss. "Claude Code" and "Codex" are CLIs/desktop products. You cannot drop them into LangGraph. What you'd actually be calling is the raw Claude/OpenAI API — with **none** of the affordances that make Claude Code useful (skills, MCP, sandboxing, todos, sub-agent dispatch, hooks). You'd be rebuilding those inside LangGraph nodes. That cost was never priced into the recommendation.

**It underpriced the move off your $200/mo Claude Max subscription.** Moving to API-only means pay-per-token across multiple agents looping until tests pass. For your described workload — militant iteration, multi-agent QA — API-direct will likely run **$500–$2,000/month for a single user** before LangSmith ($39/seat) and LangGraph Platform fees. Gemini said "it depends." For your specific workload, it almost certainly costs more, not less.

**It missed MCP as the integration layer.** You mentioned MCP and the advisor didn't pick up on it. In 2026 the durable architecture is: MCP servers expose tools (Asana, GitHub, files) → orchestrator calls MCP → same servers work across any orchestrator. This makes your work portable across vendor changes. Whatever you pick should be MCP-native.

### Bottom line on the advisor's advice

Treat it as a valid *Stage 3* answer. Not the Stage 1 answer. You'd be making the right tool choice if you were two years and a small engineering team into this. You're not. Yet.

## 3. Recommended architecture

I'm recommending a **staged path** that solves all five of your pain points in days, not months, and leaves the door open to graduate to LangGraph if you genuinely need it later. Every stage uses MCP as the integration layer so nothing you build is throwaway when you move forward.

### Stage 1 — This week. Claude Agent SDK + MCP + Subagents + Hooks.

This is the build that runs this week. You already live in Claude's ecosystem and you already pay for it. The Claude Agent SDK gives you:

- A **supervisor agent** that delegates to subagents based on the task.
- **Subagents** with bespoke prompts, decision rights, and their own tool access (your "specialized team").
- **MCP servers** for Asana, GitHub, files, and anything else — these are the integration layer the whole architecture depends on.
- **Hooks** that fire on pre-tool-use, post-tool-use, agent-start, agent-stop — your guardrails live here.
- **Sessions and persistent memory** so context doesn't get lost across sessions.
- **A clean human-in-the-loop story** via `AskUserQuestion` and approval gates in hooks.

You can run this locally for the first 2–3 days to validate, then move to a small always-on host (a $5–20/mo VPS, or a dedicated Mac Mini if you want privacy) for 24/7 operation. The Agent SDK runs in any Python or Node environment — no platform lock-in.

### Stage 2 — Only if Stage 1 isn't enough. Deeper Agent SDK build.

If after a few weeks you've outgrown Stage 1, the upgrade is *inside the same SDK*: more subagents, a more formal supervisor, durable session state in a database (Postgres is fine), and a thin dashboard UI. You don't migrate platforms. You add layers.

### Stage 3 — Only if you need true cross-vendor orchestration. LangGraph.

LangGraph becomes the right answer when you genuinely need to mix Claude, GPT, Gemini, and open-source models in the same stateful workflow with complex conditional routing. That's a real use case — just not your day-1 problem.

### The architecture, in one picture

```
┌──────────────────────────────────────────────────────────────────────┐
│                       YOU (plain-English interface)                  │
│                  ▲                                    ▲               │
│                  │ approvals, gates                   │ status        │
└──────────────────┼────────────────────────────────────┼──────────────┘
                   │                                    │
┌──────────────────▼────────────────────────────────────┴──────────────┐
│                ATLAS  (Architect / Orchestrator)                     │
│   - Translates intent into spec, sequence, acceptance criteria        │
│   - Routes work; holds shared state; final technical approval         │
│   - Plain English to Garrett; dev-speak to peers                      │
└──┬────────┬──────────┬─────────┬──────────┬──────────┬───────────────┘
   │        │          │         │          │          │
   ▼        ▼          ▼         ▼          ▼          ▼
┌──────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌──────┐ ┌─────────────┐
│ CODY │ │SENTINEL│ │ SCOUT  │ │ SCRIBE │ │ UX   │ │  RELEASE    │
│ Impl │ │ Safety │ │   QA   │ │  Docs/ │ │ REV  │ │  CAPTAIN    │
│      │ │  Data  │ │ Smoke  │ │  Asana │ │ Polish│ │ Merge/Deploy│
└───┬──┘ └───┬────┘ └───┬────┘ └───┬────┘ └──┬───┘ └──────┬──────┘
    │       │           │          │          │           │
    └───────┴───────────┼──────────┴──────────┴───────────┘
                        ▼
         ┌──────────────────────────────────────┐
         │           MCP SERVERS (tools)        │
         │   Asana · GitHub · Supabase · Files  │
         └──────────────────────────────────────┘
                        ▲
         ┌──────────────────────────────────────┐
         │   HOOKS (deterministic enforcement)  │
         │   Signing · approval gates · secrets │
         │   No agent (incl. Atlas) overrides   │
         └──────────────────────────────────────┘
```

Three things to notice:

1. **Sentinel + hooks together form the safety architecture.** Sentinel is the reasoning agent (judgment, context, novel risks). Hooks are the deterministic substrate (signing, approval gates, secrets, no-merge-without-QA). Neither alone is sufficient — see `00-OPERATING-MODEL.md` §9.
2. **MCP is the integration boundary.** Every tool an agent uses goes through an MCP server. When you switch hosts, models, or orchestrators later, the MCP layer comes with you.
3. **Atlas is configuration, not infrastructure.** Its "job description" lives in a Markdown prompt file you can edit in plain English. You don't need to touch Python to change how the team is run.

**Phase 1 roster (this week):** Atlas, Cody, Scribe, and a thin Scout. Sentinel, full Scout, UX Reviewer, and Release Captain are added in later phases per `00-OPERATING-MODEL.md` §10.

## 4. Cost comparison

Let me put real numbers next to the choices, because this drove a lot of the Gemini conversation.

### Today (status quo)

Claude Max $200/mo + ChatGPT Plus (Codex) $20–30/mo + Asana = **~$230/mo all-in.**

### Gemini's recommendation (LangGraph + LangGraph Platform + LangSmith + API tokens)

- LangSmith Plus: $39/seat/mo
- LangGraph Platform: usage-based, expect $50–200/mo for a single user at moderate volume
- API tokens for 5 agents looping on QA: **$500–$2,000/mo** depending on model mix
- Time cost: 2–6 weeks of build before you have something running
- **Estimated all-in: $600–$2,300/mo + multi-week build**

### My recommendation (Stage 1: Agent SDK + MCP + small VPS)

- Keep Claude Max $200/mo (still useful for ad-hoc work)
- API tokens for orchestrated runs (Sonnet for routine, Opus for hard problems): expect **$100–$400/mo** at the start, scales with usage
- Small always-on VPS (DigitalOcean, Hetzner, etc.): **$5–20/mo**
- No additional observability spend at start (Agent SDK has built-in logging; add later if needed)
- Time cost: **days, not weeks**
- **Estimated all-in: $300–$650/mo + a week to build**

The Stage 1 path is roughly **half the monthly cost** and **5–10x faster to value** than the advisor's recommendation, while still solving all five pain points.

## 5. How this solves your five pain points

| Pain point | How Stage 1 solves it |
|---|---|
| 1. Tired of relaying messages between agents | Atlas owns delegation; Minimum Handoff Packet (§7 of Operating Model) ends courier work |
| 2. Context and decisions get lost across sessions | Agent SDK sessions + Asana as Plan of Record (§3 of Operating Model) |
| 3. Can't scale past 2 agents | Adding an agent = adding a prompt file + decision rights; no architecture change |
| 4. Wants 24/7 operation without laptop on | $5–20/mo VPS runs Atlas + team; your laptop is just a client |
| 5. Bespoke jobs, decision rights, human-in-the-loop, guardrails | Operating Model §2–4 define jobs and decision rights; §3, §8, §9 define enforcement |

## 6. What success looks like at end of Week 1

By end of this week, you should be able to:

- Open a single command-line or web interface and give Atlas a directive in plain English.
- Watch Atlas route the work — to Cody for implementation, to Scribe for documentation, to Scout for a smoke check.
- See your agents read from and write to Asana automatically, signed with name + UTC per the Identity-Signing Rule.
- Get pinged for approval before any human-approved-only action (per the three-tier action surface in `00-OPERATING-MODEL.md` §4).
- Read a clear activity log of what each agent did, when, and why.
- Kill any agent or pause the system instantly.

If we hit that bar by Friday, Phase 1 is validated and we know whether to expand to Phase 2 or hold and iterate.

## 7. What's in this folder

- `00-OPERATING-MODEL.md` — **source of truth** for the team, governance rules, action surfaces, checkpoints, handoff packet, conflict resolution, and phased rollout. Read first.
- `01-MASTER-PLAN.md` — this file. Strategic frame, advisor evaluation, stack rationale, cost.
- `02-WEEK-1-IMPLEMENTATION.md` — day-by-day playbook to stand up Phase 1.
- `03-AGENT-TEAM-AND-GUARDRAILS.md` — *deprecated*. Superseded by `00`. Preserved for history.
- `AGENT TEAM/Agentic Software Delivery Operating Model.docx` — Atlas's original draft of the Operating Model.
- Coming next: `05-AGENT-JOB-DESCRIPTIONS.md` and `06-STANDARD-OPERATING-PROCEDURES.md`.

Read order: `00` → `01` → `02`.

## 8. Decisions locked

All Phase 1 open questions are now closed:

- **Hosting:** DigitalOcean NYC3, 2GB / 2vCPU droplet ($12/mo) with backups ($2.40/mo). Skip-to-VPS from Day 1.
- **Phase 1 deliverable:** the platform itself (orchestrator runtime + agentic team running on the VPS, signing actions, gated approvals working end-to-end). No "first real project" in Phase 1 — that moves to Phase 2 planning when the team is alive.
- **Phase 1 roster:** Atlas, Cody, Scribe, thin Scout.
- **Agent mapping:** Atlas → Codex 5.5 High via OpenAI API · Cody → Claude (Opus or Sonnet) via Claude Agent SDK · Scribe → Sonnet · Scout → Sonnet.
- **Orchestration approach:** thin custom Python orchestrator (~300–500 lines). Validated with real Atlas at Day 1 kickoff.
- **Productization frame:** personal first; productize later at GA. Light repo hygiene from Day 1 (clean README, .env.example, sensible structure, no secrets in commits).
- **Routines:** parked. Phase 2+ evaluation as a complementary tool.
- **Operating standard for planning/prep:** Garrett's existing Atlas standard from Goal Chains 360 adopted in full — six-checkpoint rhythm, Asana Decision Fetch Rule, Cody-pending-message format, proactive autonomy. Claude (Cowork) operates as Atlas stand-in until real Atlas takes over at Day 1.

## 9. Path from here to Day 1

1. Planning docs updated for all locked decisions. ✅
2. `07-PHASE-1-SCOPE-OF-WORK.md` written. ✅
3. Agent Orchestra Asana project built directly via MCP. ✅
4. Day 1 kickoff package prepared. ✅
5. **Garrett pre-flight (parallel to above):**
   - Sign up for DigitalOcean, save SSH key.
   - Generate OpenAI API key for the runtime (Atlas's home).
   - Confirm Anthropic API access; generate API key for the runtime (Cody/Scribe/Scout's home).
   - Decide GitHub account/org for the Agent Orchestra repo.
6. **Day 1 kickoff:** Garrett hands the kickoff prompt + doc set to real Atlas and Cody. Atlas validates the thin-orchestrator architecture call. Cody scaffolds the project. Verification checkpoint at end of Day 1.
7. **Days 2–7** proceed per `02-WEEK-1-IMPLEMENTATION.md`.
