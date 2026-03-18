# ARIA — Technical Documentation

> **Structure reference:** This document follows `/DOCS_STRUCTURE.md` exactly.
> Every section is written in the same order as that outline.
> High-level narrative comes first in each section; implementation depth follows in subsections.
> Designed to serve as both a **Vector RAG** source (each `###` block is a self-contained embedding chunk)
> and a **PageIndex** tree (section titles + summaries enable LLM-driven hierarchy navigation).

---

# 1. System Overview

## 1.1 What ARIA Is

ARIA (Autonomous Rider Intelligence & Analytics System) is a portfolio project built for an AI Engineer role at Loadshare Networks. It is a full-stack, production-grade autonomous operations platform that continuously monitors a fleet of gig-economy delivery riders, detects emerging risks — dead zones, earnings collapse, restaurant delays, churn signals — and synthesises cross-domain insights into human-readable briefings every 15 simulated minutes.

At a high level ARIA does four things in every cycle:

1. **Observes** — reads live zone density, restaurant queue depth, rider session health, and pending order risk from a PostgreSQL/TimescaleDB database and Redis cache.
2. **Computes** — algorithmic modules and four XGBoost ML models score every entity (zone, restaurant, order, rider) with mathematically grounded risk scores.
3. **Reasons** — five LangGraph agents synthesise those scores, apply domain rules, write structured alerts to the database, and the Supervisor agent produces a natural-language situation briefing using an LLM.
4. **Delivers** — the briefing and live event stream reach a Next.js dashboard (Vercel-hosted) via a WebSocket connection tunnelled through Cloudflare, and also to Claude Desktop as a set of MCP tools.

The system is designed to demonstrate that production-grade AI engineering is not just about calling an LLM — it is about the full stack of data pipelines, ML models, algorithmic modules, agent orchestration, real-time infrastructure, and frontend delivery, with the LLM playing a precise and bounded role within a larger deterministic system.

---

## 1.2 The Problem It Solves

### The Loadshare 2023 Research Context

ARIA's architecture is a direct response to the **April 2023 rider retention crisis at Loadshare**, documented in research publicly published in January 2025 by **Arun Ravichandran (Ex Senior Program Manager, Loadshare Networks)** on Medium. The article is titled "How We Solved the Rider Retention Crisis" and quantifies, in concrete operational terms, the systemic failures that caused rider churn to peak at 30% and EPH to collapse to Rs.70–85/hr against rider expectations of Rs.90–100/hr.

The crisis had a structural cause: the existing operations tooling was **static and rule-based**. Zone assignments were fixed. Restaurants were flagged only after repeated manual escalations. Dead runs were identified post-mortem, not prevented. Earnings trajectory was not monitored until a rider had already churned. Operations managers were reactive — they addressed problems they could see in hindsight, not problems that were forming in real time.

ARIA is engineered to automate the **next evolution** of the solutions Loadshare discovered — moving beyond static, rule-based operations into an **autonomous control plane** that addresses the specific intelligence failures the article documents:

| Intelligence Failure | ARIA's Response |
|---|---|
| Static zone assignments stranding riders in peripheral dead zones | Zone Intelligence agent: live sister-zone ranking each cycle, dynamic repositioning recommendations |
| No real-time restaurant delay detection | Restaurant Intelligence agent: z-score deviation on live queue against per-restaurant per-hour historical baseline |
| Dead runs identified after the fact | Dead Run Prevention agent: Model 3 scores every active order's destination zone before stranding occurs |
| EPH collapse detected only at session end | Earnings Guardian agent: trajectory forecasting at 15-minute cadence with churn signal 3 sessions ahead |
| No cross-domain synthesis for ops managers | Supervisor agent: cross-agent pattern detection, LLM briefing, and RAG-grounded recommendations |

The article identifies three compounding failure modes that ARIA specifically targets:

**1. Dead Zone Accumulation**
A dead zone is a geographic area with active riders but no incoming orders. A rider stranded in a dead zone earns nothing for the duration of the stranding. At Loadshare's scale, even a 15-minute stranding across a moderate fleet causes measurable EPH degradation. The article reports that peripheral zones are disproportionately affected because order density follows a hub-commercial-residential-peripheral gradient.

**2. EPH Collapse and Churn**
The Loadshare article quantifies rider earnings expectations: supplementary riders (part-time) target Rs.90/hr; dedicated riders (full-time) target Rs.100/hr. When a rider's EPH falls below threshold for three consecutive sessions, churn probability rises sharply — the article anchors this at retention dropping to ~30% once the three-session signal fires. ARIA's Earnings Guardian agent is built directly around this signal.

**3. Restaurant Delay Ripple**
When a restaurant experiences a prep-time spike (kitchen overload, surge order volume), riders assigned to that restaurant wait beyond their expected pickup window. This idle time is economically equivalent to a dead run — the rider earns nothing during the wait. If this pattern is not detected early, the same restaurant continues to receive orders, compounding the problem across the fleet.

### Why Simulation

Loadshare's live operational data is not publicly available. ARIA uses a simulation engine that generates realistic synthetic operational states — rider sessions, orders, zone density, restaurant queues — grounded in the statistics published in the Medium article "How We Solved the Rider Retention Crisis" by **Arun Ravichandran (Ex Senior Program Manager, Loadshare Networks)**, January 2025. This is not a shortcut: the simulation is sophisticated enough that it produces the exact failure modes described above at realistic rates, allowing the full agent stack to be exercised and verified without needing access to proprietary data.

---

## 1.3 Architecture Overview

ARIA is structured as six layers, each with a clear responsibility boundary. No layer reaches across more than one layer boundary.

**All six layers at a glance:**
1. **Client** — Next.js 14 dashboard (Vercel), React Flow pipeline diagram, Leaflet zone map, WebSocket, and Claude Desktop via MCP protocol.
2. **MCP Server** — FastAPI on port 8001 (only public-facing port). 14 MCP tools, APScheduler autonomous 15-min agent cycle, WebSocket server with Redis bridge.
3. **Intelligence** — 5 LangGraph agents (Zone, Restaurant, Dead Run, Earnings Guardian, Supervisor). LLM is Qwen2.5-32B-Instruct-GPTQ-Int4 served by vLLM. 4–6 LLM calls per cycle total.
4. **Event Stream** — FastAPI on port 8003 (internal). SimClock (time compression), Simulator (rider lifecycle, order factory, dispatcher), zone density snapshot engine. Publishes to Redis pub/sub.
5. **ML Server** — FastAPI on port 8002 (Docker-internal only, no host port). 4 XGBoost models, 3 algorithmic modules (zone, session, restaurant). Authenticated via X-Internal-Key.
6. **Data** — PostgreSQL 16 with TimescaleDB (hypertables + continuous aggregates), PostGIS (zone geometry), pgvector (4096-dim embeddings for Supervisor episodic memory and docs RAG). Redis 7 as zone density cache and pub/sub bus.

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1 — Client                                               │
│  Next.js 14 dashboard (Vercel)                                  │
│  React Flow pipeline diagram · Leaflet zone map · WebSocket     │
│  Claude Desktop via MCP protocol (/mcp endpoint)                │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTPS + WSS (Cloudflare Tunnel)
┌────────────────────────────▼────────────────────────────────────┐
│  Layer 2 — MCP Server  (FastAPI · port 8001 · public)           │
│  14 MCP tools (read-only DB queries)                            │
│  APScheduler: autonomous 15-min agent cycle                     │
│  WebSocket server: cycle events + Redis bridge                  │
└──────────┬────────────────────────────────────┬─────────────────┘
           │ asyncpg                             │ Redis pub/sub
┌──────────▼──────────────┐        ┌────────────▼────────────────┐
│  Layer 3 — Intelligence │        │  Layer 4 — Event Stream     │
│  5 LangGraph agents     │        │  FastAPI · port 8003        │
│  Qwen2.5-32B via vLLM   │        │  SimClock, Simulator        │
│  Supervisor + RAG       │        │  Order factory, Dispatcher  │
└──────────┬──────────────┘        └────────────┬────────────────┘
           │ httpx (internal)                   │ asyncpg + Redis
