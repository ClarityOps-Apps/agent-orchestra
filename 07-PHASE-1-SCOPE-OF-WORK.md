# Agent Orchestra — Phase 1 Scope of Work

Author: Claude (Cowork, as Atlas stand-in)
For: Garrett
Date: May 27, 2026 (v1) · Rewritten 2026-06-02 (v2) · Updated 2026-06-02 (v3)
Status: v3 — Retool flight-deck path locked

> **Recalibration note.** v1 of this SOW scoped Phase 1 as "bootstrap + operating-model validation," with M4 framed as a manual PR cycle using me as Atlas stand-in. That delivers foundation work but does not deliver the autonomous orchestrator + flight-deck UI the Master Plan §3 actually describes. v2 corrected the gap: M4 became the autonomous orchestrator loop, M5 became the flight-deck UI, and old M5/M6 were renumbered M6/M7. v3 locks the UI path: M5 uses **Retool** as the flight-deck (no-code dashboard connecting to the runtime's REST API), shrinking the milestone from 2–4 weeks to 3–5 days. Custom FastAPI+React UI is deferred to a pre-GA phase. M4 gains task 4.11 (REST API endpoints) so Retool has something to connect to.

**Read first:** `00-OPERATING-MODEL.md`, `01-MASTER-PLAN.md`, `02-WEEK-1-IMPLEMENTATION.md`, `05-AGENT-JOB-DESCRIPTIONS.md`, `06-STANDARD-OPERATING-PROCEDURES.md`.

---

## Phase 1 Deliverable (revised)

A fully-running, **autonomously-orchestrating** Agent Orchestra platform with a **flight-deck UI**. The team (Atlas, Cody, Scribe, thin Scout) runs as LLM-driven agents inside the runtime on the DigitalOcean VPS. They coordinate through the runtime's orchestration loop without Garrett relaying messages. Garrett interacts with the platform via a Retool-hosted flight deck in his browser: gives directives, watches the team work, approves gates only at human-in-the-loop boundaries. The platform signs every action, respects the three-tier action surface, routes work through the Minimum Handoff Packet, and gates at the boundaries Atlas configured.

**Architecture:** DigitalOcean droplet `agent-orchestra-1` hosts the runtime, agents, hooks, MCP servers, and the REST API. Retool hosts the UI in its cloud and connects to the DO API over HTTPS. Single platform, two surfaces — backend on DO, frontend on Retool — connected by a documented REST contract.

What this *isn't*: a polished GA product. It's the personal-use alpha with autonomous orchestration and a working UI — proof that the platform delivers on the architectural promise. Custom-built UI (replacing Retool) is a pre-GA milestone for when productization becomes the priority.

## Milestones (revised)

| ID | Milestone | Status | Target |
|---|---|---|---|
| **M1** | Accounts & access ready | ✅ Closed 2026-05-28 | (foundation) |
| **M2** | Skeleton alive on VPS | ✅ Closed 2026-05-28 | (foundation) |
| **M3** | MCP wired + identity signing live | ✅ Closed 2026-06-01 | (foundation) |
| **M4** | **Autonomous orchestrator loop live** *(rescoped, includes REST API)* | Not yet started | ~1.5–2.5 weeks |
| **M5** | **Flight-deck UI MVP (Retool)** *(new milestone, Retool path)* | Not yet started | 3–5 days |
| **M6** | 24/7 operation hardened *(was M5)* | Not yet started | 2–3 days |
| **M7** | Phase 1 validated *(was M6)* | Not yet started | 1–2 days |

**Phase 1 total revised estimate:** 2.5–4 weeks from now to full Phase 1 completion. The Retool path shaves 2–3 weeks off M5 vs. a custom UI build.

---

## Foundation (M1–M3) — what was delivered

Captured here as a permanent record. The full task lists for these sections are preserved in the Asana project (`1215181692325579`) and in the conversation history.

**M1 — Accounts & access ready.** DigitalOcean account, VPS droplet (`agent-orchestra-1` at `159.89.86.113`), SSH key, OpenAI API key, Anthropic API key, GitHub org (`ClarityOps-Apps`), Asana project. Closed 2026-05-28.

**M2 — Skeleton alive on VPS.** `runtime/` scaffold, Python project, identity-signing hook (real), lifecycle hook (real), approval-gates and secrets-check hooks (stubbed for Phase 1, real in M4), `orchestra.py` no-op daemon, systemd service, VPS deploy via fresh git clone. Closed 2026-05-28.

**M3 — MCP wired + identity signing live.** Asana / GitHub / filesystem MCP configs, approval-gates and secrets-check hooks made real, Atlas system_prompt.md locked v1.1, Cody/Scribe/Scout system_prompts.md locked v1, Asana credentials provisioned to `.env`, integration test 2.9 passed (real Asana READ from runtime + signed comment WRITE). Closed 2026-06-01.

**What the foundation does not yet deliver:** the actual LLM-driven supervisor-calls-subagents loop, and the flight-deck UI. Those are M4 and M5.

---

## Section 3 — M4: Autonomous Orchestrator Loop *(re-scoped)*

**Owner:** Cody implementation; Atlas spec + review; Garrett verifies. **Closes M4.**

### What this milestone delivers

The runtime stops being a no-op daemon. `orchestra.py` actually loads Atlas as an LLM (Codex 5.5 High via OpenAI API) and Cody/Scribe/Scout as LLMs (Anthropic API). When Garrett gives a directive, Atlas reads it, decides what to do, calls subagents via Python function calls inside the runtime, holds shared state, and posts results. Subagents execute, return results, get reviewed by Atlas. Human-in-the-loop gates fire only at the three-tier action surface boundaries (per `00-OPERATING-MODEL.md` §4). Garrett experiences the team operating *autonomously* — no chat-window relay, no copy-paste between agents.

### Acceptance criteria

- `orchestra.py` instantiates Atlas as a real LLM call to OpenAI (model: Codex 5.5 High) using `runtime/atlas/system_prompt.md` as the system message.
- `orchestra.py` instantiates Cody, Scribe, Scout as real LLM calls to Anthropic (Claude Opus 4.6 for Cody; Claude Sonnet 4.6 for Scribe and Scout) using their respective system_prompts.
- An orchestration loop is implemented: Atlas receives a directive, decides which subagent to call, calls that subagent via a Python function (not chat relay), receives the subagent's response, repeats until the directive is satisfied or a gate fires.
- Persistent session state is implemented (SQLite memory at `runtime/memory/sessions.db`).
- The four hooks (identity_signing, approval_gates, secrets_check, lifecycle) fire correctly on every agent action.
- A CLI entry point: `python orchestra.py --directive "directive text here"` produces orchestrated work and signed receipts.
- A real end-to-end demonstration: Garrett gives a meaningful directive (e.g., "Cody, write `runtime/status.py` with a health-check command; Scout smoke-tests it; Atlas reviews"), the team executes autonomously without Garrett relaying, Garrett gets pinged at the merge gate only, and the work lands.

### Tasks

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| M4 milestone | — | All 4.x tasks closed; verification 4.10 passed | — |
| 4.1 Wire OpenAI API client in orchestra.py | Cody | OpenAI SDK installed; client instantiated; system prompt loaded from `runtime/atlas/system_prompt.md`; secrets read from `.env` (`OPENAI_API_KEY`); a test "ping" call returns a signed response | M3 |
| 4.2 Wire Anthropic API client in orchestra.py | Cody | Anthropic SDK installed; client instantiated per subagent; system prompts loaded from each agent's `system_prompt.md`; secrets read from `.env` (`ANTHROPIC_API_KEY`); test "ping" calls return signed responses | M3 |
| 4.3 Implement agent factory | Cody | A Python module that takes an agent name and returns a configured LLM client + system prompt + tool list. Returns Atlas (OpenAI) or Cody/Scribe/Scout (Anthropic) per name. | 4.1, 4.2 |
| 4.4 Implement message-passing protocol between agents | Cody | A Python function `agent.send(target, message)` that routes a signed message from one agent to another via runtime function call (not chat). Returns the target's signed response. | 4.3 |
| 4.5 Implement supervisor loop | Cody | The orchestration loop: Atlas receives a directive, parses for sub-tasks, calls subagents in sequence or parallel, holds state, decides when to escalate to Garrett at a gate. | 4.3, 4.4 |
| 4.6 Wire session state persistence | Cody | SQLite-backed session state. Atlas can resume work after a runtime restart. Memory survives crashes. | 4.3 |
| 4.7 Wire MCP tool access per agent | Cody | Each agent has the right MCP tools available (Atlas: Asana + GitHub read; Cody: full Asana + GitHub + filesystem; Scribe: Asana + filesystem; Scout: Asana + filesystem + bash for smoke scripts). Tool calls go through the hooks. | 4.3, 4.4 |
| 4.8 CLI entry point | Cody | `python orchestra.py --directive "..."` starts an orchestrated session, prints signed actions as they happen, exits when the directive is satisfied or a gate fires. | 4.5, 4.7 |
| 4.9 Real integration test: end-to-end orchestrated directive | Atlas (spec) + Cody (impl) + Scout (smoke) | Atlas writes a small spec (e.g., "implement `runtime/status.py` health check"); the runtime executes the spec autonomously; Garrett gets pinged at the merge gate; merges; team posts closure. All signed. | 4.8 |
| 4.10 ✅ Verification — autonomous orchestration | Garrett | Garrett gives a fresh directive via CLI, watches the team execute without his intervention except at gates, and confirms the experience matches the Master Plan §6 success criteria. | 4.9 |
| 4.11 Implement runtime REST API endpoints | Cody | FastAPI (or equivalent) app added to the runtime exposing: `POST /directive` (submit directive); `GET /agents` (loaded agents + last activity); `GET /activity` (recent signed actions); `GET /gates/pending` (pending approval gates); `POST /gates/{id}/decision` (approve/reject); `GET /decisions` (browse decision log). Authenticated via bearer token. Documented OpenAPI schema. | 4.5, 4.8 |

### Why this is M4

Until this lands, "Agent Orchestra is alive" means scaffolding is in place. After this lands, the platform actually does autonomous multi-agent orchestration — the architectural promise from the Master Plan §3 is delivered.

---

## Section 4 — M5: Flight-Deck UI MVP — Retool Path *(new milestone)*

**Owner:** Atlas (spec); Cody (Retool config); Scout (smoke); Garrett (account setup + final verification). **Closes M5.**

### What this milestone delivers

A Retool-hosted flight deck Garrett accesses via browser. Connects to the runtime's REST API (built in M4 task 4.11). Garrett gives directives in plain English, sees agent status at a glance, watches the activity feed, approves gates with a click, browses the decision log. The UI is functional, not polished — minimum-viable but real. No frontend code is written; everything is configured in Retool's no-code dashboard builder.

### Why Retool, not custom

A custom FastAPI+React UI would deliver the same experience but take 2–4 weeks of frontend work. Retool gives us the same five pages in 3–5 days of configuration. Trade-off: Retool is a third-party SaaS; you'd rebuild as custom when GA productization becomes the priority. For Phase 1 alpha (Garrett-only personal use), that trade-off is acceptable and saves multiple weeks.

### Acceptance criteria

- Retool account active under Garrett's email (`garrett@clarityops.co`).
- Retool app named `Agent Orchestra Flight Deck` with five pages:
  - **Directive input** — plain-English text field; submits to `POST /directive`.
  - **Agent status** — table showing each loaded agent, last activity timestamp, health indicator. Polls `GET /agents`.
  - **Activity feed** — chronological list of recent signed actions across the team. Polls `GET /activity`. Auto-refreshes every 10 seconds.
  - **Approval gates** — when `GET /gates/pending` returns a gate, the UI shows action, target, risk, rollback, and lets Garrett approve or reject via `POST /gates/{id}/decision`.
  - **Decision log viewer** — browsable history from `GET /decisions` with filters by date / agent / gate type.
- API resource configured in Retool pointing at the runtime's REST endpoint with bearer-token auth.
- All requests authenticated end-to-end (Retool → DO bearer token; bearer token stored only in Retool's secrets vault, never in app config).
- Demonstrably usable: Garrett completes a full directive-to-merge cycle entirely through Retool without touching Terminal or chat windows.

### Tasks

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| M5 milestone | — | All 5.x tasks closed; verification 5.7 passed | M4 (especially 4.11) |
| 5.1 Garrett creates Retool account and Free-tier workspace | Garrett | Retool account active at `garrett@clarityops.co`. Free tier confirmed (up to 5 users; sufficient for alpha). Workspace named `Clarity Ops` or similar. | M4 |
| 5.2 Generate runtime bearer token and add to Retool secrets | Garrett + Cody | Bearer token generated server-side on the droplet; copied once to Retool's secrets vault. Token never echoed in chat, Asana, or commits. Old token rotatable per the credential procedure. | 4.11 |
| 5.3 Configure Retool API resource pointing at the runtime | Cody (via Retool's no-code interface; Garrett executes the clicks since Cody can't currently use Retool's UI directly) | Retool's REST API resource named `agent-orchestra-api` configured with base URL = the droplet's public endpoint and bearer-token auth header. Test query (`GET /agents`) returns live data. | 5.1, 5.2, 4.11 |
| 5.4 Build the five Retool pages | Cody (writes the page specs; Garrett configures in Retool, or we hand off to an Atlas-stand-in for the click work) | Each page matches acceptance criteria above. Real data from the API. Responsive on phone + laptop. | 5.3 |
| 5.5 Configure Retool authentication for Garrett-only access | Garrett | Retool app set to private; Garrett's email is the only authorized user. Login via Retool's standard auth (email + password or SSO). | 5.4 |
| 5.6 Smoke test: full directive cycle via Retool | Scout (via the runtime; Garrett validates the UI side) | Scout runs a test directive entirely through the Retool UI: input → orchestration → gate → approval → merge. Every page touched. | 5.5 |
| 5.7 ✅ Verification — flight deck operational | Garrett | Garrett uses Retool to give a real directive, watches it execute, approves the gate, confirms the merge — all without Terminal or chat windows. | 5.6 |

### Why this is M5

The UI is what stops you from being the courier. Without it, the platform works autonomously *but* you still juggle Terminal + Asana + chat windows to interact with it. With M5, you have one Retool URL, one interface, one place to drive everything. Retool gets you there in days, not weeks.

### Future GA consideration

When you approach General Availability and the platform is shipping to clients, the Retool dependency becomes a constraint (clients need their own Retool accounts, or you stand up a multi-tenant Retool instance). At that point, the pre-GA milestone is to rebuild the flight deck as a custom FastAPI+React app served from the droplet — exactly what M5 v2 specified before we picked the Retool path. The rebuild is ~2–4 weeks of work scheduled separately from Phase 1.

---

## Section 5 — M6: 24/7 Operation Hardened *(was old M5)*

**Owner:** Cody implementation, Atlas review, Garrett verifies. **Closes M6.**

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| M6 milestone | — | All 6.x tasks closed; verification 6.6 passed | M5 |
| 6.1 Verify systemd auto-restart on simulated crash | Cody | Kill the orchestra process; verify restart within 60s; log confirms restart cause | M5 |
| 6.2 Confirm daily log digest email to Garrett works | Cody | Test email arrives at `garrett@clarityops.co` daily with prior-day signed activity summary | M5 |
| 6.3 Implement heartbeat alerting | Cody | Simulate Atlas stall; alert fires to Garrett within 10 min via email or text | M5 |
| 6.4 Document credential rotation procedure | Cody | Step-by-step procedure file in `runtime/docs/`; Garrett can rotate any credential in <10 min | M5 |
| 6.5 Add Garrett-requested monitoring | Cody | Whatever Garrett asks for after using the UI for a few days | M5 |
| 6.6 ✅ Verification — 24/7 operational without laptop | Garrett | Garrett powers off laptop; sends a directive from phone via the UI; team responds; next morning, team still running and healthy | 6.1, 6.2, 6.3 |

---

## Section 6 — M7: Phase 1 Validation *(was old M6)*

**Owner:** Atlas + Scribe-stand-in draft retro; Garrett validates. **Closes M7 → Phase 1 complete.**

| Task | Owner | Acceptance criteria | Dependencies |
|---|---|---|---|
| M7 milestone | — | All 7.x tasks closed; verification 7.7 passed | M6 |
| 7.1 Synthesize friction log into themes | Scribe (Cody stand-in until Scribe is alive) | Themes document drafted: repeated friction, near-misses, surprises, wins | Section 5 |
| 7.2 Atlas annotates themes with hypotheses | Atlas | Each theme has a "why" hypothesis and "what would change it" recommendation | 7.1 |
| 7.3 Garrett reviews retro and decides on changes | Garrett | Decisions logged in Asana: prompt edits, SOP edits, Operating Model edits, phase progression | 7.2 |
| 7.4 Evaluate Phase 2 trigger | Atlas | Recommendation in Asana on whether to start Phase 2 (Sentinel + deterministic hooks layer) immediately, defer, or replan | 7.3 |
| 7.5 Evaluate Routines as Phase 2+ complementary tool | Atlas | List of candidate Routines jobs with cost/value analysis | 7.3 |
| 7.6 Phase 1 deliverable verification against Master Plan §6 | Atlas + Garrett | All six Master Plan §6 success criteria confirmed met. Especially: "Open a single web interface and give Atlas a directive in plain English" — verified via M5. | 7.1, 7.2 |
| 7.7 ✅ Verification — Phase 1 validated | Garrett | Garrett confirms the platform delivers on the architectural promise. Phase 1 closes. | 7.6 |

---

## Section 7 — Ad-hoc / Discovered Work *(rolling buffer)*

Unchanged from v1. Catches work surfaced during build that doesn't fit a planned task.

---

## Change log

- **v1 · 2026-05-27** — Original SOW. Scoped Phase 1 as bootstrap + scaffolding + operating-model validation. M1–M3 closed under this version. M4–M6 in v1 did not deliver autonomous orchestration or a UI; that gap was implicit and not flagged.
- **v2 · 2026-06-02** — Rewrite. Garrett surfaced the gap that the autonomous orchestrator and flight-deck UI were never on a milestone with concrete acceptance criteria. v2 rescopes M4 as "autonomous orchestrator loop" and adds M5 as "flight-deck UI MVP" (custom FastAPI+React). Old M5/M6 renumbered M6/M7. Authored by Claude/Cowork on Garrett's authorization.
- **v3 · 2026-06-02** — UI path locked to Retool. Garrett's re-read of the original Gemini transcript surfaced that Gemini had explicitly recommended a dashboard UI from day one and named LangGraph Studio as the specific tool. Claude/Cowork had rejected LangGraph (correctly, for orchestration framework choice) but failed to flag that the rejection meant taking on a custom UI build cost. v3 corrects: M5 uses Retool (no-code dashboard) instead of custom FastAPI+React. M4 gains task 4.11 (REST API endpoints) so Retool has something to connect to. Phase 1 total estimate drops from 4–7 weeks (v2) to 2.5–4 weeks (v3). Custom UI build deferred to a pre-GA milestone outside Phase 1.