┌──────────▼──────────────┐        ┌────────────▼────────────────┐
│  Layer 5 — ML Server    │        │  Layer 6 — Data             │
│  FastAPI · port 8002    │        │  PostgreSQL 16              │
│  4 XGBoost models       │        │  TimescaleDB + PostGIS      │
│  3 algorithmic modules  │        │  Redis 7                    │
│  Internal Docker only   │        │                             │
└─────────────────────────┘        └─────────────────────────────┘
```

**Layer 1 — Client.** A Next.js 14 application deployed on Vercel with `basePath="/aria"`. The dashboard surfaces six data panels (zone map, cycle briefing, rider interventions, restaurant risk, dead run prevention, cycle history), a live KPI strip, and a React Flow animated pipeline diagram showing the agent execution state in real time. Claude Desktop users connect to the MCP endpoint at `/mcp`, giving them natural-language access to all 14 tools.

**Layer 2 — MCP Server.** The central coordination layer. Three subsystems run in the same FastAPI process: the MCP protocol layer (14 tools auto-generated by `fastapi-mcp` from `/tools/*` routes), the APScheduler cycle engine (fires every `CYCLE_INTERVAL_MINS` sim-minutes, runs all five agents, broadcasts results), and the WebSocket server (serves live events to the dashboard). This is the only service with a public-facing port.

**Layer 3 — Intelligence.** Five LangGraph agents instantiated inside the scheduler's cycle function. Each agent runs a directed acyclic graph of nodes: data-fetch → compute-call → LLM-synthesise → write-alerts. The LLM (Qwen2.5-32B-Instruct-GPTQ-Int4 served by vLLM) is called 4–6 times per cycle total, not once per agent per entity.

**Layer 4 — Event Stream.** A separate FastAPI service on port 8003. Houses the simulation engine: the `SimClock` (compressed time), the `Simulator` (rider lifecycle, dispatcher, order factory), and the zone density snapshot engine. Publishes live events to Redis pub/sub channels (`zone_updates`, `session_updates`, `order_updates`). The MCP Server bridges these to WebSocket clients.

**Layer 5 — ML Server.** Four XGBoost models behind a FastAPI service on port 8002. No host port mapping — Docker-internal only, authenticated via `X-Internal-Key`. The MCP Server's agents call this service via httpx. Three algorithmic modules (zone, session, restaurant) also live here and perform deterministic computations that do not need ML.

**Layer 6 — Data.** PostgreSQL 16 with three extensions: TimescaleDB (hypertables + continuous aggregates), PostGIS (zone geometry), and pgvector (1536-dim embeddings for Supervisor episodic memory). Redis 7 serves as both a zone density cache (avoiding 540+ queries per cycle) and a pub/sub bus for real-time event propagation.

---

## 1.4 Why This Stack

Each technology choice is deliberate and has a specific technical justification. This section covers the "why one over another" reasoning for the major components.

### Python + FastAPI vs Django/Flask/Node.js

FastAPI was chosen over Django REST Framework because ARIA's backend is entirely async — every database call uses asyncpg, every HTTP call uses httpx, and the event loop is the same one that drives APScheduler and WebSocket management. Django's synchronous ORM would require thread pools and sync-to-async bridging that adds latency and complexity for no benefit in a greenfield project. Flask lacks native async support. Node.js was ruled out because the ML/agent ecosystem — LangGraph, XGBoost, scikit-learn, SHAP — is Python-native with no equivalent in Node.

### PostgreSQL + TimescaleDB vs Pure Redis vs InfluxDB

The core reason for TimescaleDB over plain PostgreSQL is **continuous aggregates**. The Zone Intelligence agent needs a 28-day hourly average density baseline per zone, computed in milliseconds during every 15-second cycle window. TimescaleDB's continuous aggregate materialises this as a pre-computed view that updates incrementally — a plain PostgreSQL query over 28 days × 180 zones × 96 five-minute buckets would take seconds, not milliseconds. InfluxDB was not chosen because ARIA's data model is relational — riders, zones, restaurants, orders are entities with foreign keys, not pure time series. Redis alone cannot provide the complex joins and historical aggregations required.

### Redis vs In-Process Cache

Zone density data is written by the Event Stream service and read by agents in the MCP Server. These are separate processes. An in-process dict would not be shared across the process boundary. Redis also provides pub/sub, which drives the WebSocket bridge — solving two problems with one service.

### LangGraph vs Raw Python Loops vs LlamaIndex

LangGraph provides a directed graph abstraction over async agent execution, with explicit node boundaries, state passing, and error isolation. The key advantage over raw Python loops is **node-level observability via LangSmith** — each node's inputs, outputs, and latency are traced automatically. LlamaIndex was not used because ARIA's agents are not primarily retrieval-augmented document Q&A; they are structured data processors that call an LLM for a specific synthesis step. LangGraph's graph model fits naturally: fetch data (node 1) → run algorithms (node 2) → call LLM (node 3) → write DB (node 4).

### vLLM vs Ollama vs OpenAI API

vLLM was chosen for three specific reasons: (1) **tensor parallel support** — ARIA uses two RTX 3090s (48GB VRAM total) and a 32B quantized model requires tensor parallelism; (2) **OpenAI-compatible API** — LangChain's `ChatOpenAI` class works without modification by pointing the base URL at vLLM; (3) **continuous batching** — handles the 4–6 concurrent LLM calls within a cycle without queuing them serially. Ollama was considered but does not support multi-GPU tensor parallelism at the same performance level. The OpenAI API was ruled out for an on-premise system where data should not leave the local network.

### Cloudflare Tunnel vs Nginx Reverse Proxy vs ngrok

Cloudflare Tunnel requires **zero open inbound ports** on the host machine. The tunnel process runs outbound-only, connecting to Cloudflare's edge. This means the host firewall (UFW) blocks all inbound connections except on port 8001, which is not actually reachable from the internet — only the tunnel process reaches it from within the Docker network. Nginx would require exposing a port. ngrok adds a paid tier for custom domains and does not support persistent WebSocket connections reliably.

### Vercel vs Self-Hosting vs AWS Amplify

Vercel has zero-config Next.js deployment: push to master → deploy. ARIA's frontend is a static + serverless Next.js app with no server-side state; it calls the backend via the Cloudflare tunnel URL. Self-hosting would require managing an Nginx server for the frontend alongside the backend. The one trade-off with Vercel is that `basePath` does not auto-prefix `fetch()` calls — all client-side API calls must explicitly include `/aria/api/*`. This is a known Next.js behaviour, not a Vercel limitation.

### XGBoost vs LightGBM vs Neural Networks

Covered in depth in **Section 5 (ML Models)**. Short version: tabular data + small-to-medium dataset size + SHAP interpretability requirement + interview explainability → XGBoost wins consistently.

---

## 1.5 Key Design Principle: Agents Explain, Algorithms Compute

This is the most important architectural decision in ARIA and the one that separates a production-grade system from a "GPT wrapper."

**The rule:** No LLM is ever used to produce a number, a score, a threshold decision, or a risk classification. All quantitative outputs are produced by the algorithmic modules (pure math, deterministic) or XGBoost models (trained, calibrated, reproducible). The LLM's only job is to **synthesise** structured numbers into human-readable narratives and **reason** at the boundary where rule-based logic cannot handle edge cases.

**Concretely:**

| Task | Who does it | Why |
|------|-------------|-----|
| Compute zone stress ratio | Zone algorithm | Deterministic formula, audit-able |
| Score restaurant delay risk | Restaurant algorithm (z-score → sigmoid) | Reproducible, no hallucination risk |
| Predict dead zone probability | Model 3 (XGBoost + calibration) | Calibrated probabilities, SHAP-explainable |
| Forecast rider EPH trajectory | Model 4 (XGBoost two-stage) | Objective, trained on historical patterns |
| Decide if a rider needs intervention | Earnings agent (rule-based threshold gates) | Deterministic, consistent |
| Write "Rider 0042 is at churn risk because..." | Earnings Guardian LLM node | Synthesises zone + EPH + dead run context |
| Detect compound pressure patterns | Supervisor (deterministic) | Ratio + floor dual trigger, no LLM |
| Write the situation briefing | Supervisor LLM node | Cross-domain synthesis is LLM's core value |

**Why this matters:** An LLM asked to produce a risk score will hallucinate plausible-but-wrong numbers, change its output on identical inputs, and cannot be unit-tested. An XGBoost model with a calibrated threshold produces the same output for the same input every time, has a confusion matrix, and is auditable via SHAP feature importances. Keeping computation deterministic and pushing the LLM to the synthesis step means the system can be debugged, tested, and trusted.

**The one exception:** The Supervisor's Supervisor pattern detection is entirely deterministic — no LLM is involved. The LLM only receives the *result* of that detection (a list of pattern names, severity, financial KPIs) and writes a prose summary of it. The LLM never decides whether a pattern exists; it only explains one that has already been confirmed by deterministic logic.

---

## 1.6 Quick Reference — Commonly Asked Questions

This section provides dense, direct answers to the most common technical interview questions about ARIA. Each subsection is a standalone Q&A.

### Q: Where did the idea for ARIA come from? What is the source of the problem?

**Loadshare Networks is a real Indian logistics and supply chain company.** ARIA's entire problem statement comes from a real published research article: **"How We Solved the Rider Retention Crisis"** by **Arun Ravichandran (Ex Senior Program Manager, Loadshare Networks)**, published on Medium in January 2025. Loadshare is a real Indian logistics company. The article documented a rider retention crisis that peaked in **April 2023**, where rider churn hit 30% and Earnings Per Hour (EPH) collapsed to Rs.70–85/hr against rider expectations of Rs.90–100/hr. The root cause was static, rule-based operations tooling: fixed zone assignments, no real-time restaurant delay detection, dead runs identified post-mortem, and no earnings trajectory monitoring. ARIA is built as an autonomous AI-driven control plane that addresses each of these specific failures — it is a direct engineering response to the problems documented in that article, using Loadshare's own published statistics as the ground truth for synthetic data generation and model training thresholds.

### Q: What is MCP (Model Context Protocol) and how does Claude Desktop connect?

MCP — Model Context Protocol — is an open standard developed by Anthropic that lets AI assistants (like Claude Desktop) call external tools via a defined protocol. ARIA runs an MCP server using `fastapi-mcp`, which auto-generates 14 tool definitions from FastAPI route docstrings. When Claude Desktop connects to ARIA's `/mcp` endpoint, it can query live zone density, rider health, cycle briefings, dead run risk scores, restaurant alerts, and operator alerts using natural language. The MCP API key is injected server-side so it is never exposed to the browser.

### Q: What is the full tech stack — every service, language, library, and database?

Backend: Python 3.11, FastAPI, asyncpg, httpx, APScheduler, LangGraph, LangChain, `fastapi-mcp`. ML: XGBoost, scikit-learn, joblib, SHAP. LLM inference: Qwen2.5-32B-Instruct-GPTQ-Int4 served by vLLM. Embeddings: `qwen3-embedding` (4096-dim, MTEB #1) via Ollama — used for both the Supervisor episodic memory RAG and the documentation chatbot vector search. Databases: PostgreSQL 16 + TimescaleDB + PostGIS + pgvector. Cache/pubsub: Redis 7. Frontend: Next.js 14, Tailwind CSS, React Flow (pipeline diagram), Leaflet.js (zone map). Infrastructure: Docker Compose (8 services), Cloudflare Tunnel, Vercel (frontend). Hardware: Dual RTX 3090 (48GB VRAM total), i7 CPU, 128GB RAM. Languages: Python 3.11 for all backend services, TypeScript for frontend.

### Q: How does the WebSocket live dashboard work end to end?

The real-time dashboard uses a two-source WebSocket push model. Source 1: APScheduler fires `run_cycle()` every 15 sim-minutes → 5 agents run sequentially → `ws_manager.broadcast(cycle_complete)` pushes the briefing to all connected frontends. Source 2: the event-stream service publishes zone density updates, session opens/closes, and order status changes to Redis pub/sub channels (`aria:zone_updates`, `aria:session_updates`, `aria:order_updates`) → the MCP server's Redis bridge task (`start_redis_bridge()`) reads these and calls `ws_manager.broadcast()` → live events reach the frontend. The frontend animates the pipeline diagram on `cycle_start` and refreshes all KPI panels on `cycle_complete`. Dead connections are cleaned up lazily on the next broadcast call.

### Q: What is EPH and how does the Earnings Guardian decide when to escalate a rider?

EPH (Earnings Per Hour) is the primary health metric per rider, computed as `orders_completed × avg_fare / elapsed_hours`. `EPH_THRESHOLD = Rs.90` is the unified runtime alert boundary. The Earnings Guardian runs Model 4 (XGBoost two-stage: regressor → classifier) to predict each rider's `projected_final_eph` and `below_threshold` flag. Alert escalation logic: if `projected < Rs.80` OR (`below_threshold=True` AND `eph_trend = "declining"`) → `alert_level = "intervene"` (immediate ops escalation). If `projected < Rs.90` OR `eph_trend = "declining"` → `alert_level = "watch"`. Fleet-level: if `churn_risk_count / active_riders ≥ 15%` → fleet-wide `churn_surge` operator alert fires to the Supervisor. First 20 minutes of a session are always suppressed. Churn risk cooldown: 2 hours. Earnings below threshold cooldown: 30 minutes.

### Q: How does Model 4 avoid the shortcut of using EPH target as label leakage?

Model 4 is two separate models: a regressor predicting `projected_final_eph` (continuous) and a classifier predicting `below_threshold` (binary). The `eph_target` feature is intentionally excluded from the classifier's feature set. If included, the classifier would learn "high eph_target → predict below_threshold=1" — a spurious shortcut reading the label definition rather than learning trajectory. The regressor does include `eph_target` because knowing a rider's target (Rs.95 vs Rs.90) genuinely informs projected earnings; dedicated riders work harder to hit higher targets. Training labels use higher thresholds (Rs.95 supp, Rs.110 ded) than the runtime alert threshold (Rs.90 unified) — the model learns full-target health, not just the alert boundary.

### Q: How does the Supervisor episodic memory RAG ground its LLM prompt with past outcomes?

The Supervisor's `retrieve_context` node embeds the current situation as a canonical deterministic string (`severity + patterns + KPIs`) using `qwen3-embedding` (4096-dim, Ollama, MTEB #1). It queries `supervisor_episode_memory` using a hybrid multi-stage filter: (1) recency — last 30 days; (2) severity adjacency — adjacent severity levels only; (3) city/zone overlap — array `&&` operator on `city[]`; (4) embedding cosine similarity ≥ 0.65 via pgvector exact scan; (5) minimum support ≥ 2 episodes. Results are ranked by `effectiveness_score = patterns_resolved / actionable_patterns` — outcome-grounded ranking, not pure similarity. Top-3 episode snippets (capped at 1200 chars total) are injected into the LLM's system prompt. If no episodes pass all filters, the Supervisor runs in non-RAG mode.

### Q: What are the 4 ML models in ARIA, what do they predict, and how were they trained?

ARIA has four XGBoost models: **Model 1** (Rider Persona Classifier) — binary classifier, predicts whether a rider is supplementary or dedicated based on session patterns; trained on synthetic data grounded in Loadshare article statistics. **Model 2** (Delivery Duration Scorer) — XGBoost regressor, predicts `Time_taken(min)` per delivery; the only model trained on real data (Kaggle `gauravmalik26` food delivery dataset, 41,953 rows, 22 Indian cities). **Model 3** (Dead Zone Risk Predictor) — two-stage model: XGBoost classifier (is this a dead zone at all?) + XGBoost regressor (how risky is it?); uses interaction features `peripheral_ld_risk` and `dist_x_dead_rate` computed at inference. Calibrated with `CalibratedClassifierCV` (isotonic regression) for reliable probability outputs. **Model 4** (Earnings Trajectory Forecaster) — two-stage: regressor (forecast future EPH) + classifier (will EPH fall below 90?); uses momentum features `eph_slope` and `eph_acceleration` computed from lag EPH fields. All models use SHAP importances pre-computed at training time and served from `shap_importance.json` (never recomputed at inference). All model artifacts stored in `services/ml-server/models/`, served via internal FastAPI on port 8002.

### Q: How does the documentation chatbot work — what is PageIndex and how does it differ from vector RAG?

ARIA's documentation chatbot supports two retrieval modes. **Vector RAG**: the user's query is embedded with `qwen3-embedding` (asymmetric retrieval — query gets an instruction prefix, documents do not), then pgvector cosine similarity finds the top-5 most similar documentation chunks from the `docs_chunks` table, and those chunks are fed to the LLM for generation. **PageIndex** (structure-aware RAG): the documentation's markdown header hierarchy is parsed into a tree of nodes (level-0 chapters, level-1 sections, level-2 subsections). Instead of embedding similarity, the LLM navigates the tree in three steps: (L0) shown ~12 chapter titles, picks 1-2 most relevant chapters; (L1) shown all section titles within those chapters, picks 1-3 most relevant sections; (L2) collects those sections and their subsection children. The resulting nodes are passed as context to the LLM for generation. PageIndex requires no embedding at query time and works well for structured navigation queries; vector RAG works better for informal/conversational queries where embedding similarity is more reliable. Both modes use `qwen3-embedding` for the ingest step (vector RAG only) and Qwen2.5-32B-Instruct for generation.

### Q: What is system_zone_pressure and how does ARIA detect a platform-wide crisis?

`system_zone_pressure` is ARIA's platform-level collapse signal. It fires when `dead_zone_count / total_zones >= 0.50` — meaning at least half of all active delivery zones are classified as dead (stranding riders). At that point, per-zone analysis is meaningless and a single operator alert bypasses all zone-level evidence gates. The Zone agent writes this alert directly, ensuring it fires even if the Supervisor fails. The Dead Run agent has a parallel signal: if `flagged_orders / total_scored >= 0.50`, a `system_dead_zone_pressure` operator alert fires regardless of per-order evidence. The Supervisor escalates severity to `"critical"` on `system_zone_pressure` alone (named critical override) and also on the compound pattern `churn_surge AND dead_zone_pressure` firing simultaneously — two signals together are treated as more alarming than either alone. This deterministic multi-layer detection (Zone agent → Dead Run agent → Supervisor pattern detection) ensures the crisis signal cannot be missed by a single component failure.

### Q: Why did you choose this overall design approach over alternatives?

Every major design decision in ARIA favored the **simplest thing that solves the actual problem**: XGBoost over neural networks (tabular data at small scale, interpretability required via SHAP); vLLM over OpenAI API (on-premise, dual-GPU tensor parallelism for 32B model, no data leaving the network); LangGraph over plain Python (node-level observability via LangSmith, typed state schema as living documentation); deterministic algorithms for computation (zone stress, session EPH, restaurant z-score) and LLM only for synthesis and explanation — not computation; pgvector inside existing PostgreSQL over a separate vector DB (no extra service, SQL hybrid filters, tiny volume); TimescaleDB for continuous aggregates over plain PostgreSQL (28-day hourly baselines that update automatically, sub-millisecond query time vs full table scan); sequential agents over concurrent (single DB connection pool, eliminates read-write conflicts without transactions); pre-computed SHAP over runtime SHAP (3–15s per cycle eliminated vs zero overhead at inference); synthetic data over waiting for real data (3 of 4 models have no public dataset). The unifying principle: don't over-engineer. Each choice minimizes complexity while solving the specific constraint that actually exists.

---

*End of Section 1. Next: Section 2 — Data Strategy.*

---

# 2. Data Strategy

## 2.1 The Dataset Search: What Exists and What Doesn't

Before making any modelling decision, the first step was to search for publicly available datasets that could train the models ARIA needs. The result was clear and blunt: **almost nothing rider-side or supervisor-side exists publicly**.

The datasets that do exist for food delivery in India are **customer-facing and platform-facing** — they capture what the customer experiences (delivery time, ratings, order volume by time-of-day, restaurant metadata). Examples include order-volume datasets, restaurant review aggregations, and delivery time prediction datasets like the Kaggle Gaurav Malik dataset. These are useful for understanding the *delivery duration* from a customer perspective, but they contain nothing about:

- Rider session economics (EPH, idle time, session earnings)
- Zone-level demand density from the rider's perspective
- Dead run events or stranding rates by zone type
- Multi-session churn signals
- Rider persona types (supplementary vs dedicated)

These are **internal operational metrics** that platforms like Loadshare, Swiggy, and Zomato do not publish. The data exists inside their systems and is treated as proprietary. No Kaggle dataset, no academic paper, no government open data release, and no web-scraped source contains rider-side operational data at the resolution ARIA needs.

**What was found:** One dataset matched a real need — the **Food Delivery Time Prediction** dataset by Gaurav Malik (`gauravmalik26/food-delivery-time-prediction`), covering 41,953 delivery orders across 22 Indian cities, with actual delivery duration as the target. This is customer/logistics-side data, not rider-economics-side data, but it is the right data for Model 2 (Delivery Duration Scorer) — which predicts delivery duration, not rider earnings.

**What was not found:** No public dataset exists for rider EPH trajectories, zone dead-run rates, session health scores, or persona classification labels. For Models 1, 3, and 4, synthetic data is not a stylistic choice — it is the only engineering option available.

---

## 2.2 Real Kaggle Data (Model 2) — Source, Scope, Feature Engineering

Model 2 (Delivery Duration Scorer) is the only model trained on real data: the **Food Delivery Time Prediction** dataset by Gaurav Malik on Kaggle (`gauravmalik26/food-delivery-time-prediction`).

**Dataset facts:**
- 41,953 rows, 22 Indian cities (matching ARIA's zone set exactly)
- Features: delivery person age, ratings, order hour, weather condition, road traffic density, vehicle type, multiple distance columns, city name
- Target: `Time_taken(min)` — actual delivery duration in integer minutes
- Date columns: `Day` and `Month` stored as separate integers (no year column)

**Why real data for Model 2 specifically:**
Model 2 predicts delivery duration. This feeds the Restaurant Intelligence agent's baseline computation — the expected delivery time that anchors the z-score delay calculation. Getting this right requires the model to have learned the actual distribution of delivery times across real Indian cities under real traffic, weather, and route conditions. Synthetic data cannot replicate the multivariate correlations between distance, city tier, hour-of-day, and actual travel time — these emerge from real GPS-trace-derived data and cannot be reproduced from first principles.

**Data sanitization applied:**

The raw Kaggle dataset had several quality issues that required cleaning before training:

1. **Column name whitespace:** Several column names had leading/trailing spaces (e.g., `" Delivery_person_Age"`). All column names were stripped and normalised to snake_case.
2. **NaN in categorical columns:** `Weather_conditions`, `Road_traffic_density`, and `Type_of_vehicle` contained `NaN` rows. These were dropped (not imputed) — imputing a categorical label for weather or traffic would introduce false signal for a regression target.
3. **String-encoded numerics:** `Delivery_person_Age` and `Delivery_person_Ratings` were stored as object dtype (e.g., `"25 "`, `"4.9 "`). These were stripped and cast to `float`.
4. **Target column:** `Time_taken(min)` was stored as `"(min) 24"` — parenthesised string format. Parsed with regex `r'\(min\)\s*(\d+)'` to extract the integer.
5. **Zero-distance rows:** A small number of rows had `distance = 0.0` (data entry errors or same-location pickups). These were dropped as they produce undefined EPH contribution ratios.
6. **Date reconstruction:** No year column exists in the dataset. Dates were reconstructed as `pandas.Timestamp(year=2022, month=month, day=day)` — year is arbitrary (only relative ordering matters for the chronological train/val/test split).

**Feature engineering applied:**

The raw dataset does not contain `distance_km` as a direct field — it contains `Restaurant_latitude`, `Restaurant_longitude`, `Delivery_location_latitude`, `Delivery_location_longitude`. From these, the haversine distance was computed as a derived feature:

```python
distance_km = haversine_km(rest_lat, rest_lng, del_lat, del_lng)
```

This is the same haversine formula used in the simulation's `order_factory.py` — ensuring training-serving feature consistency.

Raw coordinate columns were then **dropped entirely** after computing distance. Keeping them would cause the model to memorise specific restaurant GPS positions (overfitting to the Kaggle dataset's restaurant locations) rather than generalising to arbitrary pickup-delivery pairs.

Additional engineered features:
- `is_peak_hour`: boolean derived from `Order_Hour` using ARIA's peak window definition `{7,8,9,12,13,18,19,20,21,22}`
- `is_long_distance`: boolean flag `distance_km > 5.0` — mirrors the `LD_BONUS` fare threshold and is informative for duration prediction

**Categorical encoding:** All categorical features (`Weather_conditions`, `Road_traffic_density`, `Type_of_vehicle`, `City`) were label-encoded using the same `CITY_TIER_ENC`, `WEATHER_ENC`, `TRAFFIC_ENC` dicts in `constants.py` that are used at inference time. This guarantees training/serving consistency — the same integer that trained the model is the same integer passed at prediction time.

---

## 2.3 Why Synthetic Data for 3 of 4 Models

Three of ARIA's four XGBoost models are trained on synthetic data: the Rider Persona Classifier (Model 1), the Dead Zone Risk Predictor (Model 3), and the Earnings Trajectory Forecaster (Model 4). As established in §2.1, this is not a stylistic preference — no public dataset exists for the quantities these models need to learn.

**What grounds the synthetic data:**

The Loadshare research article by Arun Ravichandran publishes the statistical ground truth:
- 80% supplementary / 20% dedicated rider split
- Supplementary EPH target Rs.90–100/hr, dedicated Rs.100+/hr
- Crisis EPH range Rs.70–85/hr
- 2 hours daily idle time
- Dead zone probability by zone type: hub 5%, commercial 20%, residential 45%, peripheral 75%
- Churn signal fires at three consecutive below-threshold sessions, retention drops to 30%

Synthetic data generated from these ground-truth statistics is not "made up" — it is a probabilistic model of reality calibrated to published measurements. The tradeoff is that models trained on synthetic data may over-fit to the synthetic distribution and underperform on real data. ARIA mitigates this deliberately:

1. **Gaussian noise injected per feature** — realistic variance, not perfectly clean synthetic inputs
2. **Hard class swaps (Model 1)** — 12% of training rows get features sampled from the opposite class distribution with 1.5× spread, forcing the boundary to be genuinely uncertain (prevents F1=1.0)
3. **Synthetic EPH values span the full crisis-to-healthy range** — not an artificially clean bimodal split

The result is models that learn general patterns (declining EPH slope → churn risk; peripheral zone + high distance + high dead rate → dead zone risk) rather than memorising synthetic identifiers.

---

## 2.3 Target Encoding Leakage Bug and Fix

During Model 2 training, an initial approach used `MEstimateEncoder` from the `category_encoders` library to encode the `City` categorical feature. This caused silent **target leakage**.

**What MEstimateEncoder does:** Replaces a categorical value with a smoothed mean of the target (`Time_taken(min)`) for that category, computed on the full training set. The smoothed mean formula is:

```
encoded_value = (n × mean_city + m × global_mean) / (n + m)
```

where `n` = samples for that city, `m` = smoothing parameter.

**Why this is leakage:** The encoded value for each city already encodes the target's distribution for that city. The model effectively "sees" a compressed version of the target during training. On test data (same cities), the same encoding is applied — it still works, but the model has not learned to generalise from features to delivery time. It has learned to look up a pre-computed summary of the target.

**The symptom:** Unrealistically high R² on validation (the encoded values are so informative that almost any weak learner achieves near-perfect fit) followed by poor generalisation.

**The fix:** Replace `MEstimateEncoder` with simple label encoding — `City` → integer (0 = Metropolitan, 1 = Urban, 2 = Semi-Urban). This encoding carries no target information; it merely converts city tier to the ordinal the model expects. The integer encoding is defined in `constants.py` as `CITY_TIER_ENC` and is the same encoding used at inference time — training/serving consistency is guaranteed because both import from the same source.

---

## 2.4 Seeding Strategy: 30-Day History from Day One

A live system that has just started has no history. Without history, the algorithmic modules cannot compute baselines, the ML models have no lag features, and the entire agent stack produces "data unavailable" outputs.

ARIA's seeding strategy solves this by generating 30 days of synthetic historical data at startup time, before any live simulation runs. `seed_from_v2.py` inserts:

- **500 riders** across 180 zones in 12 Indian cities
- **200 restaurants** with 14 days of delay event history per restaurant
- **~120,000 zone density snapshots** covering the full 30-day window
- **~10,500 completed rider sessions** (30 days × 500 riders × activity probability)
- TimescaleDB continuous aggregates are force-refreshed immediately after seeding

After this seed, every algorithmic module has a 28-day hourly baseline to work from on cycle 1.

### 2.4.1 Warm Start: `_ORDERS_PER_SIM_HOUR = 1.58` Derivation

The seed gives ARIA historical data but not a live operational state — on container startup, there are no active riders, no open sessions, and no orders in flight. The warm start (`warm_start.py`, called from the event-stream lifespan before `simulator.run()`) pre-seeds live state so that the first agent cycle does not see a cold, empty system.

The warm start needs to estimate how many orders a rider has completed in the `worked_h` sim-hours they have already been online. The derivation of `_ORDERS_PER_SIM_HOUR = 1.58`:

```
Observed rate from live session data:
  avg orders per session ≈ 8.4 orders
  avg session length     ≈ 4–6 sim-hours (supplementary and dedicated mix)
  → raw rate ≈ 8.4 / 5.0 = 1.68 orders/sim-hour

Apply 0.75 ramp-down factor to account for:
  - session start ramp-up (first ~15 sim-min riders wait for first order)
  - scheduler idle gaps between dispatch ticks
  - peak/off-peak mixing (not all warm-started riders are in peak)

_ORDERS_PER_SIM_HOUR = 1.68 × 0.75 ≈ 1.58
```

This constant is used to compute `orders_done = round(_ORDERS_PER_SIM_HOUR × worked_h × Uniform(0.8, 1.15))`, giving each warm-started rider a realistic pre-filled order count.

### 2.4.2 Warm Start Health Snapshot: Simplified Formula and Why It Diverges

The warm start writes one `rider_health_snapshots` row per rider so that the Earnings Guardian's Model 4 has lag EPH values on cycle 1. The health snapshot formula used in warm start is a simplified version of the full session module formula:

```python
current_eph   = total_earnings / max(worked_h, 1/60)
projected_eph = current_eph * Uniform(0.92, 1.08)   # ±8% jitter
health_score  = min(100.0, (current_eph / target_eph) * 100)
```

This intentionally diverges from the full 3-component health score formula (EPH 60pts + efficiency 25pts + trend 15pts). The reason: at warm-start time, the components needed for efficiency (dead run count relative to baseline) and trend (prior lag snapshots) are not yet available from live data. Using the simplified formula produces a health score in the correct range (reflecting EPH relative to target) without requiring data that doesn't exist yet. The divergence is acceptable because warm-start snapshots are only used as `eph_lag1/2/3` inputs to the Model 4 trajectory forecaster — the absolute health score value in these rows is not surfaced to the frontend.

### 2.4.3 Warm Start Worked Hours Distribution: `Uniform(0.5, 2.5)`

```python
worked_h = random.uniform(0.5, min(2.5, total_shift_h - 0.25))
```

The 0.5h floor ensures riders have at least 30 sim-minutes of data (enough to have completed at least one order). The 2.5h ceiling prevents warm-started riders from being so far into their shift that their first cycle check immediately triggers "almost done" end-of-shift logic. The `total_shift_h - 0.25` guard ensures at least 15 sim-minutes remain in the shift at warm-start time.

### 2.4.4 Warm Start `projected_eph`: ±8% Jitter and Rationale

```python
projected_eph = current_eph * random.uniform(0.92, 1.08)
```

The ±8% jitter on projected EPH serves two purposes:

1. **Avoids a perfectly deterministic warm start state** — without jitter, all riders at the same EPH level would have identical projected values, causing the Earnings Guardian to flag or clear exactly the same cohort every cycle until live data diverges. The jitter ensures agents see a realistic spread.

2. **Stays within a calibrated range** — ±8% corresponds to roughly ±Rs.7 on a Rs.90 EPH target, which is within the Model 4 regressor's expected error band (RMSE approximately Rs.8–12 on the training set). Larger jitter would produce projected values that are inconsistent with the rider's actual current trajectory.

### 2.4.5 Sentinel UUID for Warm-Start Health Snapshots

```python
_WARM_CYCLE_ID = "00000000-0000-0000-0000-000000000000"
```

The `rider_health_snapshots` table has a `cycle_id UUID NOT NULL` column — every snapshot must reference a cycle. Warm-start snapshots are not produced by any real agent cycle, so they use a fixed sentinel UUID (all zeros). This makes them identifiable and filterable in queries that need to distinguish agent-produced snapshots from warm-start data.

In the Earnings Guardian's lag EPH query, the sentinel rows are treated identically to real snapshots — their `current_eph` values feed into `eph_lag1/2/3`. The sentinel UUID is never stored in `cycle_briefings`, so `JOIN cycle_briefings ON health_snapshots.cycle_id = cycle_briefings.id` queries naturally exclude warm-start rows.

---

## 2.5 Zone Coverage: 180 Zones, 12 Cities

ARIA uses 180 zones across 12 Indian cities, distributed as follows:

| Zone Type   | Zones per city | Dead Zone Probability | Density Base |
|-------------|---------------|-----------------------|--------------|
| Hub         | 3             | 5%                    | 45 orders/hr |
| Commercial  | 4             | 20%                   | 28 orders/hr |
| Residential | 5             | 45%                   | 18 orders/hr |
| Peripheral  | 3             | 75%                   | 8 orders/hr  |

**15 zones per city × 12 cities = 180 zones.**

The cities are drawn directly from the Kaggle dataset's city list: Bangalore, Mumbai, Hyderabad, Chennai, Jaipur, Pune, Kochi, Kolkata, Indore, Mysore, Surat, Coimbatore. The centroid coordinates for each city are derived from the actual GPS coordinates in the dataset, not made up. This means the Leaflet map in the dashboard shows zones at geographically correct positions within real Indian cities.

Zone metadata is stored in the `boundary_geojson` JSONB column rather than a dedicated type column, because the schema was designed to be extensible and the `zones` table schema is shared with other potential uses. The zone type is queryable via `boundary_geojson->>'zone_type'`.

Sister zones (zones within 6–7km radius that have complementary order density) are pre-computed at generation time and stored as a UUID array in the `sister_zone_ids` column. The Zone Intelligence agent uses these for recommendations: when a zone goes dead, the sister zone list provides the candidates to evaluate.

---

## 2.6 Encoding Defaults and Safe Priors

Across ARIA's ML models and algorithmic modules, several constants encode the "when unknown, assume X" decisions. Each choice reflects a deliberate prior about the risk profile of unknown entities.

### 2.6.1 `ZONE_TYPE_ENC_DEFAULT = 2` (residential)

When a zone's type is not present in `boundary_geojson`, the ML feature assembly defaults to `2` (residential encoding). This is the **conservative mid-risk** default:

- `hub = 0` → safest zone, lowest dead zone probability (5%) — defaulting here would understate risk for unknown zones
- `residential = 2` → moderate risk (45% dead zone probability) — appropriate pessimistic prior for any zone we have no data on
- `peripheral = 3` → highest risk — would be overly alarmist for zones that are simply missing metadata

The choice of residential rather than hub or peripheral follows the principle of "fail towards caution without being maximally pessimistic."

### 2.6.2 `CITY_TIER_ENC_DEFAULT = 0` (Metropolitan)

All 12 seeded cities are metropolitan (Bangalore, Mumbai, etc.). The default of `0 = Metropolitan` means that any order with an unknown city is treated as if it originated in a high-density metropolitan area. This is consistent with the dataset: virtually all orders in the Kaggle data come from metropolitan cities, and an unknown city in ARIA's context is almost certainly a metro city that was simply not resolved.

### 2.6.3 `historical_dead_rate default = 0.3`

In the Dead Run Prevention agent, when a zone has no historical dead zone snapshot data (new zone, or data not yet available), the historical dead rate defaults to `0.3`. This is a **pessimistic prior** — 30% historical dead rate — that ensures new or unknown zones are treated as moderately risky rather than safe. The rationale: if we have no data on a zone, assuming it is safe is riskier than assuming it is moderately dangerous. An alert on a false positive is preferable to silence on a true positive in a dead zone context, where the cost is a stranded rider and lost earnings.

### 2.6.4 `ASSUMED_EPH_RS_PER_HR = 82.0`

Used in the dead run cost calculation: `earnings_lost = (stranding_mins / 60) × 82.0`. This constant is derived from the Loadshare article's published EPH range:

```
Article: crisis range Rs.70–85/hr, target Rs.90-100/hr
Midpoint of crisis range: (70 + 85) / 2 = 77.5 → rounded to 82.0 (slightly above midpoint)
Rationale: use a conservative estimate of what the rider "should" be earning,
           not the best-case target, to avoid overstating the cost of dead runs.
```

An Rs.82/hr assumed EPH means a 30-minute stranding costs the rider approximately Rs.41 — a realistic cost estimate for the EPH context of the simulation.

---

*End of Section 2. Next: Section 3 — Simulation Engine.*

---

# 3. Simulation Engine

The simulation engine is a self-contained subsystem that generates a realistic synthetic operational environment: riders come online, get assigned orders, travel to restaurants and delivery zones, earn fares, and go offline. All of this happens in compressed simulated time, driven by the `SimClock`. The output is a live PostgreSQL + Redis state that the agent stack reads and analyses every 15 simulated minutes.

---

## 3.1 SimClock: Time Compression

### The Problem

Agents need to process events measured in hours (EPH, shift length, zone baselines). But a demo running in real time would require hours of waiting before any interesting patterns emerge. The `SimClock` solves this by compressing time — every real second corresponds to `TIME_SCALE` simulation seconds.

### Formula

```
sim_time = sim_start + timedelta(seconds=sim_elapsed)
sim_elapsed = max(0, real_elapsed − total_pause_secs) × TIME_SCALE

where:
  real_elapsed      = (datetime.now(UTC) − real_start).total_seconds()
  total_pause_secs  = accumulated real seconds spent paused
```

This is implemented exactly in `clock.py:now()`:
```python
real_elapsed = max(0.0, real_elapsed - self._total_pause_real_secs)
sim_elapsed  = real_elapsed * self._time_scale
return self._sim_start + timedelta(seconds=sim_elapsed)
```

### Why `TIME_SCALE=10` for Demos

At `TIME_SCALE=10`, one real minute equals 10 simulated minutes. A full simulated hour passes in 6 real minutes. This allows:
- A peak lunchtime (12:00–14:00) to pass in 12 real minutes
- A 4-hour supplementary rider shift to complete in 24 real minutes
- A full 24-hour simulated day to pass in ~2.4 real hours

For a demo, `TIME_SCALE=10` is the sweet spot: fast enough to see the agent firing multiple cycles within a few minutes, but not so fast that the order pipeline can't keep up. At `TIME_SCALE=300`, the simulation outruns the dispatcher (see Section 3.5 on DISPATCHER_TICK_SECS).

### Pause Accumulation

When the simulation is paused, real time continues but sim time does not advance. The pause mechanism accumulates real pause duration:
```python
def pause(self):
    self._pause_real_at = datetime.now(UTC)

def resume(self):
    self._total_pause_real_secs += (datetime.now(UTC) - self._pause_real_at).total_seconds()
```

Subtracting `total_pause_secs` before multiplying by `TIME_SCALE` ensures sim time freezes cleanly across arbitrarily many pause/resume cycles without any drift.

### On-the-Fly Time Scale Change

Changing `TIME_SCALE` without re-anchoring would cause a sim-time discontinuity (jump forward or backward). `set_time_scale()` re-anchors by computing `current_sim = self.now()` before the change and then resetting `real_start = now()` and `sim_start = current_sim`. After the re-anchor, `now()` still returns `current_sim` at the moment of change, and from that point forward the new scale applies.

---

## 3.2 Fare Model

### Formula

```
fare = BASE_FARE_RS + distance_km × PER_KM_RATE_RS + (LD_BONUS_RS if distance > 5km else 0)
     + Uniform(−1.5, +2.5)

where:
  BASE_FARE_RS   = 25.0 Rs
  PER_KM_RATE_RS =  4.0 Rs/km
  LD_BONUS_RS    = 15.0 Rs (long distance, >5km)
```

**Example fares:**
- 2km non-LD: 25 + 8 + noise ≈ Rs.33
- 6km LD: 25 + 24 + 15 + noise ≈ Rs.64
- Density-weighted average distance ≈ 2km, ~30% LD rate → weighted avg ≈ Rs.42

### Fare Noise: Asymmetric `Uniform(−1.5, +2.5)`

The noise is intentionally asymmetric — the upper bound (+2.5) is larger than the lower bound (−1.5). This models the reality that delivery fares in India have tip structures and surge adjustments that skew slightly positive. Symmetric noise would produce a zero-mean distribution around the base fare; asymmetric noise produces a small positive expected shift of +0.5 Rs per order, which compounds to approximately Rs.1.5/hr at 3 orders/hr.

### EPH Calibration

```
Target EPH = Rs.90/hr (supplementary rider target from Loadshare article)

Actual: 3.3 orders/hr × avg fare Rs.42 × efficiency factor = ~Rs.89/hr

Components:
  avg fare Rs.42 = (70% non-LD × Rs.33) + (30% LD × Rs.64)
                 = Rs.23.1 + Rs.19.2 = Rs.42.3
  orders/hr 3.3 = _ORDERS_PER_SIM_HOUR (1.58) × time multiplier
                  (under healthy mid-peak conditions, actual throughput ≈ 3.3/hr)
  Rs.42.3 × 3.3 ≈ Rs.139/hr gross, less ~35% idle fraction → Rs.90/hr net
```

Note: the "Rs.27 per order" figure cited in the code comment (`3.3 × Rs.27 ≈ Rs.89`) reflects an earlier calibration pass. After `BASE_FARE_RS` was raised from 15 to 25, the per-order average rose from Rs.27 to Rs.42, but the `3.3 orders/hr` rate and the Rs.89 target remain correct — the comment refers to a transitional calculation and should not be taken as the current per-order average.

### Why BASE_FARE Was Raised from 15 to 25

With `BASE_FARE_RS=15`, the fare structure was:
- 2km non-LD: Rs.23 (too low)
- EPH at 3.3 orders/hr: Rs.23 × 3.3 × ~0.65 efficiency ≈ Rs.49/hr

This produced EPH readings of ~Rs.26/hr in live testing — far below the Rs.90 target and appearing as constant "critical" health scores on every rider from the first cycle. Raising `BASE_FARE_RS` to 25 corrected EPH to the target range without changing the rate-per-km structure.

---

## 3.3 Order Factory

### Haversine Distance Formula

```
d = 2R × arcsin(√(sin²(Δlat/2) + cos(lat1)×cos(lat2)×sin²(Δlng/2)))
where R = 6371.0 km
```

Used everywhere distances are needed: order factory, zone sister ranking, dispatcher zone selection, warm start zone assignment, and the MCP server's own rider creation endpoint. The formula is implemented identically in `order_factory.py`, `main.py` (event-stream), and `mcp-server/main.py` to avoid cross-service dependencies.

### Queue-Aware Prep Time

```
capacity       = max(2, round(base_prep_mins / PREP_TIME_PER_SLOT))  # PREP_TIME_PER_SLOT = 5.0 min
congestion_fac = 1.0 + max(0, (queue_len − capacity) / capacity)
actual_prep    = base_prep_mins × congestion_fac × N(1.0, σ=0.12)

Example: base=20min → capacity = 20/5 = 4 orders
  queue=2:  congestion = 1.0,  actual ≈ 20min  (no queue pressure)
  queue=6:  congestion = 1.5,  actual ≈ 30min  (50% overhead)
  queue=10: congestion = 2.5,  actual ≈ 50min  (severe delay)
```

The `PREP_TIME_PER_SLOT = 5.0` constant models a restaurant with fixed kitchen throughput: each 5-minute prep slot can handle one order. When the queue exceeds that capacity, each additional order adds a fractional delay proportional to the overflow. This is the same signal that the Restaurant Intelligence agent's z-score pipeline reads.

### Travel Time

```
base_travel_mins = (distance_km / speed_km_h) × 60
actual_travel    = max(2.0, base_travel × N(1.0, σ=0.10))
```

The 10% Gaussian noise models real-world variance: traffic lights, route deviation, GPS drift. The 2-minute minimum prevents zero-distance assignments from producing negative or zero travel times.

### Travel to Restaurant = 0.4 × Delivery Distance

```python
travel_to_rest = travel_mins(dist_km * 0.4, zone_type, is_peak)
travel_to_del  = travel_mins(dist_km,       zone_type, is_peak)
```

The pickup leg (rider to restaurant) is modelled as 40% of the delivery distance. Rationale: riders start from within their home zone, restaurants are distributed within zones, and the inter-zone delivery distance typically exceeds the intra-zone pickup distance. 40% is a calibration constant derived from observing that metropolitan zone radii average ~1.5km while inter-zone delivery distances average ~2–4km — a ratio of approximately 0.4–0.5.

### Speed Table by Zone Type and Period

| Zone Type   | Peak (km/h) | Off-Peak (km/h) |
|-------------|-------------|-----------------|
| Hub         | 14          | 22              |
| Commercial  | 12          | 20              |
| Residential | 18          | 25              |
| Peripheral  | 28          | 35              |

Hub and commercial zones are slower during peak because they are dense urban areas with high traffic congestion. Residential zones are moderately fast because they have residential street networks that are not as congested. Peripheral zones are fastest because they are on city outskirts with fewer traffic signals and wider roads.

### Peak Hour Window

```python
PEAK_HOURS = frozenset(range(7, 10)) | frozenset(range(12, 14)) | frozenset(range(18, 23))
# = {7, 8, 9, 12, 13, 18, 19, 20, 21, 22}
```

Derived from the Loadshare article's description of supplementary rider behaviour: morning commute (7–9), lunch (12–14), dinner and evening (18–22).

### Density-Weighted Zone Selection

```python
TYPE_WEIGHTS = {"hub": 2.0, "commercial": 1.8, "residential": 1.0, "peripheral": 0.4}
weight = TYPE_WEIGHTS[zone_type] × max(0.1, order_count_in_cache)
```

Delivery destinations are weighted by zone type and current order activity. Hub zones attract twice as many deliveries as residential zones. Peripheral zones get 40% of a residential zone's weight — reflecting their role as origin zones for riders rather than delivery destinations. The `max(0.1, ...)` floor ensures that zones with zero current orders are not completely excluded, only de-prioritised.

### 80/20 Pickup Zone Split

```python
pickup_zone_id = (
    random.choice(sisters) if sisters and random.random() < 0.2
    else home_zone_id
)
```

Riders spend 80% of their time assigned to orders in their home zone and 20% from a sister zone. This models the Loadshare article's observation that 90% of LD orders crossed zone boundaries — most rider activity is local, but a meaningful fraction involves adjacent zones.

### Weather Sampling: Time-Correlated

```python
if 15 <= hour <= 20:  # late afternoon / evening
    weights = [0.35, 0.30, 0.25, 0.10]   # Clear / Cloudy / Rain / Heavy_Rain
else:
    weights = [0.60, 0.22, 0.14, 0.04]
```

Rain probability is higher during late afternoon and evening hours (hours 15–20). This matches the Indian seasonal pattern (afternoon thunderstorms, evening rain) and creates realistic variance in Model 2 (Delivery Duration Scorer) predictions, which includes weather as a feature.

### Traffic Sampling: Zone + Time Correlated

Hub and commercial zones during peak hours see `(medium:20, high:50, jam:30)` weight distribution. Off-peak or residential/peripheral zones default to `(low:55, medium:35, high:10)`. This prevents residential zones from ever being classified as `jam`, which would be unrealistic and would bias Model 2.

### Coordinate Jitter: ±0.004° from Zone Centroid

```python
pickup_lat  = zone["centroid_lat"] + random.uniform(-0.004, 0.004)
pickup_lng  = zone["centroid_lng"] + random.uniform(-0.004, 0.004)
```

±0.004° corresponds to approximately ±440m at Indian latitudes. This ensures that each order appears at a slightly different map position rather than all stacking on the zone centroid, making the Leaflet map look realistic with orders distributed across the zone area rather than as a single dot.

---

## 3.4 Zone Density Snapshot Engine

Every `CYCLE_INTERVAL_MINS` simulated minutes, `snapshot_all_zones()` runs for all 180 zones and computes:

### `density_score`

```
density_score = min(1.0, order_count / max(rider_count, 1) / 10.0)
```

This is a demand/supply saturation metric. The denominator `max(rider_count, 1)` prevents division by zero. The divisor `10.0` normalises: a zone with 10 orders and 1 rider has `density_score = 1.0` (fully saturated), while a zone with 5 orders and 2 riders has `density_score = 0.25` (25% saturated). Values above 1.0 are capped — there is no meaningful distinction between "10 orders per rider" and "20 orders per rider" from a zone health perspective; both indicate ample demand.

### `stress_ratio`

```
# Case 1: zone is empty (no orders, no riders)
if order_count == 0 and rider_count == 0:
    stress_ratio = 1.0   # neutral — unknown, not dead

# Case 2: historical baseline available
elif baseline > 0:
    stress_ratio = density_score / baseline   # current vs 28-day avg

# Case 3: no history, but zone has activity
elif density_score > 0:
    stress_ratio = density_score / 0.5   # 0.5 = neutral reference

# Case 4: zone has riders but no orders
else:
    stress_ratio = 0.0   # dead
```

**The neutral stress rule (Case 1):** A zone with zero orders AND zero riders is not dead — it is simply empty. Dead means there are riders present but no orders (supply without demand). An empty zone has no signal either way, so `stress_ratio = 1.0` (neutral) is correct. This prevents the Zone Intelligence agent from flagging quiet zones during off-peak hours as dead zones.

### `order_delta`

```
order_delta = order_count − prev_order_count
```

The previous count is read from the most recent snapshot row per zone. For the first snapshot, no previous row exists, so `order_delta = order_count − order_count = 0`. This delta is used by the Zone Intelligence agent's surge detection: a zone with `stress_ratio > 1.2 AND order_delta > 0` is experiencing active surge (orders are arriving), not just static high density.

### Redis TTL = 900s

```python
pipe.expire(key_zone_density(zone_id), 900)  # 900 = CYCLE_INTERVAL_MINS × 60 at TIME_SCALE=1
```

The zone density cache TTL is set to 900 seconds (15 real minutes at `TIME_SCALE=1`). At `TIME_SCALE=10`, a 15-minute sim cycle passes in 90 real seconds, so the cache is refreshed well before the TTL expires. The TTL serves as a safety net: if the event-stream crashes, the cache entries expire naturally within one cycle's worth of real time rather than serving stale data indefinitely.

---

## 3.5 Dispatcher Design

### Central Dispatcher vs Pull Model

ARIA uses a **central dispatcher** — a single loop that iterates over all idle riders every tick and assigns orders. An alternative "pull" model would have riders request orders from a queue. The central model was chosen because:

1. ARIA's zone-density-weighted delivery zone selection needs a global view of all zones to pick the correct destination. A rider-side pull has no access to this.
2. The dispatcher can enforce fairness policies (e.g., not assigning dead zone orders during known pressure periods) as a single authority.

### `DISPATCHER_TICK_SECS = 1`

This is the real-time interval at which the dispatcher loop fires. At `TIME_SCALE=300` and a tick of 5 real seconds, the sim advances 1500 sim-seconds (25 sim-minutes) between ticks. An idle rider waits up to 25 simulated minutes before receiving their first order — destroying EPH at high simulation speeds. At `DISPATCHER_TICK_SECS = 1`, the worst-case idle gap is 300 sim-seconds (5 sim-minutes) at `TIME_SCALE=300`. This was a live bug fix: EPH readings dropped to ~Rs.10–15/hr at `TIME_SCALE=100` with the original 5-second tick.

### Tick Interval Summary

| Loop | Interval | Type | Purpose |
|------|----------|------|---------|
| `_dispatcher_loop` | `DISPATCHER_TICK_SECS=1` | real-time | Assign idle riders to orders |
| `_order_pipeline_loop` | `PIPELINE_TICK_SECS=2` | real-time | Advance order states (assigned → picked_up → delivered) |
| `_scheduler_loop` | `SCHEDULER_TICK_SECS=30` | real-time | Bring riders online/offline based on shift schedule |
| `_zone_snapshot_loop` | `CYCLE_INTERVAL_MINS×60/TIME_SCALE` | sim-scaled | Write zone density snapshots to DB and Redis |

---

## 3.6 Rider Online Probability (Poisson-Inspired)

Each scheduler tick, offline riders are checked against a probability table to decide whether to come online:

**Supplementary riders:**
| Hours | Probability |
|-------|------------|
| 7, 8, 9 (morning rush) | 0.18 |
| 12, 13 (lunch peak) | 0.10 |
| 18, 19, 20, 21, 22 (dinner) | 0.22 |
| Other | 0.02 |

**Dedicated riders:**
| Hours | Probability |
|-------|------------|
| Peak hours (any) | 0.20 |
| Business hours (8–21, non-peak) | 0.10 |
| Night (other) | 0.01 |

These probabilities are per-tick (per 30 real seconds). At `TIME_SCALE=10`, 30 real seconds = 5 sim-minutes, so the evening probability of 0.22 means a given supplementary rider has a ~22% chance of coming online in any given 5-sim-minute window during dinner hours. The concurrency caps (`PEAK_MAX_RIDERS=150`, `OFFPEAK_MAX_RIDERS=40`) act as a ceiling — no new riders come online if the current count exceeds the target.

**Shift length distributions:**
- Supplementary: `Uniform(3.0, 5.0)` sim-hours — aligns with article's part-time pattern (half-day peaks)
- Dedicated: `Uniform(8.0, 12.0)` sim-hours — full-day coverage

---

## 3.7 Order State Machine

Orders progress through seven states in a strictly ordered pipeline:

```
pending → assigned → rider_inbound → picked_up → en_route_delivery → delivered
                                                                     → failed (terminal)
```

| Transition | Trigger | Due-time computation |
|-----------|---------|---------------------|
| pending → assigned | Dispatcher tick | Immediate |
| assigned → rider_inbound | sim_now ≥ assigned_at + travel_to_restaurant | travel_to_rest_mins |
| rider_inbound → picked_up | sim_now ≥ rider_inbound_at + prep_time | actual_prep_mins |
| picked_up → en_route_delivery | Immediate | 0 |
| en_route_delivery → delivered | sim_now ≥ picked_up_at + travel_to_delivery | travel_to_del_mins |
| any → failed | Rider shift ends, or simulation restart | Immediate |

`delivered` and `failed` are terminal states — orders in these states are excluded from all active order counts and zone density calculations.

---

## 3.8 Session Idempotency

### Reopen Resets All Stats

When a rider session is opened (via `session_manager.open_session()`), if a session row already exists for that `(rider_id, session_date)`, it is reset with `shift_start = NOW()` and all counters cleared (`total_orders=0`, `total_earnings=0`, etc.). This prevents stale carry-forward: if a rider's session row from a previous run has partial data, reopening it produces a clean slate. The rider's EPH at the next agent cycle will reflect only the current run's data.

### DELETE-before-INSERT in `warm_start.py`

The warm start uses `INSERT INTO rider_sessions` with a `UNIQUE(rider_id, session_date)` constraint. Before inserting a warm-start session, the seeder deletes any closed (non-NULL `shift_end`) session for that `(rider_id, session_date)` pair:

```python
await conn.execute(
    "DELETE FROM rider_sessions WHERE rider_id=$1 AND session_date=$2 AND shift_end IS NOT NULL",
    rider_id, session_date,
)
```

This resolves the bug where a previous run's completed session for today blocked the warm-start INSERT. The deletion only targets closed sessions — open sessions (from a concurrent restart edge case) are left alone, and the subsequent idempotency check catches them.

---

## 3.9 Restart Semantics

On every container restart or `Simulator.load_reference_data()` call, two cleanup actions run before the simulation starts:

**1. Ghost session cleanup:**
```sql
UPDATE rider_sessions SET shift_end = NOW() WHERE shift_end IS NULL
```
Any open session from a previous run is closed. Without this, the Earnings Guardian's `hours_elapsed` calculation reads `(sim_now − session_start)` where `session_start` may be from a prior run with a different `TIME_SCALE` — producing `hours_elapsed=0` (→ EPH = infinity) or `hours_elapsed` spanning multiple real days.

**2. Order failure on restart:**
```sql
UPDATE orders SET status = 'failed', failed_at = NOW(),
    failure_reason = 'simulation_restart'
WHERE status NOT IN ('delivered', 'failed')
```
All non-terminal orders are failed. Without this, the order pipeline would attempt to advance orders whose `due-time` datetimes belong to a previous sim session and may be arbitrarily far in the past or future relative to the new clock. Stale active orders also skew zone density (inflating `order_count`) and session stats (an order never delivered still occupies the rider's slot in-memory).

---

*End of Section 3. Next: Section 4 — Algorithmic Modules.*

---

# 4. Algorithmic Modules

The three algorithmic modules — Zone, Session, Restaurant — are pure Python functions with no LLM calls, no side effects other than DB reads, and no stochastic elements. They form the mathematical backbone of ARIA: every number the agents report is produced here. Agents call these functions, receive structured results, and use those results as grounding for LLM synthesis or rule-based decision gates.

---

## 4.1 Zone Module (`algorithms/zone.py`)

Computes `density_score`, `stress_ratio`, dead/low/normal/stressed zone classifications, `risk_level` scores, sister zone rankings, and `system_zone_pressure` (platform-wide crisis signal). Also contains the dual-threshold hysteresis and staleness gate. Called by Agent 03 (Zone Intelligence) each cycle.

### The Problem Zone Algorithms Solve

A density reading of 0.4 at 2 PM on a Tuesday means something different from 0.4 at 8 PM on a Friday. Raw density has no context. The zone module converts raw density into *contextual* stress — how does this moment compare to the same hour historically? That contextual signal is what makes "zone is dead" meaningful rather than just "zone is quiet."

### `density_score`: Demand/Supply Saturation

```
density_score = min(1.0, order_count / max(rider_count, 1) / 10.0)
```

This is the same formula used in the zone density snapshot engine (Section 3.4). The algorithmic module reads it from the most recent `zone_density_snapshots` row rather than computing it live. `max(rider_count, 1)` prevents division by zero. The divisor 10 normalises so that a 10:1 order-to-rider ratio represents full saturation (score=1.0).

### Stress Ratio: Current vs 28-Day Historical Baseline

```
stress_ratio = current_density / baseline_density
```

where `baseline_density` = average `density_score` for the same `hour_of_day` over the last 28 days, from the `zone_density_hourly` continuous aggregate.

**Why same-hour baseline:** Peak lunch density (12:00) is structurally higher than 3 AM density. If ARIA used the overall 28-day average (including all hours), a zone with normal 2 PM density would appear "stressed" because it's above the overnight average. Same-hour comparison removes this structural variation and isolates genuine anomalies.

**Fallback when no history:**
```python
# No baseline available but zone has activity
stress_ratio = current_density / 0.5   # 0.5 = neutral reference
```
The value 0.5 is chosen as the neutral reference: a zone at 50% demand saturation is considered operating at a normal, unremarkable level. This fallback produces `stress_ratio = 2.0` for a fully saturated new zone (density=1.0) and `stress_ratio = 0.4` for a quiet new zone (density=0.2).

**Neutral stress rule:**
```python
if order_count == 0 and rider_count == 0:
    stress_ratio = 1.0  # no signal — treat as neutral
```
An empty zone is not a dead zone. Dead means riders are present but orders are not. A completely empty zone has no operational signal — setting `stress_ratio=1.0` (neutral) prevents it from appearing in dead zone alerts.

### Dead / Low / Normal / Stressed Thresholds

| stress_ratio | Level | Interpretation |
|-------------|-------|----------------|
| < 0.5 | dead | Riders present, orders scarce — stranding risk |
| 0.5–0.8 | low | Below average, watch for deterioration |
| 0.8–1.2 | normal | Operating within normal variance |
| ≥ 1.2 | high | Over-demanded, rider competition |

`DEAD_ZONE_STRESS_THRESHOLD=0.5` and `STRESSED_ZONE_THRESHOLD=1.2` are configurable via environment variables and used consistently across zone module, zone engine, and all agents that reference zone classification.

### `risk_level`: Continuous Dead Zone Score

```
if stress_ratio == 0:
    risk_level = 1.0                                 # worst case
elif stress_ratio < DEAD_ZONE_STRESS_THRESHOLD:
    risk_level = 1.0 − (stress_ratio / DEAD_ZONE_STRESS_THRESHOLD)
else:
    risk_level = 0.0
```

This is a linear decay from 1.0 (completely dead, stress_ratio=0) to 0.0 (at the threshold). The frontend heatmap overlay uses `risk_level` directly as the colour intensity — zones approaching the dead threshold appear progressively more red. At `stress_ratio=0.5`, `risk_level=0`. At `stress_ratio=0.25`, `risk_level=0.5`.

### Two-Threshold Hysteresis: 0.50 for DB, 0.45 for Recommendation Trigger

The Zone Intelligence agent (Section 6.3) uses a slightly lower threshold (0.45) to trigger zone recommendations than the module's dead zone classification threshold (0.50). This is **hysteresis** — preventing rapid oscillation where a zone alternates between "dead" and "not dead" as its stress_ratio bounces around the 0.50 boundary. A zone classified as dead at 0.50 requires the stress_ratio to rise to at least 0.45+ before it stops triggering recommendations.

### Sister Zone Ranking

When a zone is flagged as dead or low, the agent needs to recommend where riders should move. Sister zones (pre-seeded as zone radius neighbours) are ranked by:

```python
# Sort: highest density first, distance as tie-breaker
sisters.sort(key=lambda z: (-z["density_score"], z["distance_km"]))
```

**In the Zone Intelligence Agent** (not the module), sister zones are further filtered:

```
Eligibility gate:
  - same city (never cross-city recommendation)
  - stress_ratio > 0.45 (not itself at risk)
  - distance ≤ 7.0 km (from Loadshare article: 6–7km sister zone radius)
  - snapshot not stale (last seen within STALE_SNAPSHOT_MINS=20)

adjusted_density = density_score × TYPE_MULTIPLIER
TYPE_MULTIPLIERS: hub=1.15, commercial=1.05, residential=1.0, peripheral=0.9
```

The type multipliers are intentionally narrow (range: 0.9–1.15). They represent a soft preference for higher-quality zone types without overriding actual density readings. A peripheral zone with density=0.8 still ranks above a hub zone with density=0.3 even after multipliers. The multipliers break ties when densities are close.

**Density gain filter:** A sister zone must have `density_score ≥ current_zone_density + 0.10` to be recommended. Moving a rider 5km for a 0.01 density improvement is not worth it. The 0.10 filter ensures only meaningful improvements trigger a recommendation.

### Staleness Gate

```python
age_mins = (now - snapshot.timestamp).total_seconds() / 60
is_stale = age_mins > STALE_SNAPSHOT_MINS   # = 20 minutes
```

At `TIME_SCALE=10`, the zone snapshot loop runs every 90 real seconds (= 15 sim-minutes). A 20-minute real-time staleness threshold means any snapshot written within the last 4 cycles is still considered fresh. A stale snapshot puts the zone in "unknown" state — it is neither classified as dead nor healthy.

### Zone Pressure and Platform-Wide Crisis Detection

`system_zone_pressure` is a fleet-level collapse signal. The **Zone agent** fires it when `dead_zone_count / total_zones >= 0.50` — at least half of all zones are dead simultaneously. At that threshold, per-zone analysis is meaningless and human escalation is required. This alert is written by the Zone agent directly (not the Supervisor), so it fires even if the Supervisor fails on this cycle. The **Dead Run agent** has a parallel signal: `system_dead_zone_pressure` fires if `flagged_orders / total_scored >= 0.50` — if half of all active orders are heading toward dead zones, the problem is platform-wide. The **Supervisor's `analyze_patterns` node** uses `system_zone_pressure` as a named critical override — `dead_zone_count / total_zones >= 0.50` immediately sets severity to `"critical"` regardless of any other pattern scores. A compound critical also fires if `churn_surge AND dead_zone_pressure` both trigger simultaneously, even if neither alone would reach critical.

---

## 4.2 Session Module (`algorithms/session.py`)

Computes per-rider session metrics: current EPH, health score (3-component formula), dead run cost, shortfall calculation, and churn signal detection. Called by Agent 04 (Earnings Guardian) each cycle.

### Current EPH

```python
hours_elapsed = max((now - shift_start).total_seconds() / 3600, 1/60)
current_eph   = total_earnings / hours_elapsed
```

The `max(..., 1/60)` floor clamps `hours_elapsed` to at least 1 minute. Without this, a rider who just came online (shift_start = now) would produce `hours_elapsed ≈ 0` → `current_eph → ∞`. The 1-minute floor gives a conservative EPH reading at session open.

### Health Score: 3-Component Formula

The health score combines EPH performance, session efficiency, and trajectory into a single 0–100 score:

```
health_score = eph_score (0–60) + efficiency_score (0–25) + trend_score (0–15)
```

**EPH Component (60 points):**
```
projected_ratio = min(projected_eph / eph_target, 1.5)
current_ratio   = min(current_eph   / eph_target, 1.5)
blended_ratio   = 0.7 × projected_ratio + 0.3 × current_ratio
eph_score       = blended_ratio × 60
```

The 70/30 split weights the Model 4 projection more heavily than the current reading. This makes the health score **forward-looking** — a rider who has a low current EPH due to a slow start but whose trajectory (Model 4) predicts a strong finish will score better than a rider whose trajectory is declining. The 1.5× cap prevents a rider earning double their target from scoring above 100 (the maximum useful score is 1.0× target = 60 pts on this component).

**Efficiency Component (25 points):**
```
dead_run_penalty = min(15.0, dead_runs_count × 5.0)   # 5pts each, max 15
idle_fraction    = idle_time_mins / max(hours_elapsed × 60, 1)
idle_penalty     = min(10.0, idle_fraction × 20.0)    # 20% idle → 4pts, 50% → 10pts
efficiency_score = max(0.0, 25.0 − dead_run_penalty − idle_penalty)
```

Dead runs and idle time both reduce efficiency. Three dead runs wipe out the entire efficiency component. An idle fraction of 50% produces a 10-point penalty — the maximum. The component cannot go negative.

**Trend Component (15 points):**
```
trend_score = min(15.0, max(0.0, blended_ratio × 15.0))
```

This is the same blended ratio used for the EPH component but scaled to 15 points. It acts as a reinforcing signal: riders above their EPH target get bonus trend points; riders below get trend penalty. The total maximum health score is `60 + 25 + 15 = 100`.

### Health Classifications

| health_score | Class | Action threshold |
|-------------|-------|-----------------|
| ≥ 75 | healthy | No action |
| 50–74 | watch | Monitoring only |
| 40–49 | at_risk | Intervention consideration |
| < 40 | critical | Immediate intervention |

### Dead Run Cost

```
earnings_lost   = (stranding_mins / 60) × ASSUMED_EPH_RS_PER_HR   (= 82.0)
severity_ratio  = earnings_lost / ASSUMED_EPH_RS_PER_HR
health_penalty  = −(5.0 + min(10.0, severity_ratio × 20.0))
```

A 30-minute stranding: `earnings_lost = Rs.41`, `severity_ratio = 0.5`, `penalty = −(5 + 10) = −15 pts`. A 10-minute stranding: `earnings_lost = Rs.13.67`, `severity_ratio = 0.17`, `penalty = −(5 + 3.4) = −8.4 pts`. The base penalty of 5 points ensures even short strandings register, while the severity scaling captures the proportional earnings impact.

### Shortfall Calculation

```
shortfall_rs = (eph_target − projected_eph) × min(remaining_hrs, 2.0)
```

The **2-hour cap** on remaining hours is the key design decision here. If a rider has 6 hours left in their shift and their projected EPH is Rs.60 vs Rs.90 target, a naive calculation produces a shortfall of `(90−60) × 6 = Rs.180`. But ARIA's interventions (zone recommendations, rest alerts) can only affect the next 1–2 hours. A Rs.180 shortfall number is alarmist and misleading — a rider could recover in 2 hours if the intervention works. The 2-hour cap produces `(90−60) × 2 = Rs.60`, which accurately represents the actionable earnings at risk.

### Churn Signal: Multi-Session Detection

```python
signal_strength = 0.4 × consec_score + 0.4 × eph_deficit + 0.2 × trend_penalty

where:
  consec_score = min(1.0, consecutive_bad / CHURN_SIGNAL_SESSIONS)
  eph_deficit  = max(0, (EPH_TARGET_SUPP − avg_eph)) / EPH_TARGET_SUPP
  trend_penalty = max(0, (older_avg − recent_avg) / older_avg)  # 2 most recent vs 2 older

is_churn_risk = signal_strength ≥ 0.5 OR consecutive_bad ≥ CHURN_SIGNAL_SESSIONS (= 3)
```

**Why 40/40/20 weights:** Consecutive below-threshold sessions and EPH deficit are the primary signals (equal weight) because the Loadshare article identifies these as the two independent causes of churn: the streak effect (each bad session increases probability of leaving) and the absolute EPH level (below target = economically rational to leave). Trend is a supporting signal at 20% because it measures direction rather than magnitude.

**Why the OR gate for `is_churn_risk`:** `signal_strength ≥ 0.5` catches compound weak signals (moderate streak + moderate deficit + declining trend). `consecutive_bad ≥ 3` catches the pure streak case (3 consecutive bad sessions is the Loadshare article's identified trigger threshold for retention dropping to 30%, regardless of EPH deficit magnitude).

---

## 4.3 Restaurant Module (`algorithms/restaurant.py`)

Computes per-restaurant queue congestion risk scores using a z-score pipeline (current queue vs 28-day same-hour baseline). Calculates `actual_prep` time with a congestion factor. Called by Agent 01 (Restaurant Intelligence) each cycle.

### What Exactly Is Being Z-Scored

A common misconception: the Restaurant Intelligence agent does **not** z-score actual prep time measurements against historical prep times. It z-scores the **queue-overflow congestion component** of prep time:

```
congestion_extra = base_prep × max(0, (queue_len − capacity) / capacity)
```

This is the additional time beyond base prep caused by queue pressure. This is what the baseline is compared against — not the raw absolute prep time, which varies between restaurants by design (a pizza place takes longer than a salad shop regardless of queue pressure).

**The z-score:**
```
deviation_mins = actual_delay_mins − baseline_avg_delay_mins
z_score        = deviation_mins / max(baseline_std_delay_mins, 0.5)
```

The `max(..., 0.5)` floor for the standard deviation prevents z-score explosion when a restaurant has very consistent historical delay (std ≈ 0). Without the floor, a restaurant with std=0.01 that deviates by 1 minute would produce z=100, triggering immediate critical alerts for trivial variations.

**Risk score:**
```
risk_score = sigmoid(z_score) = 1 / (1 + e^(−z_score))
```

Using sigmoid rather than a direct threshold converts the z-score into a smooth probability-like [0,1] range. At z=0: risk=0.5 (neutral). At z=1.5 (threshold): risk=0.82. At z=3: risk=0.95. The sigmoid's bounded output prevents extreme z-scores (which can happen in low-sample restaurants) from producing hard 1.0 outputs, preserving relative ranking across restaurants.

### Confidence Gate

```
confidence = min(sample_count / 20.0, 1.0)
```

A restaurant with only 3 delay events has `confidence = 0.15`. The Restaurant Intelligence agent caps severity at "low" for any restaurant with `sample_count < 5`. This prevents newly-seeded or newly-observed restaurants with sparse data from generating high-severity alerts based on statistical noise. Full confidence (1.0) is reached at 20+ samples.

### Two-Tier Baseline: 28-Day vs 14-Day Window

The baseline lookup uses two tiers:

**Primary (28-day, time-specific):**
```sql
SELECT AVG(avg_delay), AVG(std_delay), SUM(sample_count)
FROM restaurant_delay_hourly
WHERE restaurant_id = $1
  AND hour_of_day = $2 AND day_of_week = $3
  AND bucket > NOW() - INTERVAL '28 days'
```
This is the `restaurant_delay_hourly` continuous aggregate, which TimescaleDB pre-computes. Zero cold aggregation at query time — the result is a materialised view scan. The 28-day window captures full weekly cycles (4 complete weeks), giving the baseline stability across day-of-week and seasonal variation.

**Fallback (14-day, any-time):**
```sql
SELECT AVG(delay_mins), STDDEV(delay_mins)
FROM restaurant_delay_events
WHERE restaurant_id = $1 AND timestamp > NOW() - INTERVAL '14 days'
```
The fallback uses a raw table scan with a shorter 14-day window. A shorter window is used because a raw scan on 28 days of data for a busy restaurant could be expensive — the continuous aggregate handles the 28-day case efficiently. The fallback only fires when no time-specific data exists (new restaurant, off-peak hour with no events), so its performance impact is rare.

**Why different windows (28 vs 14):** The 28-day window for the aggregate captures day-of-week patterns reliably (at least 4 Mondays, 4 Fridays, etc.). The 14-day window for the raw fallback is a cost/benefit tradeoff: longer would be more stable but slower, shorter would be less stable but faster. 14 days catches the most recent operational state (new slow kitchen equipment, new cook) while being short enough to be responsive to improvements.

### `wait_anchor`: Pickup Wait Time Signal

```python
anchor    = row["rider_inbound_at"] or row["assigned_at"]
wait_mins = (now - anchor).total_seconds() / 60
```

When the rider has physically arrived at the restaurant (`rider_inbound_at IS NOT NULL`), the wait time is measured from arrival. When the rider is still en route (`rider_inbound_at IS NULL`), the wait time is measured from order assignment. This is the correct signal: the Restaurant Intelligence agent is interested in how long a rider is being held at or waiting for a restaurant, not how long the order has been in the system overall.

### `score_assignment`: ML Feature Assembly for Dead Zone Risk

The `score_assignment` function assembles the nine features required by Model 3 (Dead Zone Risk Predictor) for a specific order-rider pair:

```python
{
    "dest_zone_type_enc":     ZONE_TYPE_ENC.get(zone_type, ZONE_TYPE_ENC_DEFAULT),   # hub/commercial/residential/peripheral → 0/1/2/3
    "city_tier_enc":          CITY_TIER_ENC.get(city_tier, CITY_TIER_ENC_DEFAULT),  # Metropolitan/Urban/Semi-Urban → 0/1/2
    "hour_of_day":            now.hour,
    "day_of_week":            now.weekday(),
    "is_weekend":             1 if now.weekday() >= 5 else 0,
    "is_ld_order":            1 if is_long_distance else 0,
    "dist_from_home_zone_km": haversine(rider_home, delivery_zone),
    "current_density_ratio":  latest density_score at delivery_zone,
    "historical_dead_rate":   fraction of snapshots at delivery_zone with stress_ratio < 0.5 (last 14 days),
}
```

The `historical_dead_rate` defaults to **0.3** when no history exists — the pessimistic prior (Section 2.6.3). The `dest_zone_type_enc` and `city_tier_enc` both import from `constants.py` — the single encoding source of truth that ensures training-time and inference-time encodings are identical (Section 2.6).

---

*End of Section 4. Next: Section 5 — ML Models.*

---

# 5. ML Models

ARIA uses four XGBoost models trained on a mix of real and synthetic data. Each model is a dedicated specialist — it does one job and does it well. This section covers the shared engineering decisions that apply to all four models, then each model individually.

---

## 5.0 All Four ML Models — Summary

ARIA has exactly four XGBoost models, each a different task type: **Model 1** (Rider Persona Classifier) — XGBoost binary classifier; predicts `supplementary` vs `dedicated` rider type from first 5–10 ride signals; trained on synthetic data grounded in Loadshare article statistics; serves `/internal/predict/persona`. **Model 2** (Delivery Duration Scorer) — XGBoost regressor; predicts `Time_taken(min)` per delivery using route, weather, traffic, rider features; the only model trained on real data (Kaggle `gauravmalik26` food delivery dataset, 41,953 rows, 22 Indian cities); serves `/internal/predict/duration`. **Model 3** (Dead Zone Risk Predictor) — two-stage: XGBoost classifier (is this zone a dead zone?) + XGBoost regressor (estimated stranding duration); uses interaction features `peripheral_ld_risk` and `dist_x_dead_rate`; calibrated with `CalibratedClassifierCV` (isotonic regression) for reliable probability outputs; serves `/internal/predict/dead-zone`. **Model 4** (Earnings Trajectory Forecaster) — two-stage: XGBoost regressor (projected final EPH) + XGBoost classifier (will EPH fall below 90 Rs/hr?); uses momentum features `eph_slope` and `eph_acceleration`; `eph_target` is injected into regressor features but excluded from classifier features to prevent label leakage; serves `/internal/predict/earnings-trajectory`. All models stored in `services/ml-server/models/`, served via internal FastAPI port 8002, SHAP importances pre-computed and served from `shap_importance.json`.

---

## 5.1 Shared: Why XGBoost — All Alternatives Considered

The ML model selection was not arbitrary. Before choosing XGBoost, the following alternatives were evaluated for ARIA's specific constraints: **tabular data, small-to-medium dataset size (5k–50k rows), interpretability requirement, and rapid training iteration**.

### vs LightGBM

LightGBM uses a **leaf-wise** tree growth strategy (best leaf first) vs XGBoost's **level-wise** (all nodes at a depth level). This makes LightGBM faster on very large datasets (millions of rows). However:
- On ARIA's 5k–50k synthetic rows, the training time difference is negligible (both train in under 60 seconds)
- Leaf-wise growth on small datasets can produce unbalanced trees with high variance — LightGBM needs explicit `num_leaves` and `min_data_in_leaf` tuning to avoid this, adding complexity
- LightGBM's SHAP integration works via `shap.TreeExplainer`, but the `CalibratedClassifierCV` wrapper for probability calibration (needed for Model 3) is less well-tested on LightGBM

### vs CatBoost

CatBoost's main advantage is native handling of categorical features (ordered encoding that prevents target leakage). This is irrelevant for ARIA: all categorical features are pre-encoded as integers via `constants.py` before the model ever sees them. CatBoost would offer no advantage here and adds a dependency with significantly more complex hyperparameter names and less community support for SHAP integration.

### vs Random Forest

Random Forest is a bagging ensemble: each tree is trained on a random subset of data independently, and predictions are averaged. This produces high variance on small datasets (each tree sees only ~63% of data with bootstrap sampling). XGBoost's boosting sequentially corrects the previous model's errors, making better use of limited data. On ARIA's class-imbalanced Model 3 dataset (12.8% positive rate), Random Forest's `class_weight` mechanism is less principled than XGBoost's `scale_pos_weight` parameter.

### vs Logistic/Linear Regression

Logistic regression assumes a linear decision boundary in the feature space. The features ARIA uses have non-linear interactions: `dist_from_home_km × historical_dead_rate` (Model 3's `dist_x_dead_rate` interaction feature) is definitionally non-linear. Similarly, EPH trajectory (Model 4) involves the interaction between `eph_slope`, `time_remaining`, and `current_eph` — a linear model would require explicit polynomial feature engineering to capture this. XGBoost discovers these interactions automatically through tree splits.

### vs Neural Networks (MLPs, LSTMs, Transformers)

For tabular data at ARIA's scale (up to ~50k rows), XGBoost consistently outperforms deep learning in the literature. The intuition: neural networks require large amounts of data to learn feature representations. On small tabular datasets, XGBoost's inductive bias (axis-aligned splits over the actual feature values) is better suited than MLP's continuous representation learning. A transformer or LSTM for time-series EPH trajectory would require sequential session data per rider formatted as a time series — ARIA's data is stored as independent session rows, not sequences. Engineering the data into LSTM format would add complexity for marginal gain.

**Why XGBoost won:**
1. Tabular data of this size is XGBoost's documented strong suit
2. `CalibratedClassifierCV(isotonic)` works out-of-the-box for Model 3 probability calibration
3. `shap.TreeExplainer` produces exact SHAP values for tree models — not approximations
4. SHAP feature importances are the primary "why did this rider get an intervention?" explainability signal for interviewers
5. Training time: all four models train in under 5 minutes total on CPU

---

## 5.2 Shared: Hyperparameter Search Strategy

All four models use the same hyperparameter search space and strategy:

**Search space:**
```python
param_grid = {
    'max_depth':        [3, 4, 5, 6],
    'learning_rate':    [0.05, 0.1, 0.2],
    'n_estimators':     [100, 200, 300],
    'subsample':        [0.8, 1.0],
    'colsample_bytree': [0.8, 1.0],
    'min_child_weight': [1, 3, 5],
}
```

**30 random iterations** rather than exhaustive grid search. A full grid would be `4×3×3×2×2×3 = 432` combinations. Random search samples 30 random combinations from this space. Research (Bergstra & Bengio 2012) shows that random search over 30 iterations finds near-optimal configurations for most hyperparameter spaces in a fraction of the time. Early stopping (20 rounds) prevents overfitting within each iteration.

**Retrain on train+val with best iteration:** After the search finds the best parameters, the final model is retrained on `train + val` combined with `n_estimators = best_model.best_iteration + 1`. This ensures the final model sees more data (85% of total) while using the `n_estimators` found optimal during early stopping — not an arbitrary fixed number. The `+1` because `best_iteration` is 0-indexed.

**Duplicate `n_estimators` bug fix:** `best_params` from the search includes `n_estimators`. When constructing the final model with `n_estimators = best_n, **best_params`, Python raises `TypeError: duplicate keyword argument`. Fix: strip `n_estimators` from `best_params` before spreading:
```python
**{k: v for k, v in best_params.items() if k != 'n_estimators'}
```

---

## 5.3 Shared: Train/Val/Test Splits

| Model | Method | Reason |
|-------|--------|--------|
| Model 1 (Persona) | **Stratified** 70/15/15 | 80/20 class imbalance — random split could give val with 0 dedicated riders |
| Model 2 (Duration) | **Chronological** 70/15/15 | Real Kaggle data with dates — random split leaks future data into train |
| Model 3 (Dead Zone) | **Random** 70/15/15 | Synthetic data with no real temporal ordering |
| Model 4 (Earnings) | **Random** 70/15/15 | Synthetic data with no real temporal ordering |

**Model 2 date reconstruction:** The Kaggle dataset stores the order date as separate `Day` and `Month` integer columns (no year column). The preprocessing script reconstructs dates as `pandas.Timestamp(year=2022, month=month, day=day)` before sorting for the chronological split. The year is arbitrary (2022) because only relative ordering matters for the split.

**Why not use time-based split for Models 1, 3, 4:** Synthetic data is generated with a fixed seed (`random.seed(42)`) and has no real temporal structure — rows are not ordered by any meaningful time sequence. A chronological split on synthetic data would simply split the first 70% of generated rows from the last 30%, which is statistically equivalent to a random split but would hide the equivalence and mislead readers.

---

## 5.4 Shared: Evaluation Metrics

**Regression (Models 2, 4 regressor, 3 regressor):**
- **RMSE:** Primary metric. Penalises large errors more than small ones — appropriate when a 15-minute error in delivery duration prediction is more than twice as bad as a 7-minute error.
- **MAE:** Secondary. More interpretable ("average error of X minutes").
- **R²:** Proportion of variance explained. R² < 0 means the model is worse than the mean baseline.

**Classification (Models 1, 3 classifier, 4 classifier):**
- **Weighted F1:** Primary metric. Accounts for class imbalance by weighting each class's F1 by its support. More informative than accuracy when classes are imbalanced.
- **AUC-ROC:** Measures ranking quality independent of threshold — how well the model separates classes. AUC = 0.85 means an 85% chance that a randomly chosen positive ranks above a randomly chosen negative.
- **Precision/recall:** Class-level breakdown. For Model 3 (dead zone), recall (catching true dead zones) matters more than precision (avoiding false alarms at a slight cost).
- **Confusion matrix:** Visual verification that the model isn't predicting only the majority class.

**Calibration (Model 3 only):**
- **Calibration curve (10 bins):** Bins predictions by probability decile and plots predicted probability vs actual fraction. A well-calibrated model lies on the diagonal.
- **Brier score:** Mean squared error between predicted probability and actual binary outcome. Lower is better. Random classifier: 0.25. Perfect classifier: 0.0.

---

## 5.5 Shared: SHAP Importances — Pre-Computed, Not Runtime

```python
shap_importance = compute_shap_importance(final_model, X_test, FEATURE_COLS)
# Capped at n_samples=500
```

SHAP values are computed once at training time on a 500-sample subset of the test set. This 500-sample cap produces stable mean |SHAP| rankings (global importance is stable with 500 samples) while keeping training time under 30 seconds per model. The result is saved as `shap_importance.json` alongside `model.joblib`.

At inference time, the ML server returns the top 3 SHAP features from this pre-computed dict for every prediction. These appear in the frontend as `key_factors` on risk cards — "Dead zone risk driven by: peripheral_ld_risk, dist_x_dead_rate, historical_dead_rate". Computing SHAP values per-request would add ~200ms latency for a 300-rider cycle with 0 value over pre-computed global rankings.

---

## 5.6 Shared: XGBoost 2.x SHAP Compatibility Fix

XGBoost ≥ 2.0 changed how `base_score` is serialized in the model's JSON config. It now stores it as `[9.294E1]` (bracket notation) rather than the plain float `92.94`. SHAP's `TreeExplainer` calls `float()` on this string directly, which raises `ValueError: could not convert string to float: '[9.294E1]'`.

**Fix in `_patch_xgb_booster()`:**
```python
raw     = str(lmp['base_score'])           # '[9.294E1]'
cleaned = re.sub(r'[\[\]]', '', raw).strip()  # '9.294E1'
lmp['base_score'] = cleaned                 # patch in place
booster.load_config(json.dumps(cfg_dict))   # reload patched config
```

The patch is applied before creating `shap.TreeExplainer(patched)`. It also handles `CalibratedClassifierCV` wrappers by unwrapping the inner XGBoost estimator first. The patch is best-effort: if config patching fails (unknown XGBoost version), it passes through and SHAP will fail naturally with its original error.

---

## 5.7 Shared: sklearn 1.6 `FrozenEstimator` Compatibility Fix

`CalibratedClassifierCV(estimator=model, cv='prefit')` was deprecated in sklearn 1.6, which requires using `FrozenEstimator` instead:

```python
try:
    # sklearn >= 1.6
    from sklearn.frozen import FrozenEstimator
    calibrated = CalibratedClassifierCV(
        estimator = FrozenEstimator(base_model),
        method    = "isotonic",
    )
except ImportError:
    # sklearn < 1.6
    calibrated = CalibratedClassifierCV(
        estimator = base_model,
        method    = "isotonic",
        cv        = "prefit",
    )
```

The try/except handles both versions in the same codebase. `FrozenEstimator` prevents sklearn from re-fitting the base model during calibration — critical because the base model has already been fitted with early stopping and the original training data.

---

## 5.8 Shared: `_NumpyEncoder` — Type Serialization Fix

XGBoost returns `np.bool_` (boolean predictions) and `np.int64` (feature importances). Python's `json.dumps()` does not handle these types natively — it raises `TypeError: Object of type bool_ is not JSON serializable`.

All model artifact saving and inference response serialization passes through `_NumpyEncoder`:
```python
class _NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.bool_):  return bool(obj)
        if isinstance(obj, np.int64):  return int(obj)
        if isinstance(obj, np.float64): return float(obj)
        return super().default(obj)
```

---

## 5.9 Model 1 — Rider Persona Classifier

**Task:** Classify a rider as `supplementary` (part-time, peak-hour focused) or `dedicated` (full-time, high-commitment) from their first 5–10 ride signals.

**Why this matters:** EPH targets differ by persona (Rs.90 supplementary vs Rs.100 dedicated). The Earnings Guardian's health score formula, churn detection, and shortfall calculations are all persona-specific. Misclassifying a supplementary rider as dedicated causes false "critical" alerts (Rs.100 target applied to a rider aiming for Rs.90).

**Features (10 signals):**
```
n_rides_observed        — sample size (5–10)
peak_hour_rate          — fraction during lunch/dinner peaks
morning_rate            — fraction during 7–11am
night_rate              — fraction during 10pm–6am
n_distinct_zones        — zone drift (dedicated riders roam more)
acceptance_rate         — fraction of pings accepted
ld_rejection_rate       — fraction of long-distance pings rejected (supplementary reject LD more)
avg_shift_hours         — shift duration (supplementary 3–5h, dedicated 8–12h)
off_peak_acceptance     — acceptance during off-peak (key discriminator: 0.12 supp vs 0.75 ded)
avg_orders_per_shift    — throughput (8 supp vs 18 ded)
```

**Class balance:** 80% supplementary, 20% dedicated — directly from the Loadshare article. Model 1 uses a **stratified split** to preserve this ratio in train/val/test.

**The F1=1.0 bug and fix:**

Initial training produced perfect F1=1.000 on the test set. This was not a success — it was a failure. The synthetic features were too clean: `off_peak_acceptance` was 0.12 for supplementary and 0.75 for dedicated with Gaussian noise σ=0.10. The gap is ~5σ. Any decision tree with a single split on this feature achieves perfect separation.

**Fix — two-step overlap injection:**

*Step 1 — Per-feature Gaussian noise:*
```python
noise_stds = {
    "off_peak_acceptance": 0.10,
    "avg_shift_hours":     0.80,
    "ld_rejection_rate":   0.10,
    ...
}
```
This softens the edges of each distribution but does not merge them — the clusters are still ~5σ apart after noise.

*Step 2 — Hard class swaps (`SWAP_RATE=0.12`):*
```python
for row in noisy:
    if rng.random() < SWAP_RATE:
        label = int(row["persona_label"])
        src   = ded_dist if label == 0 else supp_dist   # opposite class
        for col, (mean, std) in src.items():
            row[col] = float(rng.normal(mean, std * 1.5))   # extra 1.5× spread
```

12% of rows have their features replaced with samples drawn from the **opposite** class's distribution with 1.5× spread. This creates genuinely ambiguous riders (supplementary riders with dedicated-like shift hours, dedicated riders with supplementary-like acceptance rates). The `1.5× spread` deliberately broadens the swap distribution to create the most challenging overlap.

**Note on SWAP_RATE:** The code comment says "20% of rows" but the constant is `SWAP_RATE=0.12` (12%). The 20% figure refers to the intended outcome (15–20% total class overlap after both noise + swaps), not the per-row probability.

**Target F1:** 0.90–0.95. Values above 0.96 trigger a warning.

---

## 5.10 Model 2 — Delivery Duration Scorer

**Task:** Predict delivery duration in minutes from route, weather, traffic, and rider features.

**Real data only:** As explained in Section 2.2, delivery duration requires learning real GPS-trace-derived patterns across 22 Indian cities. No synthetic data source can replicate the multi-city, multi-condition distribution of the Kaggle dataset.

**17 features** after feature engineering:
- Distance (dominant predictor, expected based on physics)
- Delivery person age and ratings
- Order hour, day of week
- Weather condition (encoded)
- Traffic density (encoded)
- Vehicle type (binary: bike vs other)
- City tier (encoded from City label)
- Peak hour flag

**Raw coordinates dropped:** The original dataset contains `Restaurant_latitude`, `Restaurant_longitude`, `Delivery_location_latitude`, `Delivery_location_longitude`. These are dropped because:
- An XGBoost model would memorise specific restaurant-to-neighbourhood routes (Delhi's 28.6° lat + 77.2° lng → 25-minute routes)
- This cannot generalise to new cities or relocated restaurants
- Distance (haversine-computed from the coordinates) captures the causal signal without memorising geographic specifics

**Expected RMSE floor: 2–3 minutes.** The Kaggle target `Time_taken(min)` is stored as an integer. Even a perfect model predicting 27.7 minutes for a 28-minute delivery contributes 0.3 minutes of irreducible error. The RMSE floor due to integer rounding is approximately `0.5/√3 ≈ 0.29 minutes`, but measurement noise in real delivery data produces a practical floor of 2–3 minutes.

**Role in ARIA:** Model 2 is not called in the main agent cycle. It is available via `/internal/predict/duration` for the Restaurant Intelligence agent as a cross-validation signal — if Model 2 predicts 35 minutes for a route and the restaurant's actual prep + delivery is taking 55 minutes, the 20-minute overage is attributable to the restaurant's delay contribution. In the current implementation, the restaurant agent uses the algorithmic z-score approach (Section 4.3) directly rather than calling Model 2, but the model is available for future use.

---

## 5.11 Model 3 — Dead Zone Risk Predictor

**Task:** Given an order about to be assigned to a rider, predict: (1) probability the delivery zone becomes a dead zone (stranding the rider), (2) if high risk, expected stranding duration in minutes.

**Two-stage design: classifier then regressor**

The classifier produces a calibrated probability `[0,1]`. If `probability ≥ DEAD_ZONE_THRESHOLD (0.55)`, the regressor predicts `expected_stranding_mins`. The regressor is only called when the classifier says "high risk" — no point predicting stranding time for a safe zone.

**Positive rate derivation:**
```
POSITIVE_RATE = 0.128   (12.8% of orders result in dead zone stranding)

Definition of positive:
  stress_ratio < 0.50 (zone below dead zone threshold)
  AND
  density_score < 0.30 (zone truly undersupplied)

Both conditions required — a low-stress zone that still has reasonable density
is not operationally dead. The two-condition gate produces 12.8% positive rate.

scale_pos_weight = (1 − 0.128) / 0.128 = 0.872 / 0.128 ≈ 6.8
```

`scale_pos_weight = 6.8` means that each positive training example counts as 6.8× a negative example. This compensates for the 12.8% positive rate, preventing the model from predicting "safe" for all orders (which would achieve 87.2% accuracy but 0% recall on dead zones).

**Interaction features:**

Raw features with Model 3 achieve AUC ~0.78. Two interaction features push this to AUC 0.85+:

```python
peripheral_ld_risk = (dest_zone_type_enc / 3.0) × is_ld_order
```
Rationale: The Loadshare article states 90% of long-distance orders to peripheral zones create dead zone risk. A peripheral zone (type_enc=3, normalised=1.0) combined with a long-distance order (is_ld=1) produces `peripheral_ld_risk=1.0`. A hub zone (type_enc=0) or non-LD order produces 0. XGBoost cannot easily learn this multiplicative interaction from individual features; making it explicit as a feature saves many tree splits.

```python
dist_x_dead_rate = dist_from_home_zone_km × historical_dead_rate
```
Rationale: A rider 10km from home in a zone with 30% historical dead rate (`dist_x_dead_rate=3.0`) faces far more stranding risk than a rider 2km from home in the same zone (`dist_x_dead_rate=0.6`). Distance amplifies the prior. Again, XGBoost must learn this interaction implicitly from two separate features; the explicit product cuts the required tree depth in half.

**`historical_dead_rate` is a prior, not a label:** The prior independence check at training time verifies that `corr(historical_dead_rate, is_dead_zone) < 0.90`. If the correlation were >0.90, the prior would essentially be the label (leakage). The acceptable correlation (~0.4–0.6) means the prior predicts risk but doesn't determine it — the model still learns from the combination of all features.

**Calibration: `CalibratedClassifierCV(isotonic)`**

Uncalibrated XGBoost probabilities tend to be overconfident — a model that outputs 0.8 may have a true positive rate of only 0.5 at that threshold. In an alerting system, this matters critically:
- If the model outputs 0.7 but the true rate is 0.4, agents over-alert
- Riders who receive too many false-positive dead zone warnings start ignoring them
- This is the same failure mode as the unaddressed restaurant ripple: repeated false alarms erode the signal

Isotonic regression calibration maps raw model outputs to calibrated probabilities by fitting a non-decreasing step function between predictions and actuals on the training+val set. After calibration, a predicted probability of 0.55 should correspond to a ~55% true positive rate.

The threshold `DEAD_ZONE_THRESHOLD=0.55` is never re-adjusted in the agent layer — it reflects the calibrated boundary between "likely safe" and "likely dead." Agents that re-threshold would effectively uncalibrate the model.

**Regressor: trained only on positive class (~1,923 rows)**

```
Total rows ≈ 15,000
Positive rows = 15,000 × 0.128 ≈ 1,923
```

There is no value in training a stranding-time regressor on negative examples: the stranding time for a safe zone delivery is 0 by definition. Including negative examples would force the regressor to learn to predict 0 for negatives and non-zero for positives simultaneously — a classification-regression mixed task. Training only on positives lets the regressor focus entirely on "given we know this is a dead zone, how long will the rider be stranded?"

---

## 5.12 Model 4 — Earnings Trajectory Forecaster

**Task:** Given a rider mid-shift, predict: (1) their projected final EPH at end of shift, (2) whether they will finish below their EPH threshold.

**Two-stage design: regressor → classifier**

The regressor (`XGBRegressor`) predicts the continuous `projected_final_eph` value. The classifier (`XGBClassifier`) predicts the binary `below_threshold` flag. These are two separate models trained on two separate feature sets.

**Momentum features:**
```python
eph_slope        = current_eph − eph_lag1_30min    # direction (positive = improving)
eph_acceleration = eph_lag1_30min − eph_lag2_60min  # rate of change
```

These are computed at both training time and inference time from the rider's recent health snapshots. The analogy: a car driver who knows their current speed (current_eph), their speed 30 minutes ago (lag1), and their speed 60 minutes ago (lag2) can infer whether they are accelerating or decelerating. Slope=−5 means "losing 5 Rs/hr per 30-minute window" — a rider declining from 93 → 88 → 83 → 78. Acceleration=−3 means the decline is itself accelerating.

**Lag EPH from health snapshots:**

The earnings agent fetches the 3 most recent health snapshots per rider from `rider_health_snapshots`. Each snapshot is written by the Supervisor at the end of each 15-minute agent cycle, so 3 snapshots ≈ 45 minutes of trajectory window. The snapshots are ordered by `timestamp DESC LIMIT 3`, mapped to `eph_lag1/lag2/lag3` in order.

**Stale lag handling:**
```python
_LAG_STALE_MINS = 30
# If a lag snapshot is older than 30 minutes: pad with current_eph
# This makes slope=0 (stable prior) rather than using a potentially
# misleading old value from a prior session
```

**Why `eph_target` in regressor but NOT in classifier:**

The classifier's target is `below_threshold = projected_eph < eph_target`. If `eph_target` is included as a classifier feature:
- A rider with a high `eph_target` (dedicated, Rs.110) will almost always have `below_threshold=True` relative to that target
- The model learns "high eph_target → predict below_threshold=1" — a spurious shortcut
- This is not learning trajectory; it is reading the label definition

The regressor does not have this problem because it predicts the absolute `projected_eph` value, not whether it is above/below a threshold. Including `eph_target` in the regressor is genuinely informative: knowing a rider aims for Rs.95 vs Rs.90 helps predict whether they push harder later in their shift.

**Training targets vs runtime thresholds:**

| Context | Supplementary threshold | Dedicated threshold |
|---------|------------------------|---------------------|
| Training labels (`below_threshold`) | Rs.95 | Rs.110 |
| Runtime alert (`EPH_THRESHOLD`) | Rs.90 | Rs.90 (unified) |

The training data uses higher thresholds (Rs.95 supp, Rs.110 ded) because these reflect the full EPH targets from the Loadshare article — the model is trained on what a "healthy" vs "struggling" trajectory looks like at full target. The runtime alert threshold is unified at Rs.90 for operational simplicity. This means the classifier's `below_threshold` output at inference is a conservative flag — it fires when the rider is projected below their full target, not just the alert boundary.

**Alert level compound logic:**
```python
if projected < 80.0 or (below_threshold and eph_trend == "declining"):
    alert_level = "intervene"
elif projected < EPH_THRESHOLD or eph_trend == "declining":
    alert_level = "watch"
else:
    alert_level = "none"
```

"Intervene" fires when projected EPH is critically low (< Rs.80) OR when both the binary flag and declining trend are simultaneously true. "Watch" fires for projected < Rs.90 OR for a declining trend without the critical flag. The OR logic ensures declining trends are always caught even when the current EPH is still above threshold.

**Inference fallback for corrupted base_score:**
```python
if projected < 20.0:
    projected = req.current_eph
```
An XGBoost model loaded in a different XGBoost version (1.x model loaded by XGBoost 2.x) can produce corrupted `base_score` that drives all predictions to near-zero. A projected EPH of less than Rs.20 is physically implausible (even a rider in crisis earns ~Rs.70/hr). The fallback uses `current_eph` — the current known value — as a safe minimum-information estimate rather than propagating a corrupted prediction to the health score calculation.

**No calibration on Model 4 classifier:** Unlike Model 3, Model 4's `below_threshold` output is a binary flag used as a component in the compound alert logic. The exact probability value at which it fires does not need to be calibrated to a true positive rate — it is used as a binary decision gate (`>= 0.5`), not as a probability-weighted score. Calibration adds complexity without operational benefit here.

---

*End of Section 5. Next: Section 6 — LangGraph Agents.*

---

# 6. LangGraph Agents

> **Structure reference:** This section follows `/DOCS_STRUCTURE.md` §"LangGraph Agents" exactly before writing, to ensure no subsection is missed.

The intelligence layer of ARIA is built as five LangGraph `StateGraph` pipelines that execute sequentially each cycle: Zone → Restaurant → Dead Run → Earnings → Supervisor. Each agent receives a shared PostgreSQL connection and Redis client, runs its graph to completion, writes its outputs to the database, and returns a dict that the Supervisor consumes.

---

## 6.0 All Five Agents — Summary

Five LangGraph agents run sequentially each cycle. **01 Restaurant Intelligence**: detects queue congestion anomalies vs 28-day baseline; alerts riders and operators. **02 Dead Run Prevention**: scores active orders via Model 3 for dead zone risk; proactive rider alerts. **03 Zone Intelligence**: classifies 180 zones (dead/low/normal/stressed); writes zone snapshots; repositioning recs; fires system_zone_pressure if ≥50% zones dead. **04 Earnings Guardian**: scores riders' EPH via Model 4; churn detection; alert_level watch/intervene; fleet churn alert if >15% at risk. **05 Supervisor**: cross-agent synthesis; detects compound patterns (churn_surge, dead_zone_pressure, restaurant_cascade); produces cycle_briefing with severity (low/medium/high/critical), KPIs, operator actions; Phase 2 adds episodic RAG.

## 6.1 Why LangGraph vs Raw Python Loops

The first engineering question is: why use a graph framework at all for what are essentially sequential pipelines?

**LangGraph vs plain LangChain (LCEL).** LangGraph is built *on top of* LangChain — it uses LangChain's model integrations (`ChatOpenAI`, `ChatOllama`) and tool abstractions. The question is why add LangGraph's `StateGraph` on top of LangChain Expression Language (LCEL) chains. LCEL provides composable runnable sequences but lacks explicit named state, per-node tracing boundaries, and conditional edge routing without monolithic refactoring. LangGraph's `StateGraph` gives every node a typed `TypedDict` checkpoint, automatic LangSmith per-node traces, and clean error isolation: a failure in the `synthesize` node does not corrupt the `fetch_data` or `score` nodes. For ARIA's 5-agent pipeline with 4–6 nodes per agent, this observability is non-negotiable — without it, debugging a partial failure in a 15-minute cycle would require re-running the entire cycle.

**State immutability as documentation.** LangGraph's `TypedDict` state schema forces every intermediate output to be named and typed. In a raw async function chain you would accumulate results in a dict that grows invisibly; in LangGraph each node declares what it adds and what it reads. The `RestaurantState`, `DeadRunState`, `ZoneState`, `EarningsState`, and `SupervisorState` TypedDicts serve as living specs of each agent's data flow.

**Observable node boundaries.** LangGraph records entry/exit per node. With LangSmith integration every node's input and output are logged automatically, giving cycle-level observability without hand-rolling timing or logging for each step.

**Composable conditional routing.** While all five agents happen to use linear graphs today, LangGraph's conditional edges would let us add branching without refactoring (e.g., "if zone data is stale, skip rider recommendations and go directly to synthesize"). The overhead is zero when edges are linear; the flexibility is retained for future cycles.

**LLM calls fit the graph mental model.** An agent that calls an LLM needs to handle timeouts, empty responses, and fallbacks. Having "synthesize" as an explicit node makes it clear that the LLM call is optional and isolated — a failure in synthesize does not corrupt the fetch or score nodes.

The alternative — raw `async def` chains — would work but would require disciplined naming conventions, manual state typing, and hand-written observability, all of which LangGraph provides for free.

---

## 6.2 BaseAgent Contract

All five agents subclass `BaseAgent` (`agents/base.py`). The contract has two components:

**Mandatory return schema.** `run()` must return a dict containing at minimum:

```
status        : 'success' | 'partial' | 'failed'
summary_text  : one-liner string for the Supervisor's context window
alert_count   : int
severity      : 'normal' | 'warning' | 'critical'
```

The Supervisor's `_validate_inputs()` node checks all four keys. Any agent missing them is classified as `partial`, not `ok`.

**`_log_to_db()` persistence.** Every agent writes its full output JSON, summary text, execution time (ms), and status to the `agent_memory` table after `run()` completes. This table is the audit trail for every cycle — it lets the Supervisor's Phase 2 RAG retrieve past situation data and lets post-mortem analysis see exactly what each agent concluded each cycle.

```python
INSERT INTO agent_memory
    (id, agent_name, cycle_id, output_json, summary_text, execution_ms, status)
VALUES ($1, $2, $3::uuid, $4, $5, $6, $7)
```

The `default=str` JSON serializer means datetime, UUID, and Decimal fields serialize safely without crashing on edge-case types.

---

## 6.3 Dependency Injection via Closure (not LangGraph State)

LangGraph nodes receive only the current state dict. They cannot receive external objects like a database connection or Redis client through the node signature. There are two approaches to injecting non-serializable dependencies:

1. **Store them in LangGraph state** — rejected. Database connections and Redis clients are not serializable. Putting them in state would break LangGraph's checkpointing and make the state schema misleading.

2. **Closure injection** — used in ARIA. The `_build_graph(conn, redis)` function captures `conn` and `redis` in closure scope, then defines inner async functions that wrap the real node implementations:

```python
def _build_graph(conn, redis):
    async def fetch_data(state):
        return await _fetch_data(state, conn, redis)
    g.add_node("fetch_data", fetch_data)
    ...
```

The actual `_fetch_data()` function takes `conn` and `redis` as explicit parameters. The wrapper function (used as the LangGraph node) captures them from the outer scope. This keeps the node implementations fully testable in isolation (pass any mock connection) while satisfying LangGraph's state-only node signature requirement.

---

## 6.4 Agent Design Principle: No Computation in Agents

This is the central architectural rule. **Agents never compute.** The four computational services — `algorithms/zone.py`, `algorithms/session.py`, `algorithms/restaurant.py`, and the ML server — produce all numeric results. Agents:

1. Fetch data from the database and Redis.
2. Call algorithms or the ML server.
3. Interpret the results and decide what actions to take (write alerts, generate recommendations).
4. Call the LLM once to synthesize a natural-language summary.
5. Persist outputs to the database.

The agents do not implement the EPH formula, the z-score formula, the health score calculation, or any ML inference. This separation has two consequences: the computational logic can be unit-tested without spinning up a LangGraph graph, and the agents can be changed (new alert logic, new confidence gates) without touching the math.

The only exception is that the Dead Run agent and Earnings agent replicate some inline logic for performance: `_build_ml_inputs()` in the Dead Run agent mirrors `score_assignment()` but uses cached data (avoiding per-order DB round-trips), and `_compute_churn_inline()` in the Earnings agent mirrors `compute_churn_signal()` but operates on pre-fetched bulk data. In both cases this is an explicit performance optimization with a comment explaining why the duplication is intentional.

---

## 6.5 LLM Call Budget: 4–6 Calls per 15-Minute Cycle

The LLM (Qwen2.5-32B-Instruct-GPTQ-Int4 via vLLM) is called exactly once per agent that has alert output, and once for the Supervisor. The total budget per cycle is:

| Agent | LLM Call | Purpose | max_tokens |
|---|---|---|---|
| Restaurant | Yes (if high-risk restaurants exist) | 2–3 sentence operator briefing on top-5 risks | 150 |
| Dead Run | Yes (if flagged zones exist) | 2–3 sentence dispatch briefing on zone risks | 150 |
| Zone | Yes (always, cycle-level summary) | Zone classification + repositioning narrative | 200 |
| Earnings | Yes (if at-risk riders exist) | Fleet EPH summary with cross-agent context | 200 |
| Supervisor | Yes | Full cycle briefing with RAG context | 450 |

Each LLM call is wrapped with a fallback template that activates if the response is empty (vLLM down, timeout, or empty generation). The Supervisor has a four-step fallback JSON parser for cases where the LLM returns JSON with minor formatting issues.

The rider-level intervention text (Earnings agent) is template-generated, not LLM-generated — calling the LLM once per active rider (~300 riders) would be ~300 calls per cycle, which is not feasible. The templates are parameterized with cross-agent context slots (zone recommendation text, dead run flag, EPH trajectory data) to produce specific, actionable messages without an LLM call per rider.

### An Honest Assessment: Per-Agent LLM Calls as an Expensive JSON-to-Text Function

Within the agentic pipeline specifically, it would be dishonest to oversell what the LLM is doing. **Each per-agent LLM call is, in practice, an expensive JSON-to-text function.**

Every agent already has fully computed, structured output — risk scores, ranked lists, delta values, severity classifications — produced deterministically by algorithms and ML models before the LLM is ever invoked. The LLM's only job at that point is to wrap that structured data into 2–3 readable English sentences an operator can skim. A well-parameterized string template would produce nearly identical output at zero cost.

So why use an LLM here at all? **Modularity toward future phases.** The LLM call is deliberately placed at a boundary where, in a future iteration, it could do genuinely reasoning-heavy work: cross-agent synthesis with incomplete data, ambiguity resolution when signals conflict, generating hypotheses about unusual patterns not covered by the deterministic pattern library. The current code structure — prompt template + JSON input + structured output parsing — scales naturally into that future without a refactor.

For the current agentic pipeline, the LLM contributes readable operator-facing paragraphs and not much else. The analytical weight is carried entirely by the algorithms and ML models. This is worth stating plainly — "solid deterministic foundation with a clear path to reasoning-layer integration" is a more credible position than overstating what the agents currently do with the LLM.

This assessment applies only to the per-agent narrative generation calls. Elsewhere in the system — the Supervisor's RAG-grounded synthesis and the planned documentation chatbot (PageIndex RAG) — the LLM is doing work that genuinely requires language understanding and cannot be replaced by a template.

---

## 6.6 sim_now Injection: How Simulated Time Reaches LLM Prompts

The Restaurant agent and Zone agent use simulated time (`sim_now`) — not wall-clock `datetime.now()` — when querying the historical baseline. The restaurant baseline is keyed by `(restaurant_id, hour, day_of_week)`. Using real UTC time during a simulation running at 300× speed would consistently hit the same hour bucket; using sim_now correctly traverses the full 24-hour cycle.

`sim_now` is injected from the scheduler:

```python
# scheduler.py: fetch sim_now from event-stream, then pass to each agent
r = await run_cycle(pool, redis, ws_manager)
```

The `RestaurantAgent.run()` signature accepts `sim_now: datetime | None`. If not provided, it falls back to `datetime.now(timezone.utc)` — making the code safe for production (real time) and simulation (injected time) without branching.

---

## 6.7 Why Not Concurrent Agents: Single DB Connection Design

The five agents run sequentially (Zone → Restaurant → Dead Run → Earnings → Supervisor), not concurrently. The reason is the shared `asyncpg.Connection` object. asyncpg connections are single-query-at-a-time — issuing two queries on the same connection concurrently produces a runtime error.

The alternative of giving each agent its own connection from the pool would require acquiring and releasing 5 connections per cycle. With a pool of 4–16 connections and cycles every 15 minutes, this is not a scarcity problem in production, but it introduces connection management complexity. The sequential design is simpler, and 15-minute cycles have no time pressure that demands concurrent agents.

The one genuine concurrency in the system is within the Dead Run agent's ML calls: `asyncio.gather(*[_score_one(o) for o in orders], return_exceptions=True)` under a `Semaphore(20)`, but this happens within a single node that does no DB writes during the scatter phase — it only calls the ML HTTP server.

---

## 6.8 Agent 01 — Restaurant Intelligence

**Purpose.** Detect restaurants whose current queue congestion is anomalously high relative to their per-hour historical baseline. Alert riders waiting at those restaurants and surface the risk to operators.

### Pipeline

```
fetch_data → score_all → write_scores → create_alerts → synthesize
```

**Node 1 — fetch_data.**
Two DB queries + one Redis pipeline in a single node.

- Query 1: all active restaurants with `avg_prep_time_mins` (the "base prep time" used in the congestion formula).
- Redis MGET: `aria:restaurant_queue:{restaurant_id}` for all restaurants in one round-trip. Returns the current in-queue order count maintained by the event-stream's dispatcher.
- Query 2: orders in `assigned` or `rider_inbound` status — used only for rider alert targeting. These riders are at or heading to a restaurant; they are the audience for `restaurant_delay` alerts.

`active_pickups_now` (the current queue count at pickup stage) is attached to the scored dict for display and sorting but **does not influence the risk score**. Risk is purely a function of historical pattern deviation.

**Node 2 — score_all: the z-score signal.**

The exact quantity being z-scored is the **congestion_extra_mins** component — the expected extra wait caused by current queue length above the restaurant's capacity, not the raw measured prep time. The formula:

```
capacity           = max(2, round(base_prep_mins / 5.0))
congestion_factor  = 1 + max(0, (queue_len − capacity) / capacity)
congestion_extra   = base_prep_mins × (congestion_factor − 1)
                   = base_prep_mins × max(0, (queue_len − capacity) / capacity)
```

The `5.0` is `PREP_TIME_PER_SLOT` (minutes per order slot) — a restaurant with a 20-minute base prep time has capacity 4 concurrent orders; a queue of 6 means 2 excess orders, producing a 50% congestion factor.

The z-score normalizes this against the restaurant's historical baseline **at the same hour and day-of-week**:

```
z_score    = (congestion_extra − baseline_avg) / max(baseline_std, 0.5)
risk_score = sigmoid(z_score) = 1 / (1 + e^(−z_score))
```

The `max(baseline_std, 0.5)` floor prevents division by near-zero variance for restaurants with perfectly consistent delay history (which would cause extreme z-scores from tiny deviations).

**Why z-score, not Model 2 (Delivery Duration)?** Model 2 predicts total delivery duration given order features. Duration includes travel time, pickup time, and drop-off time — it cannot isolate the restaurant's contribution. The z-score approach directly measures whether this restaurant's current queue is abnormal for this time slot, which is the question the agent is trying to answer.

**Confidence gate.** `sample_count < 5` → severity forced to `"low"`. New restaurants or restaurants with sparse historical data do not trigger production alerts — the baseline is not yet reliable enough. `confidence = min(sample_count / 20.0, 1.0)` is written to the `restaurant_risk_scores` table as an explicit data quality field.

**Alert thresholds.**

| Risk Score | Raw Severity | With Low Confidence |
|---|---|---|
| ≥ 0.65 (RESTAURANT_RISK_THRESHOLD) | critical | low |
| ≥ 0.50 | medium | low |
| < 0.50 | normal | normal (unchanged) |

The threshold split between `operator_alerts` (fires at medium = 0.50) and `above_threshold_count` (counts at critical = 0.65) was a specific bug that required fixing. The Supervisor reads `above_threshold_count` as its proxy for "high-risk restaurants" — it must match what the frontend panel shows (which displays rows with `delay_risk_score ≥ 0.65`).

**Cooldown.** A 30-minute cooldown per restaurant prevents the same restaurant from flooding the alert stream across consecutive cycles. The check is a DB read for any unresolved `restaurant_delay` alert created within the last 30 minutes.

**Two alert targets.**
- `rider_alerts` (type: `restaurant_delay`): one per rider currently waiting at a high-risk restaurant. Carries deviation_mins, z_score, risk_score, and current wait_mins in `metadata_json`.
- `operator_alerts` (type: `restaurant_high_risk`): one per restaurant. Carries the full scoring context including sample_count and active_pickups_now.

---

## 6.9 Agent 02 — Dead Run Prevention

**Purpose.** Score all active orders against Model 3 (Dead Zone Risk Predictor) to identify riders heading toward zones that will strand them after delivery. Alert those riders proactively and surface zone-level pressure to operators.

### Pipeline

```
fetch_orders → score_orders → write_scores → create_alerts → synthesize
```

**Node 1 — fetch_orders: bulk fetch design.**

The agent uses 3 queries + 1 Redis pipeline and zero per-order queries. This is achievable because all the data needed for ML feature assembly can be keyed by `zone_id` (zone density, historical dead rate) or is embedded in the order row itself (distance, weather, traffic, persona).

- **Main JOIN query:** orders in `('assigned', 'rider_inbound', 'picked_up')` joined with delivery zone metadata, rider home zone, and a `LATERAL` subquery for the rider's open session `dead_runs_count`. The lateral join fetches the single most-recent open session per rider without N additional queries.
- **Redis pipeline:** `HGET aria:zone_density:{zone_id}:density_score` for all unique delivery zones in one round-trip. Falls back to `zone_density_snapshots` DB table for zones with cache misses.
- **Historical dead rate query:** single `IN` query for all unique zone IDs, computing `COUNT(snapshots where stress_ratio < 0.5) / COUNT(*)` over the last `DEAD_ZONE_HISTORY_DAYS` days.

**Score window rationale.**

| Status | Alert Eligible | Why |
|---|---|---|
| `assigned` | Yes | Rider committed, hasn't departed yet |
| `rider_inbound` | Yes | En route to restaurant, can still plan re-route |
| `picked_up` | Analytics only | In transit to drop-off, alerting too late but zone data is valid |
| `pending` | Excluded | Event-stream dispatches in seconds; no pending orders at 15-min cycle time |

**Node 2 — score_orders: concurrent ML with Semaphore.**

All orders are scored concurrently under `asyncio.Semaphore(20)`, matching the httpx `max_connections=20` configured in the ML client. This ensures we never queue more concurrent HTTP requests than the ML server's connection pool can handle — a deliberate capacity alignment.

`asyncio.gather(..., return_exceptions=True)` means one ML server timeout does not abort the batch. ML failures produce a safe fallback row (`is_high_risk=False`, `dead_zone_probability=0.0`, `ml_failed=True`). The failure count is tracked; if `>50%` of scored orders had ML failures, the agent status is downgraded to `"partial"`.

**Critical design note:** the agent uses `ml_result["is_high_risk"]` directly from the ML server response. It does not re-apply `DEAD_ZONE_RISK_THRESHOLD` to `dead_zone_probability`. The ML server uses the 0.55 calibrated boundary (from CalibratedClassifierCV isotonic regression). Re-thresholding in the agent would override the calibration and produce systematically wrong decisions.

**ML feature assembly (inline, not score_assignment()).**

The `_build_ml_inputs()` function assembles the Model 3 feature dict from pre-fetched data. It mirrors `score_assignment()` in `algorithms/restaurant.py` but reads from the cached state dicts rather than making DB calls per order. The key features:

```python
{
    "dest_zone_type_enc":     ZONE_TYPE_ENC.get(dest_zone_type, ZONE_TYPE_ENC_DEFAULT),
    "city_tier_enc":          CITY_TIER_ENC.get(dest_city_tier, CITY_TIER_ENC_DEFAULT),
    "hour_of_day":            hour,
    "day_of_week":            dow,
    "is_weekend":             1 if dow >= 5 else 0,
    "is_ld_order":            1 if is_long_distance else 0,
    "dist_from_home_zone_km": haversine_km(home, dest),
    "current_density_ratio":  zone_density_cache.get(zone_id, 0.0),
    "historical_dead_rate":   zone_dead_rate_cache.get(zone_id, 0.3),
}
```

`ZONE_TYPE_ENC_DEFAULT = 2` (residential) and `historical_dead_rate default = 0.3` are conservative priors for unknown zones — residential is a mid-risk type, 0.3 is the pessimistic prior used in training.

**Persona-specific EPH for cost computation.** When computing `earnings_lost_rs` for high-risk orders, `compute_dead_run_cost()` is called with `EPH_TARGET_DEDICATED = Rs.100` for dedicated riders and `EPH_TARGET_SUPPLEMENTARY = Rs.90` for supplementary riders. This produces a more accurate per-rider earnings-at-risk figure than the flat `ASSUMED_EPH_RS_PER_HR = 82.0` default.

**Node 3 — write_scores: zone aggregation and evidence gate.**

Every scored order writes one row to `order_risk_scores`. Zone-level aggregation then builds `flagged_zones` from orders sharing a `delivery_zone_id`:

```
risk_level            = avg(dead_zone_probability) across all orders to zone
expected_stranding_mins = max(expected_stranding_mins) across flagged orders
```

Evidence gate before writing a `dead_zone_snapshot` or operator alert:

```
max_risk >= 0.75                                              (single high-confidence order)
OR (max_risk >= DEAD_ZONE_RISK_THRESHOLD AND flagged_count >= 2)   (two corroborating signals)
```

This prevents a single anomalous order from polluting the zone-level snapshot record. Only zones passing this gate are stored in `flagged_zones` and eligible for operator alerts.

**Node 4 — create_alerts: dual cooldown and system pressure.**

Rider alerts use a `(rider_id, delivery_zone_id)` cooldown key — the same rider can receive alerts about different zones, but the same rider+zone combination is suppressed for 30 minutes. Session escalation: `session_dead_runs ≥ 1` → severity upgraded to `"critical"` and a stronger message noting the rider's existing dead run count.

Operator alerts use a zone-level cooldown key. Severity: `"critical"` if `avg_risk ≥ 0.75`, else `"warning"`.

System pressure: if `flagged_orders / total_scored ≥ 0.50`, a single `system_dead_zone_pressure` operator alert fires regardless of zone-level evidence gates. This is the city-level collapse signal that bypasses per-zone filtering — if half of all active orders are heading toward dead zones, the problem is platform-wide, not zone-specific.

---

## 6.10 Agent 03 — Zone Intelligence

**Purpose.** Classify all 180 active zones by demand state, write `zone_stress_snapshots` for the frontend heatmap, and generate per-rider zone repositioning recommendations. This is the direct implementation of Loadshare's "Sister Zone Revolution" — live-ranked alternative zones replacing static assignments.

### Pipeline

```
fetch_state → classify_zones → write_zone_snapshots → write_recommendations → synthesize
```

**Node 1 — fetch_state: single LATERAL JOIN for all 180 zones.**

A single SQL query fetches all 180 zones with their latest snapshot data using a LATERAL subquery:

```sql
LEFT JOIN LATERAL (
    SELECT density_score, stress_ratio, order_count, order_delta, snapshot_ts
    FROM zone_density_snapshots
    WHERE zone_id = z.id
    ORDER BY timestamp DESC
    LIMIT 1
) snap ON TRUE
```

This replaces 180 separate `compute_zone_stress()` calls. The algorithm functions in `algorithms/zone.py` are per-zone DB functions — useful for one-off tool queries but not viable for a full 180-zone batch.

Rider state comes from Redis, not the DB. The event-stream updates `aria:rider_state:{rider_id}` on every status transition in real time. DB `rider_sessions` only records session open/close — it does not track minute-to-minute idle/assigned/delivered status. Redis is the authoritative source for "is this rider idle right now?"

**Node 2 — classify_zones: freshness gate, stress levels, sister ranking.**

Freshness gate: if a zone's `snapshot_ts` is older than `STALE_SNAPSHOT_MINS = 20` minutes, the zone is classified as `"unknown"` and excluded from rider recommendations. This prevents recommending moves based on stale data when the event-stream is down or behind.

Stress level classification:

```python
def _stress_level(stress_ratio: float) -> str:
    if stress_ratio < 0.5:  return "dead"
    if stress_ratio < 0.8:  return "low"
    if stress_ratio < 1.2:  return "normal"
    return "stressed"
```

Sister zone ranking happens in Python from the in-memory `zones_dict` — no per-zone DB calls. For each dead/low zone needing recommendations:

1. Read `sister_zone_ids` (pre-fetched, set during zone seeding).
2. Filter sisters: same city, not itself dead (`stress_ratio > 0.45`), freshness gate passes.
3. Compute `adjusted_density = density_score × zone_type_multiplier`.
4. Filter: `density_gain >= 0.10` (adjusted density gain after current zone), `distance_km <= 7.0 km`.
5. Sort by `adjusted_density` descending. Take top 2.

Zone type multipliers: `hub=1.15, commercial=1.05, residential=1.0, peripheral=0.9`. The spread is intentionally narrow (not 1.3/1.1/1.0/0.8). A wider spread would let a hub zone at density 0.3 outrank a residential at density 0.6 — but the residential zone has double the actual order activity. The tight spread balances zone quality signal against real demand volume.

**Two-threshold design (stateless hysteresis).**

```
DEAD_ZONE_STRESS_THRESHOLD = 0.50    # used for is_dead_zone DB flag
_DEAD_ZONE_RECOMMENDATION_THRESHOLD = 0.45  # used for rider recommendation trigger
```

A zone at `stress_ratio = 0.47` is technically dead but borderline. Recommending a rider move at 0.47, then withdrawing it at 0.52 on the next cycle, then recommending again at 0.48 creates alert fatigue. The 0.45 threshold only triggers recommendations for zones clearly in the dead range. This is stateless (no cycle-to-cycle history needed) — the two thresholds create a dead band in one pass.

**`expected_impact = density_gain × 10`**

The per-recommendation expected impact is computed as `density_gain × 10`, where `density_gain` is the difference in adjusted_density between the recommended sister zone and the current dead zone. The scaling factor `10` is the calibrated expectation that `density_score = 1.0` corresponds to approximately 10 orders per hour. This converts the abstract density gain into a concrete "~N extra orders/hr" estimate for the rider.

**Three-tier urgency classification.**

| Urgency | Condition |
|---|---|
| `monitor` | Dead zone, but `order_delta > 0` (orders appearing, zone may self-recover) |
| `immediate` | Idle rider in dead zone with `order_delta <= 0` (no recovery signal) |
| `post_delivery` | Engaged rider in dead zone, or idle rider in low zone |

**Stressed zone operator alert gate: `stress_ratio > 1.2 AND order_delta > 0`.**

A zone at permanently high stress (structural state, happens every evening) should not generate an alert every cycle. The `order_delta > 0` condition requires that demand is actively growing, distinguishing a genuine surge from a structurally busy zone.

**No ML server call.** Zone classification is entirely algorithmic. `stress_ratio` from the historical baseline is the signal. There is no ML model for zone demand prediction — the Loadshare article's zone insight was about density patterns, not ML forecasting.

**Zone agent as primary writer of `system_zone_pressure`.** The Zone agent writes the `system_zone_pressure` operator alert directly if `dead_zone_count / total_zones >= 0.50`. This fires even if the Supervisor fails on this cycle — the most critical operational alert does not depend on the full cycle completing.

**Partial status threshold.** If `stale_zone_count > 30%` of total zones, the agent returns `status="partial"`. Stale data means recommendations are incomplete.

**Severity thresholds.** The agent computes an overall `severity` for the Supervisor from the dead zone classification:

| Condition | Severity |
|---|---|
| `dead_zone_count > 10` (> 5.6% of 180 zones) | `critical` |
| `dead_zone_count > 0` | `warning` |
| `dead_zone_count == 0` | `normal` |

The 10-zone threshold (5.6% of 180) is the calibrated boundary between "a few isolated dead zones" and "a structurally degraded cycle." Below 10, dead zones may be isolated peripheral areas that self-recover. Above 10, the pattern suggests a systemic demand collapse across multiple city areas — the Supervisor needs to treat the cycle as critical. The `warning` lower bound is `> 0` because even a single confirmed dead zone is worth flagging in the briefing.

---

## 6.11 Agent 04 — Earnings Guardian

**Purpose.** Score all active riders by EPH trajectory, detect multi-session churn patterns before they materialise as app uninstalls, and surface the fleet-level earnings shortfall as a headline business metric to the Supervisor.

### Pipeline

```
fetch_riders → score_riders → write_health_snapshots → create_alerts → synthesize
```

**Node 1 — fetch_riders: three bulk queries for 300 riders.**

Three DB queries + one Redis pipeline, zero per-rider queries:

1. **Main session query:** all open rider sessions joined with rider profile (persona_type, home_zone_id) and the most-recent 3 health snapshots per rider (via `LATERAL ... ORDER BY cycle_id DESC LIMIT 3`). These 3 snapshots provide `eph_lag1_30min`, `eph_lag2_60min`, `eph_lag3_90min` for Model 4.

2. **Churn history query:** last 5 completed sessions per rider, used to compute the multi-session churn signal inline without calling `compute_churn_signal()` per rider.

3. **Cooldown query:** unresolved `earnings_below_threshold` and `churn_risk` alerts per rider — used to suppress repeated alerts within the cooldown window.

4. **Redis pipeline:** `HGET aria:zone_density:{home_zone_id}:density_score` for all active riders' home zones.

**Node 2 — score_riders: inline churn computation and Model 4.**

For each rider, the agent:

1. Computes `current_eph` from session `orders_completed × avg_fare / elapsed_hours`.
2. Calls `compute_session_health_score()` from `algorithms/session.py` (3-component: EPH 60pts + efficiency 25pts + trend 15pts).
3. Computes the churn signal inline via `_compute_churn_inline()` — a replica of `compute_churn_signal()` operating on the pre-fetched session history without per-rider DB calls.
4. Calls `predict_earnings_trajectory()` from the ML client (Model 4) to get `projected_eph` and `trajectory_flag`.

**Lag EPH approximation.** Model 4 requires `eph_lag1_30min` / `eph_lag2_60min` / `eph_lag3_90min`. At 15-minute cycle cadence, the last 3 `rider_health_snapshots` represent EPH at approximately 15/30/45 minutes ago. The model cares about trajectory shape (slope, acceleration), not exact minute labels, making this a good-enough approximation.

**Staleness handling:** if the most recent snapshot is older than `_LAG_STALE_MINS = 30` minutes (missed cycle), all three lags are padded with `current_eph` (slope = 0, stable prior). Alert severity is downgraded — the trajectory is unknown, not alarming. Snapshot writes are never suppressed; only alerts downgrade.

**Minimum observation gate (`_MIN_OBS_MINS = 20`).** Intervention alerts (`earnings_below_threshold`, `churn_risk`) are suppressed for the first 20 minutes of a session. Early-session EPH is noisy — the first order could be a long-distance dead run. Health snapshots are always written regardless of this gate. The gate only affects alert generation.

**Shortfall computation — 2-hour cap.**

```python
shortfall_rs = (eph_target − projected_eph) × min(remaining_hrs, INTERVENTION_HORIZON_HRS)
# INTERVENTION_HORIZON_HRS = 2.0
```

Projecting shortfall over the full remaining shift (up to 7 hours for a dedicated rider with 1 hour elapsed) produces an inflated number that operations cannot act on. The 2-hour cap makes `total_earnings_shortfall_rs` an actionable number: it represents the gap over the horizon where intervention is still feasible.

**Cross-agent context for churn-risk riders.** For the small subset of riders flagged as churn risk, two IN queries read the most recent `zone_recommendations` (Zone agent) and `order_risk_scores` (Dead Run agent). This enriches the intervention template:

```python
def _build_intervention_text(rider_data, scoring, churn, cross):
    # Dead run hint
    if cross["dead_run_flag"]:
        "Dead Run agent flagged a high-risk zone assignment..."
    # Zone repositioning hint
    if cross["zone_rationale"]:
        cross["zone_rationale"]
    # EPH trajectory
    if projected < target:
        f"Projected EPH: Rs.{projected:.0f}/hr vs Rs.{target:.0f}/hr target..."
    # Multi-session streak
    if consecutive >= 2:
        f"{consecutive} consecutive sessions below EPH target..."
```

This makes ARIA's agents visibly aware of each other's outputs — a rider's intervention message cites both the Zone agent's recommendation and the Dead Run agent's risk flag. The reads use `most recent records, not current cycle` — the current cycle hasn't finished executing all agents when the Earnings agent runs.

**Dual cooldown design.**

| Alert Type | Cooldown | Reason |
|---|---|---|
| `earnings_below_threshold` | 30 minutes | Short-cycle signal, refresh each cycle after cooldown |
| `churn_risk` | 120 minutes (2 hours) | Multi-session signal — cycling it every 15 min is spam |

**Churn surge detection.** If `churn_risk_count / active_riders ≥ 0.15` (15% of fleet is churn risk), a single operator alert fires for the fleet-wide signal. This is the Supervisor's headline indicator for platform retention pressure.

**Recovery detection.** The lag snapshot query provides the previous cycle's `health_classification`. Transition from `at_risk/critical → watch/healthy` triggers a low-severity `earnings_recovery` alert. This demonstrates closed-loop impact — ARIA's interventions appear to correlate with recoveries, visible in the operator panel.

**Quality KPIs.** The Earnings agent exposes `ml_failures`, `stale_lag_count`, `alerts_suppressed_by_cooldown`, and `new_vs_repeat_at_risk` as first-class output fields, not buried in log strings. The Supervisor's observability reporting picks these up directly.

---

## 6.12 Agent 05 — Supervisor

**Purpose.** Cross-agent synthesis: consume all four sub-agent results, detect multi-agent patterns that no single agent can see, produce a single `cycle_briefing` with severity classification, financial KPIs, and operator-facing recommended actions. Phase 2 adds episodic memory RAG — past outcomes ground the LLM's recommendations in what actually worked.

### Pipeline (6 nodes, strictly linear)

```
ground_past_outcomes → validate_inputs → analyze_patterns
    → retrieve_context → call_llm → write_and_publish
```

**Node 0 — ground_past_outcomes (Phase 2).**

Grounds `outcome_1cycle` and `outcome_3cycle` for past ungrounded episodes in `supervisor_episode_memory`. Uses `cycle_briefings ORDER BY timestamp` to find the next +1 and +3 cycles after each episode — not time arithmetic. Time arithmetic fails under manual `/cycle/run` triggers and scheduler restarts; timestamp ordering is always correct.

`effectiveness_score = patterns_resolved / max(actionable_patterns, 1)`

`observability_degraded` is excluded from the `actionable_patterns` denominator — it is a system health observation, not an operational pattern that recommendations can resolve.

Runs at cycle start so grounded outcomes are available for this cycle's retrieval. `LIMIT 20` per cycle prevents this node from dominating cycle time.

**Node 1 — validate_inputs.**

Each sub-result classified as `ok / partial / failed / missing`. Classification logic:

```
ok      : result is a dict, status='success', all 4 required keys present
partial : result is a dict, status='partial' OR missing required keys
failed  : result is a dict, status='failed'
missing : result is None or empty
```

`observability_degraded` is set if `(failed + partial) >= 2`. Safe defaults replace missing/failed results so downstream nodes never `KeyError`.

**Node 2 — analyze_patterns.**

Deterministic pattern detection with ratio+floor triggers. Both conditions must be satisfied:

```python
_CHURN_SURGE_PCT = 0.25    _CHURN_SURGE_ABS = 3
_DEAD_ZONE_PCT   = 0.30    _DEAD_ZONE_ABS   = 2
_RESTAURANT_PCT  = 0.40    _RESTAURANT_ABS  = 3
```

**Why ratio AND floor?** A pure ratio trigger fires during low-volume periods when absolute numbers are trivial (e.g., 2 churn-risk riders out of 4 active = 50% ratio, but 2 riders is not a surge). A pure floor trigger fires during high-volume periods on noise. Both are needed.

**Named critical override: `system_zone_pressure`.** If `dead_zone_count / total_zones >= 0.50`, the severity is immediately set to `"critical"` regardless of other pattern scores. 50% dead zones is a platform-level collapse — no further reasoning needed.

**Compound critical: `churn_surge AND dead_zone_pressure`.** If both patterns fire simultaneously, the severity escalates to `"critical"` even if neither alone would reach critical. Two simultaneous signals are more alarming than either in isolation.

**KPI proxy mappings:**

| Supervisor reads | From agent | Interpretation |
|---|---|---|
| `restaurant.operator_alerts` | RestaurantAgent | ≈ high-risk restaurant count (medium+critical severity) |
| `earnings.snapshots_written` | EarningsAgent | ≈ total active riders this cycle |
| `restaurant.above_threshold_count` | RestaurantAgent | exact high-risk restaurant count (≥ 0.65 threshold) |
| `zone.dead_zone_count` | ZoneAgent | direct |
| `dead_run.total_earnings_at_risk_rs` | DeadRunAgent | direct financial KPI |

**Node 4 — retrieve_context (Phase 2 RAG).**

Eight steps:

1. Build `embed_input` — a canonical deterministic string from severity + pattern types + KPIs. **Never embed the LLM's situation summary** — LLM output is non-deterministic and changes across temperatures/models. The canonical string produces stable, reproducible embeddings for retrieval.

2. Call Ollama `qwen3-embedding` via the embedding client for a 4096-dimensional vector (asymmetric retrieval: canonical string is document-side, no instruction prefix). Log latency_ms.

3. SQL pre-filter: `recency(30d) + severity_adjacent + city overlap + embedding_status='ok' + outcome_1cycle IS NOT NULL` → `ORDER BY vector similarity, LIMIT 10`.

4. Python filter: `>= 1` shared pattern type with current cycle.

5. Python filter: `similarity >= 0.65` (RAG_SIMILARITY_THRESHOLD).

6. Minimum support gate: if `< 2` episodes pass all filters, skip RAG entirely. Prevents single-anecdote anchoring.

7. Rank top 3 (RAG_TOP_K) by `(similarity DESC, effectiveness_score DESC)`.

8. Build compressed 5-line snippets, cap total at 1200 characters.

**Severity adjacency mapping:**

| Current severity | Retrieves episodes of |
|---|---|
| `critical` | critical, warning |
| `warning` | critical, warning, normal |
| `normal` | normal, warning |

Critical cycles retrieve warning episodes too — a warning that escalated to critical has pattern overlap and actionable outcomes. Normal cycles do not retrieve critical episodes — a critical pattern context would distort recommendations in a calm cycle.

**Node 5 — call_llm.**

If `rag_context_used=True`, the RAG snippets are prepended to the prompt:

```
### Past similar situations and their outcomes:
[Episode snippets capped at 1200 chars total]

### Current situation:
[Financial KPIs + pattern summary + agent summaries]
```

`max_tokens=450` (bumped from 350 in Phase 1 to accommodate RAG context).

LLM output constraints applied post-generation:
- `situation_summary` truncated at 2000 chars
- `recommended_actions` truncated at 300 chars each, max 5 actions
- `reasoning` truncated at 500 chars

**4-step LLM JSON fallback chain:** (1) direct `json.loads`; (2) extract JSON block from markdown code fences; (3) regex-based field extraction; (4) use the raw text as `situation_summary` with empty actions list.

**Node 6 — write_and_publish.**

Idempotent `ON CONFLICT` write to `cycle_briefings`. Redis `PUBLISH` to `CHANNEL_CYCLE_COMPLETE`. Phase 2: also writes a new row to `supervisor_episode_memory` with `outcome_1cycle = NULL` (to be grounded by the next cycle's Node 0).

The `cities_active` field in the briefing comes from the Zone agent's output. DB fallback: `SELECT DISTINCT city FROM zones WHERE city IS NOT NULL LIMIT 20` if the Zone agent is unavailable.

**`observability_degraded` is excluded from `effectiveness_score` denominator.** System health observations (like "two agents failed") cannot be "resolved" by the Supervisor's recommended actions. Including them would artificially deflate effectiveness scores, making well-handled degradation look like intervention failure.

## 6.13 Zone Pressure and Platform-Wide Crisis Detection

`system_zone_pressure` is the platform-level collapse signal. It fires from the **Zone agent** (node 6.10) when `dead_zone_count / total_zones >= 0.50` — half of all active zones are dead. This alert is written by the Zone agent directly, not the Supervisor, ensuring it fires even if the Supervisor fails on this cycle. The threshold of 0.50 is intentionally high: if half of all zones are dead simultaneously, per-zone analysis is meaningless and the platform needs immediate human escalation.

The **Dead Run agent** (node 6.9) has a parallel crisis signal: if `flagged_orders / total_scored >= 0.50`, a `system_dead_zone_pressure` operator alert fires regardless of zone-level evidence gates. This catches fleet-wide dead run pressure even when individual zone snapshots are stale.

The **Supervisor** (node 6.12, `analyze_patterns` node) elevates severity to `"critical"` in two cases: (1) **Named critical override** — `system_zone_pressure` pattern alone triggers `critical` severity immediately, bypassing all other scoring. (2) **Compound critical** — `churn_surge AND dead_zone_pressure` together trigger `critical` even if neither pattern alone would. Two simultaneous distress signals are treated as more alarming than either in isolation. Normal severity scoring uses ratio+floor pattern triggers (e.g., churn_risk_count / active_riders >= 0.15 AND count >= 3) which the LLM interprets and synthesizes into actionable recommendations.

---

# 7. Supervisor Episodic Memory (Phase 2 RAG)

> **Structure reference:** This section follows `/DOCS_STRUCTURE.md` §"Supervisor Episodic Memory" before writing.

Phase 2 adds retrieval-augmented generation (RAG) to the Supervisor. Rather than generating recommendations in a vacuum each cycle, the Supervisor retrieves past situation episodes with similar patterns and grounds its recommendations in what actions actually worked before.

---

## 7.1 Why pgvector, Not a Separate Vector DB

ARIA already runs PostgreSQL with TimescaleDB and PostGIS. Adding a dedicated vector database (Pinecone, Chroma, Weaviate) would mean:
- A new service in docker-compose with its own health check, memory allocation, and failure mode.
- Split between the relational query (pre-filters by severity, recency, city) and the vector query — requiring a two-phase query or an API that supports hybrid filtering (not all services do).
- Operational complexity for a volume of data that peaks at one episode per 15-minute cycle (~35K episodes/year at most).

pgvector adds vector similarity search as a PostgreSQL extension — the same database that already holds all agent data. The HNSW index (`USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)`) delivers approximate nearest-neighbour performance at this volume. The SQL pre-filter (recency + severity adjacency + city overlap + outcome_grounded) runs as a standard WHERE clause before the vector scan, dramatically reducing the ANN candidate set. This hybrid approach is impossible with most standalone vector DBs without custom integration.

---

## 7.2 The `supervisor_episode_memory` Table

```sql
CREATE TABLE supervisor_episode_memory (
    id                UUID PRIMARY KEY,
    cycle_id          UUID UNIQUE,              -- soft FK to cycle_briefings
    situation_summary TEXT NOT NULL,            -- LLM narrative (display only, never embedded)
    embed_input       TEXT NOT NULL,            -- canonical deterministic string (what IS embedded)
    patterns_detected JSONB NOT NULL,           -- full structured pattern list
    pattern_types     TEXT[] NOT NULL,          -- top-level array for GIN overlap filter
    actions_taken     TEXT[] NOT NULL,          -- recommended_actions from LLM
    severity          VARCHAR(20) NOT NULL,
    city              TEXT[] NOT NULL,          -- active cities (GIN overlap filter)
    outcome_1cycle    JSONB,                    -- null until grounded after +1 cycle
    outcome_3cycle    JSONB,                    -- null until grounded after +3 cycles
    embedding         vector(4096) NOT NULL,    -- qwen3-embedding output (4096-dim)
    embedding_status  VARCHAR(10) NOT NULL      -- 'ok' | 'failed'
);
```

Three indexes support the retrieval pipeline:
- **B-tree** on `created_at DESC`, `severity`, `embedding_status` for SQL pre-filter.
- **GIN** on `pattern_types` and `city` for fast array overlap operator (`&&`).
- **HNSW** on `embedding vector_cosine_ops` for approximate nearest-neighbour.

---

## 7.3 Outcome Grounding: What Gets Measured

`effectiveness_score = patterns_resolved / max(actionable_patterns, 1)`

Each episode stores `outcome_1cycle` (what changed after 1 cycle) and `outcome_3cycle` (after 3 cycles). These are not soft targets — they are computed from actual KPI deltas between the episode's `cycle_briefings` row and the next 1 or 3 briefings:

```
at_risk_delta_abs   = at_risk_after − at_risk_before
avg_eph_delta_abs   = avg_eph_after − avg_eph_before
dead_zone_delta_abs = dead_zone_count_after − before
rest_delta_abs      = high_risk_restaurant_count_after − before
```

Per-pattern resolution is checked deterministically:
- `churn_surge` resolved if `at_risk_after < at_risk_before`
- `dead_zone_pressure` resolved if `dead_zone_count_after < dead_zone_count_before`
- `restaurant_cascade` resolved if `high_risk_restaurant_count_after < before`
- `observability_degraded` excluded from denominator (cannot be "resolved" by ops actions)

`effective = effectiveness_score > 0 OR avg_eph_delta > 0`

The `effectiveness_score` drives retrieval ranking — episodes where recommended actions correlated with improvement rank above similar-similarity episodes that did not improve outcomes.

**LIMIT 20 per grounding cycle.** The grounding node runs at the start of every cycle. Without a LIMIT, a large backlog of ungrounded episodes (after a multi-hour outage) could dominate cycle time. LIMIT 20 bounds the grounding cost to constant time.

---

## 7.4 The Canonical embed_input: Why Not the LLM Summary

The embedding is computed on `embed_input` — a deterministic string assembled from severity + pattern types + key financial KPIs:

```
severity: critical | patterns: churn_surge, dead_zone_pressure |
kpis: avg_eph=82.3, at_risk=47, dead_zones=12, shortfall=4200
```

This is **never the LLM's `situation_summary`**. LLM output is non-deterministic: the same situation produces different summaries across temperature settings, model versions, and prompt changes. Embedding LLM output would make the retrieval results shift with every model update — two identically-structured situations in different weeks would have low similarity just because the temperature varied.

The canonical string is stable and reproducible. If the same pattern structure and KPI magnitudes occur in a different week, the embed_input produces the same embedding (modulo floating-point noise), and the retrieval correctly finds both as similar.

---

## 7.5 Retrieval Pipeline: All Eight Steps

```
1. Build embed_input (canonical, deterministic)
2. Call Ollama `qwen3-embedding` → 4096-dim vector
3. SQL: recency(30d) + severity_adjacent + city&&current + embedding_status='ok'
        + outcome_1cycle IS NOT NULL → ORDER BY cosine_similarity LIMIT 10
4. Python: pattern_types overlap >= 1 shared type
5. Python: similarity >= 0.65 (RAG_SIMILARITY_THRESHOLD)
6. Minimum support gate: if < 2 pass → skip RAG entirely
7. Rank top 3 by (similarity DESC, effectiveness_score DESC)
8. Build compressed snippets, cap at 1200 chars total
```

**Step 3 — SQL hybrid filter:**

The `outcome_1cycle IS NOT NULL` filter is critical: ungrounded episodes have no `effectiveness_score` and no proven outcome. Including them in retrieval would mean recommending actions whose consequences are unknown.

**Step 4 — Pattern overlap filter:**

A high cosine similarity between two embed_inputs means their KPI magnitudes and severity align. But KPI magnitudes alone don't guarantee pattern relevance — two cycles can have similar EPH and dead zone counts but different root causes (one has `churn_surge`, the other has `restaurant_cascade`). The pattern overlap filter requires at least 1 shared pattern type, ensuring retrieved episodes have structurally similar situations.

**Step 6 — Minimum support gate:**

If fewer than 2 episodes pass all filters, RAG is skipped entirely. A single matching episode is an anecdote; 2 or more establish a pattern. Single-episode RAG would bias recommendations toward one historical event rather than a generalizable strategy. The Supervisor runs in non-RAG mode (faster, same quality) when support is insufficient.

**Step 7 — Effectiveness ranking:**

Within top-K, episodes are ranked by `(similarity DESC, effectiveness_score DESC)`. Two episodes with identical cosine similarity: the one where the recommended actions actually resolved the patterns ranks first.

**Step 8 — Snippet format:**

Each snippet is 5 lines maximum:
```
Cycle [date]: severity=[X], patterns=[list]
EPH: [before] → [after] (+/- delta)
Actions: [list of recommended_actions from that cycle]
Outcome (1 cycle): effectiveness=[score], [pattern] resolved/persisted
```

Total across all snippets capped at 1200 characters. The cap prevents RAG context from consuming the LLM's effective context window — the Supervisor prompt already includes financial KPIs, agent summaries, and the current pattern list.

---

## 7.6 Embedding Client: Technical Details

`embedding_client.py` wraps Ollama's `/api/embeddings` endpoint:

- **Model:** `qwen3-embedding` (4096 dimensions, GPU inference via Ollama, MTEB #1 as of 2025, Apache 2.0). Supports asymmetric retrieval: query-side gets an instruction prefix, document-side is embedded raw — improves informal query → formal documentation matching.
- **Shared httpx client:** `max_connections=4, max_keepalive_connections=2`. Low cap because the embedding client is called once per cycle, not concurrently.
- **Timeout:** 60 seconds — headroom for the larger qwen3-embedding model.
- **Failure path:** returns `(None, latency_ms)`. The Supervisor writes `embedding_status='failed'` and stores a zero vector. The zero vector is excluded from retrieval by the `WHERE embedding_status='ok'` filter. The Supervisor continues in non-RAG mode — embedding failure never blocks the cycle.
- **Dimension validation:** `len(embedding) != 4096` → logs a warning and returns None. Guards against model configuration drift.

**asyncpg + pgvector integration.** asyncpg has no native awareness of the `vector` type. Vectors must be passed as TEXT and cast in SQL:

```python
def vec_to_pgvector_str(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"

# In SQL:
INSERT INTO supervisor_episode_memory (..., embedding)
VALUES (..., $1::vector)
```

The 8-decimal precision ensures no float truncation in the pgvector representation.


## 7.7 Documentation Chatbot — PageIndex RAG vs Vector RAG

ARIA includes a documentation chatbot (`/docs-chat/chat`) with two retrieval modes. **Vector RAG**: the query is embedded using `qwen3-embedding` with an asymmetric instruction prefix (`"Instruct: Given a conversational user question, retrieve the most relevant technical documentation passage that answers it.\nQuery: {text}"`), then pgvector cosine similarity retrieves the top-5 most similar chunks from `docs_chunks` (populated by the `/docs-chat/ingest` endpoint). **PageIndex** (structure-aware RAG): ARIA_DOCS.md is parsed into a node tree (`/docs-chat/build-index`) with three levels — L0 chapters (`#`), L1 sections (`##`), L2 subsections (`###`). At query time, no embedding is done. Instead the LLM navigates the tree in three stages: (1) shown ~12 chapter titles, picks 1-2 most relevant; (2) shown all section titles within those chapters, picks 1-3 sections; (3) all chosen sections plus their `###` children are assembled as context for the final LLM generation call. Total: 2 small LLM navigation calls + 1 generation call per PageIndex query. PageIndex is embedding-free at query time and stays within token budget by breaking the 92-section tree into small focused sub-lists. Vector RAG is better for informal or conversational queries where `qwen3-embedding`'s asymmetric retrieval maps query vocabulary to documentation vocabulary.

---

# 8. Database Schema

> **Structure reference:** This section follows `/DOCS_STRUCTURE.md` §"Database Schema" before writing.

ARIA's PostgreSQL database runs four extensions and 18 tables organized into 7 sections. The schema is designed around two principles: agent outputs are first-class tables (not appended to general logs), and every recurring query pattern has an index.

---

## 8.1 Extension Stack

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;   -- time-series partitioning + continuous aggregates
CREATE EXTENSION IF NOT EXISTS postgis;        -- GeoJSON zone boundary queries
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";    -- uuid_generate_v4() for primary keys
CREATE EXTENSION IF NOT EXISTS vector;         -- pgvector: Supervisor episodic memory
```

Each extension solves a specific problem:
- **TimescaleDB:** the 28-day zone density baseline and 14-day restaurant delay baseline are computed over millions of snapshot rows. Without continuous aggregates, these `AVG/STDDEV` queries would scan full tables every 15 minutes.
- **PostGIS:** zone `boundary_geojson` is stored as JSONB; PostGIS is available for future geo proximity queries. Current seeding uses centroids and haversine distance in Python.
- **uuid-ossp:** PostgreSQL 16 includes `gen_random_uuid()` natively, but `uuid_generate_v4()` from uuid-ossp is used for backward compatibility with the schema design.
- **pgvector:** vector(4096) columns for Supervisor episodic memory and docs chunk embeddings. Without this extension, the column type does not exist. HNSW index not used (hard limit of 2000 dims for HNSW); at ~200 rows exact scan is instant (<1ms).

---

## 8.2 Hypertables

Three tables converted to TimescaleDB hypertables:

| Table | Partition key | Why |
|---|---|---|
| `zone_density_snapshots` | `timestamp` | ~180 rows/cycle = ~500K rows/month; queries always have time bounds |
| `restaurant_delay_events` | `timestamp` | ~100 events/cycle; baseline queries filter by timestamp window |
| `rider_location_updates` | `timestamp` | ~300 rows/cycle; always queried as "latest per rider" |
| `observability_logs` | `timestamp` | Append-only audit log; queries always have time bounds |

Hypertables partition data into chunks by time, making `WHERE timestamp > NOW() - '28 days'` queries hit only recent chunks rather than full table scans.

---

## 8.3 Continuous Aggregates

```sql
CREATE MATERIALIZED VIEW zone_density_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', timestamp) AS bucket, zone_id,
       AVG(density_score), MAX(density_score), AVG(order_count)
FROM zone_density_snapshots GROUP BY bucket, zone_id;

CREATE MATERIALIZED VIEW restaurant_delay_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', timestamp) AS bucket, restaurant_id,
       hour_of_day, day_of_week,
       AVG(delay_mins), STDDEV(delay_mins), COUNT(*)
FROM restaurant_delay_events GROUP BY bucket, restaurant_id, hour_of_day, day_of_week;
```

These materialized views are automatically kept up-to-date by TimescaleDB as new rows arrive. The zone stress baseline (`stress_ratio = current_density / 28-day historical baseline at same hour`) queries `zone_density_hourly` rather than scanning `zone_density_snapshots` directly. At 28 days of data, `zone_density_snapshots` has ~7M rows; `zone_density_hourly` has ~120K rows (one per zone per hour). The baseline query becomes sub-millisecond.

Similarly, `restaurant_delay_hourly` powers the two-tier baseline in the restaurant algorithm: the 28-day aggregate is the primary path; a 14-day raw scan is the fallback for restaurants with no aggregate data yet.

---

## 8.4 UNIQUE(rider_id, session_date) Constraint

```sql
CREATE TABLE rider_sessions (
    ...
    UNIQUE (rider_id, session_date)
);
```

This constraint enforces the business rule: one active session per rider per day. It caused the `warm_start.py` bug where `riders_activated = 0`: the warm start INSERT was blocked because closed sessions from a previous run already existed for today's date. The fix is to DELETE closed-only sessions first, then INSERT. The constraint itself is correct — it prevents duplicate session creation.

---

## 8.5 Order State Machine

The `status` column in `orders` implements a state machine:

```
pending → assigned → rider_inbound → picked_up → en_route_delivery → delivered
                                                                    → failed
```

The DDL comment documents this flow. The event-stream's dispatcher advances orders through states. Agents filter by status to determine which orders are alert-eligible:

- Dead Run agent: `('assigned', 'rider_inbound', 'picked_up')` — picked_up is analytics only.
- Restaurant agent: `('assigned', 'rider_inbound')` — only these riders are currently at the restaurant.

`failed_at` and `failure_reason` support the simulation's dead run tracking — an order that lands in a dead zone and cannot be completed within time bounds gets `status='failed'` with reason `'dead_zone'`.

---

## 8.6 Notable Design Choices in Key Tables

**`order_delta` in `zone_density_snapshots`.**
```sql
order_delta INTEGER NOT NULL DEFAULT 0  -- order_count change vs previous snapshot (surge signal)
```
The Zone Intelligence Agent uses `order_delta > 0` as the operator alert surge gate. This column is maintained by `zone_engine.py` at snapshot time (current `order_count` minus previous snapshot's `order_count`). It avoids a self-join to compute delta at query time.

**`health_score` in `rider_sessions`.**
The 3-component health score (0–100) is cached on the session row. It's updated by the Earnings Guardian each cycle. The session row is a "summary view" — the frontend can read current health without joining to `rider_health_snapshots`.

**`last_risk_score` on `restaurants`.**
Updated by the Restaurant agent each cycle. Gives the frontend a single-row read for the current risk state without querying `restaurant_risk_scores` (which has one row per restaurant per cycle).

**`is_blacklisted` on `restaurants`.**
A legacy field from the Kaggle dataset seeding. The Kaggle dataset included a blacklisted merchant flag. ARIA seeds this flag but does not act on it — the ML models and z-score algorithm don't use it. It remains in the schema as a reminder of the original data source.

**`embed_input` vs `situation_summary` in `supervisor_episode_memory`.**
Two text fields exist intentionally: `situation_summary` stores the LLM's natural-language summary (for display in the frontend's history panel), while `embed_input` stores the canonical deterministic string (what is actually embedded). The embedding is over `embed_input`, not `situation_summary`.

---

## 8.7 Index Strategy

Every common query pattern has an index. Selected examples:

```sql
-- Zone density: current (LIMIT 1) and historical (28-day window)
CREATE INDEX idx_density_zone_time ON zone_density_snapshots(zone_id, timestamp DESC);

-- Restaurant delay: ML feature queries filter by restaurant + hour + day
CREATE INDEX idx_delay_restaurant_time ON restaurant_delay_events(restaurant_id, hour_of_day, day_of_week);

-- Orders: most queried table — composite for agent's zone+status filter
CREATE INDEX idx_orders_status_delivery_zone ON orders(status, delivery_zone_id);

-- Rider alerts: partial index for unresolved alerts (frontend's primary read)
CREATE INDEX idx_alerts_unresolved ON rider_alerts(is_resolved, created_at DESC) WHERE is_resolved = FALSE;

-- Supervisor episodic memory: three-layer RAG indexes
CREATE INDEX idx_episode_pattern_types ON supervisor_episode_memory USING gin(pattern_types);
CREATE INDEX idx_episode_city ON supervisor_episode_memory USING gin(city);
CREATE INDEX idx_episode_embedding ON supervisor_episode_memory
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
```

The partial index on `rider_alerts(is_resolved=FALSE)` is particularly impactful — the frontend unresolved alerts panel queries millions of rows but only cares about the small unresolved subset. The index covers only those rows.


---

# 9. Infrastructure

> **Structure reference:** This section follows `/DOCS_STRUCTURE.md` §"Infrastructure" before writing.

---

## 9.1 Why Docker Compose vs Kubernetes

ARIA runs 8 services on a single dual-GPU workstation (dual RTX 3090, 128GB RAM). Kubernetes is designed for multi-node clusters where pod scheduling, service discovery, and rolling deploys across machines justify its operational overhead. For a single-machine deployment with a fixed set of services, Docker Compose provides:

- Simple `docker-compose up` bring-up with dependency ordering via `depends_on`.
- Shared Docker network for service-to-service communication by name (`http://mcp-server:8001`).
- `deploy.resources.reservations.devices` for GPU assignment to vLLM (both GPUs) and Ollama (CPU/VRAM as needed).
- No control plane, etcd, or kubelet overhead consuming RAM that would otherwise go to the 32B LLM.

For this portfolio context (single workstation, fixed services, no horizontal scaling requirement) Docker Compose is the correct tool.

---

## 9.2 Why TimescaleDB

The specific feature that chose TimescaleDB over plain PostgreSQL 16 is **continuous aggregates**: automatically maintained materialized views that roll up raw time-series data into hourly buckets. Both the zone stress baseline and the restaurant delay baseline rely on 28-day / 14-day `AVG + STDDEV` queries across millions of snapshot rows. Without continuous aggregates, these queries would run as full table scans every 15 minutes — unacceptable at cycle cadence.

TimescaleDB's continuous aggregates are not a manual process (no cron + `REFRESH MATERIALIZED VIEW`). They update incrementally as new rows arrive, keeping the `zone_density_hourly` and `restaurant_delay_hourly` views fresh for the next cycle's baseline queries.

---

## 9.3 Why Redis: Dual Role

Redis serves two distinct functions in ARIA:

**Zone density cache (TTL = 900 seconds).** The event-stream writes zone density snapshots to `aria:zone_density:{zone_id}` after each zone snapshot computation. The Dead Run agent reads density for all unique delivery zones via a Redis pipeline in one round-trip. Without this cache, each agent cycle would need ~180 DB queries for the latest zone density (one per zone). The TTL of 900 seconds matches the 15-minute cycle interval — stale data expires roughly when the next snapshot would arrive.

**Redis pub/sub for live event forwarding.** The event-stream publishes to three channels on every significant event:
- `aria:zone_updates` — zone snapshot written
- `aria:session_updates` — rider session opened/closed
- `aria:order_updates` — order status changed

The MCP server's WebSocket manager subscribes to all three channels via a background `asyncio.Task` (the Redis bridge). Every published message is forwarded to all connected WebSocket clients in real time, powering the frontend's live feed without polling.

Session close: the event-stream calls `DEL rider:session:{id}` + publishes to `session_updates` when a rider goes offline. The Redis key deletion invalidates the ride's live state; the pub/sub event notifies the frontend.

---

## 9.4 asyncpg Pool: min=4, max=16

```python
pool = await asyncpg.create_pool(DATABASE_URL, min_size=4, max_size=16)
```

`min_size=4`: four warm connections always maintained. At cycle start, the scheduler acquires one connection immediately without waiting for a new connection handshake. The ML server and frontend API routes each need a connection concurrently with the cycle — 4 warm connections cover baseline concurrency without per-request latency.

`max_size=16`: limits the pool under burst conditions. Each PostgreSQL connection consumes ~5–10MB server-side. 16 connections = ~160MB maximum pool overhead. Given 128GB RAM, this is conservative — the constraint is PostgreSQL's `max_connections` setting (default 100), not memory.

---

## 9.5 Why vLLM

vLLM is chosen for LLM serving over Ollama for the primary inference workload (Qwen2.5-32B):

- **Tensor parallelism:** vLLM spans the 32B model across both RTX 3090s (24GB + 24GB = 48GB VRAM), essential for fitting the 4-bit GPTQ quantized model.
- **OpenAI-compatible API:** LangChain's `ChatOpenAI` class connects to vLLM with `base_url` and `api_key` params — no LangChain-vLLM-specific integration needed.
- **Continuous batching:** vLLM handles concurrent LLM requests efficiently. At 4–6 calls per cycle with occasional manual `/cycle/run` triggers, concurrent batching is not critical but future-proofs for multi-user scenarios.
- **`api_key="EMPTY"`:** vLLM requires a non-empty API key string for the OpenAI SDK compatibility layer. The value is meaningless for auth purposes (vLLM has no token auth by default), but an empty string causes the OpenAI client to raise a validation error.

Ollama serves the embedding model (`qwen3-embedding`) only — it runs on GPU but is small (~5GB VRAM on one RTX 3090) and does not compete with vLLM. Used for both Supervisor episodic memory RAG and the documentation chatbot vector search.

### Why Qwen2.5-32B over GPT-4, Mistral, or Llama 3

- **Data locality and privacy:** ARIA processes rider PII — earnings, session data, GPS coordinates. GPT-4 is a cloud API; data would leave the local network on every agent call. Qwen2.5-32B runs fully on-premise on the two RTX 3090s. No data ever leaves the host machine.
- **No API cost at demo scale:** Each agent cycle makes 4–6 LLM calls. At GPT-4 pricing, running demos at TIME_SCALE=100 (a full simulated day in ~15 real minutes) would generate hundreds of cycles per hour — cost becomes prohibitive for a portfolio project.
- **Hardware fit:** Qwen2.5-32B-Instruct-GPTQ-Int4 weighs ~18GB — fits across two RTX 3090s (48GB VRAM total) with `tensor_parallel_size=2`. Llama-3-70B-GPTQ would exceed 48GB. Mistral-7B would leave 30GB VRAM idle. 32B-GPTQ-Int4 is the sweet spot for this hardware.
- **Instruction following at structured tasks:** Qwen2.5 series shows strong performance on structured JSON generation benchmarks — important for ARIA's agent outputs (severity classifications, JSON briefings, pattern detection). The 32B scale gives noticeably better adherence to output format constraints than 7B alternatives.
- **Qwen2.5 vs Qwen2:** Qwen2.5 adds improved code and structured-output generation over Qwen2, validated in the ARIA context by consistent JSON output from all 5 agents without post-processing hacks.

---

## 9.6 Why Cloudflare Tunnel

Cloudflare Tunnel (`cloudflared`) exposes the MCP server to the internet without opening firewall ports:

- **No open ports:** the workstation runs UFW with only SSH open. No port 8001 exposed to the public internet. Cloudflare Tunnel creates an outbound-only connection from the workstation to Cloudflare's edge.
- **WebSocket support:** Cloudflare Tunnel proxies WebSocket connections natively. The frontend's WebSocket (`wss://`) connection to the MCP server works through the tunnel without configuration.
- **Free tier sufficient:** at portfolio scale (one user, one demo session at a time), Cloudflare's free tunnel plan handles the traffic.
- **TLS at the edge:** Cloudflare terminates TLS, giving `https://` and `wss://` endpoints for free without managing certificates on the workstation.

---

## 9.7 Port Security: UFW Configuration

The workstation runs UFW (Uncomplicated Firewall) with a minimal ruleset:

```
ALLOW  22/tcp   (SSH — management access)
DENY   all      (default policy: deny all inbound)
```

**No public inbound port for the MCP server.** Port 8001 (MCP server) is not opened in UFW. The Cloudflare Tunnel process reaches it from inside the Docker network — Cloudflare's edge makes an outbound connection to Cloudflare's infrastructure, not an inbound connection to port 8001. From the internet's perspective, port 8001 does not exist.

**ML server and event-stream ports are Docker-internal only.** Ports 8002 (ML server) and 8003 (event-stream) have no host port mapping in `docker-compose.yml`. They are accessible only from within the Docker bridge network. No UFW rule is needed or exists for them — the Docker daemon handles network isolation. An external caller cannot reach these services regardless of firewall state.

**Why this matters.** The ML server holds model weights and inference endpoints. The event-stream can modify simulation state. Neither should be reachable externally. The design ensures: external access = Cloudflare Tunnel → port 8001 only; internal access = Docker network; management = SSH on port 22.

---

## 9.8 Why Vercel for Frontend

Next.js 14 frontend deployed to Vercel because:

- **Zero-config deployment:** Vercel detects Next.js and applies optimal build settings automatically.
- **Edge functions for API routes:** Next.js `/api/*` routes run at Vercel's edge, proxying to the Cloudflare Tunnel-exposed MCP server.
- **`basePath` support:** Vercel serves ARIA under `/aria` (basePath). Vercel's configuration for Next.js basePath is one line in `next.config.js`.

**The basePath/fetch bug.** Next.js `basePath` automatically prefixes all `<Link>` and `next/navigation` router calls, but does NOT prefix `fetch()` calls. Any client-side `fetch('/api/...')` call omits the `/aria` prefix and hits Vercel's root domain (404). All client-side API calls must be written as `/aria/api/*` explicitly. This was a production bug that caused the frontend to show "disconnected" even with the backend running.

---

## 9.9 APScheduler Cycle Orchestration

**`max_instances=1`:** APScheduler fires a job on a timer. If a cycle is still running when the next interval fires (possible at high TIME_SCALE where the interval may be 30 real seconds), `max_instances=1` skips the new firing rather than running a second cycle concurrently. Concurrent cycles would create DB contention (both acquiring the same `asyncpg.Pool` connection) and duplicate alerts for the same time window.

**`reschedule_cycle()` formula:**
```
real_secs = max(MIN_REAL_INTERVAL_SECS=30, CYCLE_INTERVAL_MINS × 60 / TIME_SCALE)
```

At `TIME_SCALE=300` (5× real time), one simulated 15-minute cycle passes every 3 real minutes:
`real_secs = max(30, 15×60/300) = max(30, 3) = 30`

The 30-second floor prevents cycles from firing faster than the system can complete them.

**Why `urllib` not `httpx` for `_fetch_sim_now()`:** APScheduler fires the `run_cycle` coroutine but does so from an async context. The `_fetch_sim_now()` function needs to make an HTTP call to the event-stream to get the current sim time. Using `asyncio.to_thread(_fetch_sim_now)` calls it synchronously via `urllib.request.urlopen` in a thread pool — safe for the blocking call. Using `httpx.AsyncClient` directly would require an await inside what APScheduler sometimes calls from a thread context, causing `RuntimeError: no running event loop` in some scheduler configurations. `urllib` eliminates this class of error entirely.

**Shared DB connection per cycle:** all 5 agents share one `asyncpg.Connection` acquired from the pool via `async with pool.acquire() as conn:`. This is the root reason agents must be sequential — asyncpg connections are single-query-at-a-time.

---

## 9.10 WebSocket and Real-Time Architecture

**Two-source push model:**

```
Source 1: APScheduler → run_cycle() → ws_manager.broadcast(cycle_complete)
Source 2: event-stream → Redis pub/sub → Redis bridge → ws_manager.broadcast(live event)
```

The frontend receives two types of real-time data:
1. **Cycle events:** `cycle_start` (triggers animated pipeline diagram) and `cycle_complete` (triggers KPI refresh and briefing update).
2. **Live events:** zone density updates, session opens/closes, order status changes — forwarded from the event-stream via the Redis bridge.

**`cycle_start` / `cycle_complete` events** drive the frontend's animated pipeline diagram. When the frontend receives `cycle_start`, it begins animating the Zone → Restaurant → Dead Run → Earnings → Supervisor pipeline nodes. When `cycle_complete` arrives, all nodes show green and the briefing refreshes.

**Dead connection cleanup (lazy):** `WSManager.broadcast()` catches exceptions from dead WebSocket connections and removes them from `_connections` on the next broadcast. There is no active heartbeat that proactively discovers dead connections. The server-side 15-second ping (`await ws.send_text('{"type":"ping"}')`) in `main.py` serves this purpose — if the client is dead, the ping raises an exception and the connection is cleaned up on the next `broadcast()` call.

**Bridge lifecycle:** `start_redis_bridge()` runs as an `asyncio.Task`. On lifespan shutdown (`asyncio.CancelledError`), the task calls `await pubsub.unsubscribe()` before exiting — clean Redis cleanup.

---

## 9.11 Auth Design

**X-API-Key (MCP tools + Claude Desktop).**
All MCP tool routes in `router.py` require `X-API-Key: {MCP_API_KEY}` in the request header. The frontend's Next.js API proxy routes inject this key server-side — the key is never exposed to the browser. Claude Desktop connects to `/mcp` with the same key.

**X-Internal-Key (ML server).**
The ML server (`fastapi-ml:8002`) is reachable only within the Docker network — no host port mapping. It requires `X-Internal-Key` on all inference routes. The MCP server sets this header from the `ML_INTERNAL_KEY` environment variable. No external caller can reach the ML server directly.

---

## 9.12 MCP Tool Design

**MCP (Model Context Protocol)** is an open protocol developed by Anthropic that lets AI assistants like Claude Desktop call tools exposed by external services. ARIA implements an MCP server using `fastapi-mcp`, which auto-generates MCP-compatible tool definitions directly from FastAPI route docstrings. Claude Desktop connects to the `/mcp` endpoint and gains natural-language access to all 14 tools — it can query live rider health, zone density, cycle briefings, dead run risk scores, and more without any custom integration code.

14 tools total (up from the original 8-tool plan — `get_zone_map` was added for the frontend map panel):

| Category | Tools |
|---|---|
| Zone | `get_zone_density`, `get_zone_recommendations`, `get_zone_map`, `get_zone_stress` |
| Dead Run | `get_order_risk_scores`, `get_dead_zone_snapshots` |
| Restaurant | `get_restaurant_risk`, `get_restaurant_delay_events` |
| Earnings | `get_rider_health`, `get_rider_interventions`, `get_rider_churn_signals` |
| Briefing | `get_cycle_briefing`, `get_cycle_history` |
| Ops | `get_operator_alerts` |

Each tool is a single optimised read-only SQL query (or a small number of queries). The "tool-per-DB-query" pattern means each MCP tool has a predictable response time and a clear purpose. Claude Desktop can compose tools to build richer analyses — for example, calling `get_zone_stress` to identify problem zones, then `get_rider_health` to find at-risk riders in those zones.

`fastapi-mcp` auto-generates MCP protocol endpoints from the FastAPI route definitions. The tool descriptions in each route's docstring appear directly in Claude Desktop's tool picker.


---

# 10. Key Bugs Fixed

> **Structure reference:** This section follows `/DOCS_STRUCTURE.md` §"Key Bugs Fixed" before writing. Each bug is documented with root cause, symptom, and fix so that the same class of error is never reintroduced.

---

## 10.1 DISPATCHER_TICK_SECS: 5→1 (EPH Collapse at High TIME_SCALE)

**Symptom:** At `TIME_SCALE=300`, `avg_eph` read as ~Rs.26/hr instead of the expected ~Rs.90+/hr. Riders were completing orders at a normal rate but earning almost nothing.

**Root cause:** `DISPATCHER_TICK_SECS=5` meant the event-stream checked for idle riders to dispatch to pending orders every 5 real seconds. At `TIME_SCALE=300`, 5 real seconds = 25 simulated minutes. Each order took 25 simulated minutes of idle time before dispatch, meaning each rider's effective hourly rate was artificially deflated by the time spent waiting for the dispatcher.

**Fix:** `DISPATCHER_TICK_SECS=1` — dispatcher checks every real second (3 simulated minutes at 300×). Orders are dispatched almost immediately after they enter `pending` state.

**Why not 0 (continuous loop)?** A pure `asyncio.sleep(0)` loop would saturate the event loop, starving other coroutines. 1 second is fast enough to be imperceptible at any simulation speed while remaining a reasonable yield point.

---

## 10.2 BASE_FARE_RS: 15→25 (Fare Calibration)

**Symptom:** EPH continued to read low (~Rs.63/hr) even after fixing the dispatcher tick. The fare per order was too low.

**Root cause:** `BASE_FARE_RS=15` produced an average fare of ~Rs.28/order (`15 + 4×km`). With a typical delivery taking 25–35 minutes, this translated to EPH of ~Rs.26/hr at the old dispatcher speed and ~Rs.63/hr at the fixed speed — still below the Rs.90 supplementary target.

**Fix:** `BASE_FARE_RS=25` raises the average fare to ~Rs.42/order, bringing EPH to ~Rs.89/hr — just inside the Rs.90 target under normal simulation conditions. This matches the Loadshare article's description of the Rs.70–85 actual range during the crisis (before ARIA's interventions).

**Math:** Average fare = `25 + 4 × avg_distance(~4.5km) + LD_BONUS(15 if >5km, ~30% of orders) = 25 + 18 + 4.5 = 47.5` estimated, actual simulated average ≈ Rs.42 (shorter average distance in simulation).

---

## 10.3 warm_start.py: UNIQUE Constraint (DELETE Before INSERT)

**Symptom:** After restarting Docker containers, warm start reported `riders_activated=0`. No riders were in an active session at cycle start, causing the first cycle to show all riders with EPH=0.

**Root cause:** `rider_sessions` has `UNIQUE(rider_id, session_date)`. When the containers were stopped, the event-stream's session cleanup ran and set `shift_end` on all open sessions (marking them closed). When warm start attempted to INSERT a new session for today's date, the UNIQUE constraint blocked the INSERT — a closed session for today already existed.

**Fix:**
```python
# DELETE closed-only sessions for today before warm start INSERT
DELETE FROM rider_sessions
WHERE session_date = TODAY
  AND shift_end IS NOT NULL
  AND rider_id IN (selected_rider_ids)
```

This removes the stale closed sessions, allowing warm start to create fresh active sessions.

---

## 10.4 warm_start.py: Wrong Column Name (session_id → cycle_id)

**Symptom:** warm start crashed with `asyncpg.exceptions.UndefinedColumnError: column "session_id" does not exist`.

**Root cause:** `rider_health_snapshots` uses `cycle_id` as the correlation field (matching the cycle that wrote the snapshot). The warm start script was using `session_id` — a column name from `rider_sessions`, not `rider_health_snapshots`.

**Fix:** changed the INSERT to use `cycle_id` with the sentinel value `00000000-0000-0000-0000-000000000000` — a UUID that identifies all warm-start-generated health snapshots.

---

## 10.5 Restaurant Threshold Mismatch

**Symptom:** The Supervisor's briefing reported "N high-risk restaurants" but the frontend panel showed a different count. A rider's intervention in the panel said "zone is dead, move to zone X" but the zone recommendation ID didn't match the zone name shown.

**Root cause:** The Restaurant agent's `operator_alerts_created` counter fired at `risk_score >= 0.50` (medium severity). The Supervisor used this counter as "high-risk restaurant count." The frontend panel shows restaurants with `delay_risk_score >= RESTAURANT_RISK_THRESHOLD = 0.65`. These are different thresholds, producing different counts.

**Fix:** Added `above_threshold_count` to the Restaurant agent's return dict — counting exactly the restaurants with `risk_score >= 0.65`. The Supervisor now reads `above_threshold_count` with a fallback to `operator_alerts` for backward compatibility.

---

## 10.6 Zone uuid[] asyncpg Bug

**Symptom:** Zone agent crashed with `asyncpg.exceptions.InvalidTextRepresentation: invalid input syntax for type uuid: "{uuid1, uuid2}"`.

**Root cause:** A PostgreSQL query expected `$4::uuid[]`. The code was passing a raw string `'{uuid1, uuid2, ...}'` (the PostgreSQL array literal format) instead of a Python `list`. asyncpg expects Python lists for array parameters — it handles the PostgreSQL array encoding internally.

**Fix:** Pass `[str(uuid) for uuid in zone_ids]` (a Python list) instead of the pre-formatted PostgreSQL literal string. asyncpg converts the Python list to the correct `uuid[]` wire format.

---

## 10.7 Model 1 F1=1.0 (Synthetic Data Overfitting)

**Symptom:** Model 1 (Rider Persona Classifier) achieved F1=1.0 on the validation set — a clear sign of overfitting on synthetic data.

**Root cause:** The synthetic data generator produced perfectly separable classes: dedicated riders had systematically higher earnings, more orders, and longer shifts with zero overlap in the feature space.

**Fix:** Two-step overlap injection:
1. Add Gaussian noise to numerical features across all rows.
2. Hard class swap: for `SWAP_RATE=0.12` (12%) of training rows, sample features from the opposite class distribution with 1.5× spread, forcing the model to handle within-class variance and class boundary ambiguity.

Result: F1 dropped from 1.0 to ~0.87 — a realistic performance level for a classification task with genuine class overlap.

---

## 10.8 SHAP XGBoost 2.x: _patch_xgb_booster()

**Symptom:** SHAP TreeExplainer raised `ValueError: Could not convert string '[9.294E1]' to float` when attempting to compute feature importances for XGBoost 2.x models.

**Root cause:** XGBoost 2.x serializes `base_score` as `[9.294E1]` (bracket notation for a scalar array) rather than `9.294E1`. SHAP's TreeExplainer reads `base_score` as a raw string and fails on the bracket format.

**Fix:**
```python
def _patch_xgb_booster(booster):
    cfg = json.loads(booster.save_config())
    raw = cfg["learner"]["learner_model_param"]["base_score"]
    clean = re.sub(r"\[([0-9.Ee+\-]+)\]", r"\1", str(raw))
    cfg["learner"]["learner_model_param"]["base_score"] = clean
    booster.load_config(json.dumps(cfg))
    return booster
```

Applied to each model's booster before computing SHAP importances at training time. SHAP importance JSON files are pre-computed once and served at inference, so the patch never runs in production.

---

## 10.9 Target Encoding Leakage (Model 2)

**Symptom:** Model 2 (Delivery Duration Scorer) achieved unrealistically low training RMSE (<1 minute) on the Kaggle dataset.

**Root cause:** `MEstimateEncoder` on categorical features (city, restaurant category) computed target means that included rows from the validation set — classic target leakage where the encoder saw future labels.

**Fix:** Replaced `MEstimateEncoder` with label encoding (integer mapping). Label encoding has no knowledge of the target variable, eliminating the leakage. Validation RMSE increased to a realistic ~7 minutes.

---

## 10.10 sklearn 1.6 FrozenEstimator

**Symptom:** `CalibratedClassifierCV(estimator=xgb_classifier)` raised `FutureWarning: Passing a fit estimator as the base estimator is deprecated`. In sklearn 1.6, this is an error.

**Fix:**
```python
try:
    from sklearn.frozen import FrozenEstimator
    calibrated = CalibratedClassifierCV(FrozenEstimator(clf), cv="prefit", method="isotonic")
except ImportError:
    # sklearn < 1.6
    calibrated = CalibratedClassifierCV(clf, cv="prefit", method="isotonic")
```

The `try/except ImportError` ensures backward compatibility — the same training code works on sklearn 1.5 (without FrozenEstimator) and 1.6+.

---

## 10.11 _NumpyEncoder: np.bool_ and np.int64 JSON Serialization

**Symptom:** `TypeError: Object of type bool_ is not JSON serializable` when writing agent output to `agent_memory` (which uses `json.dumps()`).

**Root cause:** XGBoost and sklearn return `numpy.bool_` and `numpy.int64` types in prediction outputs. Python's `json.dumps()` does not handle numpy scalar types.

**Fix:**
```python
class _NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.bool_):    return bool(obj)
        if isinstance(obj, np.int64):    return int(obj)
        if isinstance(obj, np.float64):  return float(obj)
        return super().default(obj)
```

Used as `json.dumps(output, cls=_NumpyEncoder)` in the ML server's prediction responses.

---

## 10.12 Duplicate n_estimators kwarg

**Symptom:** `TypeError: __init__() got multiple values for keyword argument 'n_estimators'` during hyperparameter search.

**Root cause:** `RandomizedSearchCV.best_params_` includes `n_estimators` (selected during search). Spreading `**best_params` into `XGBClassifier(n_estimators=best_iteration+1, **best_params)` caused a duplicate keyword argument.

**Fix:** Strip `n_estimators` from `best_params` before spreading: `best_params.pop('n_estimators', None)`.

---

## 10.13 basePath fetch() Calls

**Symptom:** All client-side API calls returned 404 on the Vercel deployment. The same calls worked locally.

**Root cause:** Next.js `basePath=/aria` prefixes `<Link>` and `router.push()` calls automatically, but does NOT prefix `fetch('/api/...')` calls. On Vercel, the frontend is served at `https://domain.com/aria` but `fetch('/api/kpi-summary')` resolves to `https://domain.com/api/kpi-summary` (missing the `/aria` prefix), which returns 404.

**Fix:** All client-side fetches rewritten to `/aria/api/*`. For example: `fetch("/aria/api/kpi-summary")` instead of `fetch("/api/kpi-summary")`.

---

## 10.14 Intervention Zone Mismatch

**Symptom:** A rider's intervention card showed "move to Zone A" in the recommendation text but the zone highlighted on the map was Zone B (a different zone).

**Root cause:** The intervention text was generated using the Zone agent's `rationale` (which referenced Zone A by name), but the `recommended_zone_id` field in `rider_interventions` was populated from a density-ranking fallback that selected Zone B.

**Fix:** `recommended_zone_id` now reads from the Zone agent's cross-agent context first (using the same `zone_rec_id` that generated the rationale text). The density-ranking fallback is used only when no Zone agent recommendation exists for this rider. Text and map now reference the same zone.

---

## 10.15 APScheduler Interval Stuck at 30s After Restart

**Symptom:** After restarting the MCP server container, the cycle was firing every 30 real seconds even though the simulation speed was set to `TIME_SCALE=10`. The Supervisor received correct data but was running 3× too fast.

**Root cause:** On MCP server startup, the auto-reschedule code (`urllib.request.urlopen`) fetches the current `time_scale` from the event-stream. If the event-stream is not yet ready when the MCP server starts (Docker startup ordering), the fetch fails silently and the scheduler stays at the default 15-minute interval, not the 30-second minimum that `reschedule_cycle(10)` would compute.

**Fix:** Manually call `POST /cycle/reschedule?time_scale=10` after every MCP server restart when using a non-default time scale. The frontend's SimClock component also calls this endpoint whenever the time scale badge is edited.


---

# 11. Design Decisions — Why One Over Another

> **Structure reference:** This section follows `/DOCS_STRUCTURE.md` §"Design Decisions" before writing. Each decision includes the options evaluated, the deciding factor, and the trade-offs accepted.

---

## 11.1 XGBoost vs All Alternatives

All four ARIA models use XGBoost. The decision was made once and applied consistently.

**vs LightGBM:** LightGBM uses leaf-wise tree growth (GOSS + EFB sampling) that can reach the same accuracy as XGBoost with fewer trees on large datasets. On ARIA's training sizes (15K–100K rows), the difference in training speed is seconds — not meaningful. LightGBM's SHAP integration exists but XGBoost's is more mature and was already integrated with the `_patch_xgb_booster()` fix. No benefit to switching.

**vs CatBoost:** CatBoost handles categorical features natively without encoding. ARIA's categorical features (zone_type, city_tier, weather, traffic) are already label-encoded via `constants.py` as part of the encoding consistency requirement. If CatBoost were introduced, the encoding layer would either become redundant or create a second encoding pipeline to maintain. The encoding consistency — single source of truth in `constants.py` shared between training and inference — is more important than CatBoost's native categorical handling.

**vs Random Forest:** Random Forest is a bag of independent trees (no boosting), which typically underperforms XGBoost on tabular data at the same tree count. Model 3's positive class rate of 12.8% makes calibrated probability estimation important — Random Forest's probability estimates are known to be poorly calibrated on imbalanced data without post-hoc calibration. XGBoost with `scale_pos_weight` handles imbalance more directly.

**vs Logistic Regression / Linear Models:** Linear models cannot capture the interaction features that matter most for ARIA's predictions. `peripheral_ld_risk = (zone_enc/3.0) × is_ld_order` is a multiplicative interaction — a peripheral zone long-distance order is qualitatively different from a hub long-distance order. Linear models approximate this with independent terms; XGBoost captures it via tree splits.

**vs Neural Networks (MLPs):** Neural networks are the strongest alternative on large tabular datasets (>1M rows). At 15K–100K rows, XGBoost consistently outperforms feedforward MLPs. Neural networks need large data to learn good representations; XGBoost's tree structure directly models the high-curvature decision boundaries in ARIA's feature space (e.g., "risk is high only when zone_type=peripheral AND is_ld_order=1 AND historical_dead_rate>0.4"). SHAP compatibility is also limited for neural networks — the tree-based SHAP values are exact; neural network SHAP requires sampling approximations.

**vs Neural Networks (transformer-based tabular):** FT-Transformer and TabNet are emerging alternatives that match or exceed XGBoost on large tabular datasets. They require significantly more tuning and have no mature SHAP equivalent. For a portfolio demonstrating interpretability (SHAP feature importances displayed in the UI), XGBoost is the correct choice.

**Deciding factor: SHAP interpretability.** ARIA's UI displays the top 3 SHAP feature importances for every ML prediction. This requires a model where SHAP values are (a) exact (not sampled approximations), (b) fast to compute, (c) pre-computable at training time. XGBoost's TreeExplainer satisfies all three.

---

## 11.2 Algorithms vs LLM for Computation

The architectural principle is: **algorithms and ML models compute; LLMs synthesize and explain**.

**Why not LLM-based computation?**

1. **Latency.** An LLM call takes 2–10 seconds per call. A 15-minute cycle with 300 riders calls the Earnings agent — computing EPH, health score, and churn signal for 300 riders via LLM calls would take 600–3000 seconds per cycle. Deterministic Python functions take milliseconds.

2. **Reproducibility.** LLM output is non-deterministic. The EPH formula `orders × avg_fare / elapsed_hours` always produces the same result for the same inputs. A LLM might round differently, use different units, or produce a computation error. Financial metrics like `total_earnings_shortfall_rs` must be deterministic — a 15-minute cycle that recalculates Rs.4,200 shortfall must match the Rs.4,200 in the previous cycle's DB record.

3. **Budget.** ARIA's 4–6 LLM calls per cycle represent the maximum viable budget on a shared GPU. Each additional LLM call reduces headroom for the Supervisor's RAG-grounded synthesis — the highest-value call.

**What LLMs are good for in ARIA:**
- Synthesizing multi-agent outputs into a coherent operator narrative.
- Producing natural-language explanations that non-technical ops staff can act on.
- Generating the "why" text in rider interventions that references cross-agent signals.

The LLM adds genuine value where deterministic templates would be either too rigid or too numerous to maintain. It does not add value for computing numbers.

---

## 11.3 Calibrated Classifier (Model 3) vs Raw Probability — and Why Model 4 Is NOT Calibrated

**Model 3 calibration reason.** Model 3 (Dead Zone Risk Predictor) produces `dead_zone_probability` that is used directly in alert thresholds, zone aggregation, and operator severity classification. Raw XGBoost probabilities are known to be overconfident on imbalanced data — the model pushes predictions toward 0.0 or 1.0 rather than the true underlying probability. With a 12.8% positive rate and `scale_pos_weight≈6.8`, raw probabilities are systematically distorted. CalibratedClassifierCV with isotonic regression corrects the calibration curve, making `dead_zone_probability=0.7` mean "this order has a ~70% chance of dead zone risk" rather than an arbitrary threshold-relative score.

**Why Model 4 is NOT calibrated.** Model 4's classifier produces a binary `below_threshold_flag` (will EPH fall below target?). This is used as a boolean classification, not as a probability threshold. Calibration changes the probability distribution but not the binary classification decision. The EPH trajectory alert system uses `alert_level = "intervene" if projected < 80 OR (below_threshold AND declining)` — the exact probability value of `below_threshold` is irrelevant; only its binary value matters. Adding isotonic calibration to Model 4 would increase training complexity without changing any decision downstream.

---

## 11.4 pgvector vs Pinecone/Chroma: No Extra Service + SQL Hybrid Filters + Low Volume

Covered in Section 7.1. The deciding argument in one sentence: at one episode per 15-minute cycle, the entire lifetime of ARIA's memory never justifies the operational overhead of a separate vector service, and pgvector's SQL pre-filter capability is superior to what most managed vector DBs offer without custom integration.

---

## 11.5 TimescaleDB vs Plain PostgreSQL: Continuous Aggregates Are the Deciding Feature

Covered in Section 9.2. The deciding argument: the 28-day zone density baseline query runs 96 times per day (every 15-minute cycle). Without continuous aggregates, each run is a full `AVG()` scan over 7M rows. TimescaleDB reduces this to a sub-millisecond lookup against a maintained materialized view.

---

## 11.6 Synthetic Data vs Waiting for Real Data

ARIA is a portfolio project demonstrating AI engineering capabilities. Waiting for Loadshare's real operational data is not feasible.

**Why not use public Swiggy/Zomato data:** No public datasets exist for rider earnings, session health, or zone-level dead zone metrics at the granularity ARIA needs (per-rider per-cycle EPH, per-zone density snapshots, per-restaurant prep time events).

**Why synthetic data is defensible:**

1. **Grounded in published research.** All key constants (12.8% dead zone positive rate, Rs.70–85 EPH crisis range, 30% churn rate, zone type dead zone probabilities) are sourced from Loadshare's own published Medium article. The synthetic data is designed to reproduce the article's statistics, not arbitrary values.

2. **One real-data model.** Model 2 (Delivery Duration Scorer) trains on the Kaggle gauravmalik26 dataset (41,953 real food delivery orders from 22 Indian cities). This gives the system one anchor to real delivery times.

3. **Overlap injection prevents overfitting.** Model 1's SWAP_RATE and Gaussian noise prevent the synthetic data's artificial separability from producing degenerate classifiers.

4. **The chatbot will disclose.** The planned documentation chatbot explicitly states the data strategy and its grounding.

---

## 11.7 Deterministic Pattern Detection vs LLM Pattern Detection

The Supervisor's pattern detection (churn_surge, dead_zone_pressure, restaurant_cascade) is deterministic Python code with explicit ratio+floor triggers.

**Why not use the LLM to detect patterns?**

1. **Consistency.** A pattern triggers when `at_risk_count >= 3 AND at_risk_pct >= 0.25`. This fires identically on every cycle. An LLM might detect the same pattern in one cycle and miss it in the next due to temperature variation — unreliable as a production monitoring signal.

2. **RAG retrieval requires structured pattern types.** The `supervisor_episode_memory` table stores `pattern_types TEXT[]` for GIN-indexed overlap filtering. If patterns were LLM-generated text, they would need NLP matching (unreliable, slow) instead of exact array overlap.

3. **Explainability.** The Supervisor's briefing JSON includes `patterns_detected` with `trigger_values` and `thresholds_hit` for each pattern. This is machine-readable and documentable. An LLM-generated pattern is a sentence.

The LLM's role is to **explain** what the deterministic patterns mean in context, not to detect them. This gives ARIA the best of both: reliable detection + human-quality explanation.

---

## 11.8 2-Hour Shortfall Cap vs Full Remaining Shift

Covered in Section 6.11. The deciding argument: `total_earnings_shortfall_rs` must be an actionable number, not a theoretical maximum. If a dedicated rider has 6 hours left in their shift and is at Rs.70/hr vs Rs.100/hr target, the "full shift" shortfall is `(100−70) × 6 = Rs.180`. But an operations team can only influence the next 2 hours meaningfully — re-routing, zone reassignment, removing bad orders from the queue. The 2-hour cap produces `(100−70) × 2 = Rs.60` — the number that operations can realistically act on.

---

## 11.9 Dual-Threshold Hysteresis vs Single Threshold (Zone Recommendations)

Covered in Section 6.10. Single threshold: at `stress_ratio=0.48`, recommend a move. At `0.52`, withdraw the recommendation. Next cycle: `0.48` again, recommend. This oscillation creates alert fatigue and confuses riders.

Dual threshold: `0.50` for DB flag (is this zone dead?), `0.45` for recommendation trigger (should we recommend a move?). Zones at 0.46–0.50 are flagged dead in the DB but don't trigger move recommendations. This stateless dead band eliminates oscillation without requiring any historical state or cycle-to-cycle memory.

---

## 11.10 Pre-Computed SHAP vs Runtime Computation

SHAP TreeExplainer requires the full training data (or a representative sample) to compute values accurately. At inference time (30 concurrent ML calls per cycle for the Dead Run agent), running SHAP per-prediction would require:
- Loading 1,923 training samples into memory per call.
- Running TreeExplainer.shap_values() per prediction.
- ~100ms–500ms per call, multiplied by 30 concurrent calls = 3–15 seconds per cycle from SHAP alone.

Pre-computed solution: at training time, compute SHAP importances once on a 500-sample representative set. Store as `shap_importance.json`. At inference, return the top-3 features from this pre-computed dict as "key factors."

Trade-off accepted: the SHAP importances are population-level (which features matter most on average), not instance-level (which features drove this specific prediction). For ARIA's use case (explaining why dead zones are risky in general), population-level importances are sufficient and clearer for non-technical ops staff.

---

## 11.11 Sequential Agents vs Concurrent Agents

Covered in Section 6.7. The fundamental constraint is the shared `asyncpg.Connection` — concurrent DB writes on the same connection produce a runtime error. The alternative (one connection per agent from the pool) is viable but:

1. Requires acquiring 5 connections simultaneously, leaving 0–11 connections for the Next.js API routes that run concurrently with the cycle.
2. Introduces inter-agent write conflicts: the Zone agent and Earnings agent both write `operator_alerts`; concurrent inserts could produce duplicate cycle-level alerts.
3. Adds complexity: concurrent agents need coordination for the Supervisor's `sub_results` collection.

The sequential design accepts slightly higher total cycle time in exchange for simplicity, safety, and predictable connection usage.

---

## 11.12 Outcome-Grounded RAG vs Pure Similarity RAG

Pure similarity RAG retrieves the most semantically similar past episodes and injects them into the prompt. The problem: similar situations that produced ineffective recommendations would rank equally with similar situations where the recommendations worked. Over time, the Supervisor would recycle ineffective strategies.

Outcome-grounded RAG adds `effectiveness_score = patterns_resolved / actionable_patterns` as a secondary ranking criterion:

```
Top-K ranking: (similarity DESC, effectiveness_score DESC)
```

Within same-similarity candidates, episodes where the recommended actions resolved the flagged patterns rank first. This is a closed-loop learning signal: cycles that produced good outcomes (patterns resolved, EPH improved) inform future cycles' recommendations. The Supervisor builds institutional memory of what works, not just what is similar.

The trade-off: outcome grounding requires waiting for the next cycle's data before an episode has a meaningful `effectiveness_score`. New episodes have `outcome_1cycle=NULL` and are excluded from retrieval until grounded. This creates a 15-minute delay before a new episode can influence future recommendations — acceptable for a system that already operates at 15-minute cycle granularity.


---

## 11.13 Deterministic embed_input vs LLM-Generated embed_input

The Supervisor stores two text fields per episode: `situation_summary` (LLM-generated narrative) and `embed_input` (the canonical string that is actually embedded). The decision to embed `embed_input` rather than `situation_summary` is a deliberate stability choice.

**The problem with embedding LLM output:**

LLM-generated summaries are non-deterministic. The same cycle — identical KPIs, identical patterns — can produce different `situation_summary` text across:
- Temperature variation (0.2 still produces variation across tokens)
- Model version changes (Qwen2.5-32B replaced by a future model)
- Prompt structure changes (adding RAG context changes the conditional distribution)

If the embedding is over the LLM summary, then two cycles with structurally identical situations might have cosine similarity of 0.3–0.5 just because the LLM phrased them differently. The RAG retrieval would miss the match.

**The solution — canonical deterministic string:**

```
embed_input = f"severity: {severity} | patterns: {','.join(pattern_types)} | 
kpis: avg_eph={avg_eph:.1f}, at_risk={at_risk_count}, dead_zones={dead_zone_count}, 
shortfall={total_shortfall:.0f}"
```

This string is assembled deterministically from the structured `financial_kpis` dict and `patterns_detected` list — the same inputs always produce the same string. Across model updates, prompt changes, and temperature variation, the embedding for "critical + churn_surge + dead_zone_pressure + avg_eph=82" is always the same vector. Retrieval results are stable.

**The trade-off accepted:** The canonical string is less semantically rich than the LLM summary. A situation summary might capture nuances ("despite recovery in Zone A, restaurant cascade in Zone B is dominating") that the canonical string cannot represent. However, the primary retrieval signal is structural similarity — same severity + same patterns + similar KPI magnitudes — not narrative nuance. The LLM's nuance is applied *after* retrieval, in the prompt context injection. Stability at retrieval time outweighs richness at retrieval time.

**`situation_summary` is still stored** for display purposes — it appears in the frontend's cycle history panel as the human-readable briefing narrative. It is just never used as the embedding source.
