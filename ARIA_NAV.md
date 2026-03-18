# ARIA — Navigation Index

This file is the routing layer for PageIndex retrieval. It mirrors the section
structure of ARIA_DOCS.md exactly. Each entry contains keywords and a summary
written to match how a human would naturally ask about the topic. The actual
technical content is fetched from ARIA_DOCS.md at answer time.

---

# 1. System Overview

## 1.1 What ARIA Is
**Keywords:** what is ARIA, ARIA definition, system purpose, portfolio project, Loadshare, Loadshare Networks, autonomous rider intelligence, gig economy, delivery riders, full stack, what does ARIA do, overview, what makes ARIA different, built for Loadshare, built for Loadshare Networks, why built, Loadshare Networks specifically, why Loadshare, networks, specifically, ARIA vs dashboard, not a dashboard, autonomous decision support
**Summary:** ARIA (Autonomous Rider Intelligence & Analytics System) is a full-stack portfolio project built for an AI Engineer role at Loadshare Networks. It monitors gig-economy delivery riders, detects emerging risks — dead zones, earnings collapse, restaurant delays, churn signals — and synthesises cross-domain intelligence every 15 simulated minutes into human-readable briefings. It is not a dashboard but an autonomous decision-support system.

### The Loadshare 2023 Research Context
**Keywords:** where did the idea come from, origin, inspiration, Loadshare crisis, April 2023, rider retention crisis, Arun Ravichandran, Medium article, research source, published research, EPH collapse, 30% churn, why build ARIA, why built for Loadshare, built for Loadshare Networks, Loadshare Networks specifically, why Loadshare specifically, built specifically, networks, specifically
**Summary:** ARIA's entire problem statement comes from a real published Medium article by Arun Ravichandran (Ex Senior Program Manager, Loadshare Networks) titled "How We Solved the Rider Retention Crisis" published January 2025. The article documents the April 2023 crisis where rider churn peaked at 30% and EPH collapsed to Rs.70-85/hr against expectations of Rs.90-100/hr due to static, rule-based operations tooling.

### Why Simulation
**Keywords:** why simulation, synthetic data, no real data, Loadshare data not public, simulation justification, realistic data generation, not real operational data
**Summary:** Loadshare's live operational data is not publicly available. ARIA uses a simulation engine grounded in the statistics published in the Medium article by Arun Ravichandran. The simulation is sophisticated enough to reproduce the exact failure modes described in the research at realistic rates.

## 1.3 Architecture Overview
**Keywords:** architecture, 6 layers, six layers, system architecture, how is ARIA structured, layers, client intelligence MCP ML data infrastructure, high level design, end to end, how everything connects, full system flow, complete system, order to briefing, how does it all work together, system flow, data flow, end-to-end flow
**Summary:** ARIA is structured as six layers: Client (Next.js dashboard, React Flow, Leaflet, WebSocket, Claude Desktop via MCP), Intelligence (5 LangGraph agents on Qwen2.5-32B via vLLM), MCP Server (FastAPI port 8001, 14 tools, APScheduler), ML Models and Algorithms (4 XGBoost models + 3 algorithmic modules), Data (PostgreSQL/TimescaleDB/PostGIS + Redis), and Infrastructure (Docker Compose, dual RTX 3090).

## 1.4 Why This Stack
**Keywords:** tech stack, why FastAPI, why PostgreSQL, why LangGraph, why vLLM, why XGBoost, why Next.js, why Redis, why TimescaleDB, technology choices, stack decisions

### Python + FastAPI vs Django/Flask/Node.js
**Keywords:** why FastAPI, FastAPI vs Django, FastAPI vs Flask, FastAPI vs Node, async, ASGI, streaming, API framework choice
**Summary:** FastAPI chosen for async-native design (asyncpg, SSE streaming), automatic OpenAPI docs, and low-overhead ASGI server — critical for real-time streaming responses and async database access patterns.

### PostgreSQL + TimescaleDB vs Pure Redis vs InfluxDB
**Keywords:** why PostgreSQL, why TimescaleDB, database choice, time series, SQL vs NoSQL, InfluxDB vs Postgres
**Summary:** PostgreSQL with TimescaleDB chosen for continuous aggregates (28-day baselines computed automatically), PostGIS spatial queries, pgvector for RAG, and full SQL expressiveness. TimescaleDB's hypertables handle time-series at scale without a separate TSDB service.

### Redis vs In-Process Cache
**Keywords:** why Redis, Redis vs memory cache, in-process cache, cross-service state, zone snapshots cache
**Summary:** Redis chosen for cross-service shared state — zone density snapshots written by event-stream are read by MCP server agents. In-process cache would not survive service restarts or span service boundaries.

### LangGraph vs Raw Python Loops vs LlamaIndex
**Keywords:** why LangGraph, LangGraph vs LangChain, LangGraph vs raw loops, agent framework choice, stateful agents
**Summary:** LangGraph chosen for explicit node-by-node execution graphs, built-in LangSmith observability, and clear state management. Raw Python loops lack observability; LlamaIndex is document-retrieval-focused, not agent-orchestration-focused.

### vLLM vs Ollama vs OpenAI API
**Keywords:** why vLLM, vLLM vs Ollama, LLM serving, local LLM, OpenAI API vs local, inference server
**Summary:** vLLM chosen for production-grade OpenAI-compatible API, PagedAttention for GPU memory efficiency, and tensor parallelism across dual 3090s. Ollama is simpler but lacks vLLM's throughput optimisations needed for 4-6 concurrent agent LLM calls per cycle.

### vLLM — Why Qwen2.5-32B over GPT-4 Mistral or Llama 3
**Keywords:** why Qwen, Qwen2.5 32B, model selection, LLM choice, GPT-4 vs Qwen, Mistral vs Qwen, Llama vs Qwen, local model
**Summary:** Qwen2.5-32B-Instruct-GPTQ-INT4 chosen for strong instruction-following in Indian logistics context, GPTQ INT4 quantisation fits dual RTX 3090 VRAM, and full local deployment with no API costs or data egress.

### Cloudflare Tunnel vs Nginx Reverse Proxy vs ngrok
**Keywords:** why Cloudflare Tunnel, Cloudflare vs Nginx, Cloudflare vs ngrok, reverse proxy, tunnel, external access
**Summary:** Cloudflare Tunnel chosen for zero-port-forwarding exposure of the local stack to the internet, free TLS, and DDoS protection — no router configuration needed unlike Nginx, and production-grade unlike ngrok.

### Vercel vs Self-Hosting vs AWS Amplify
**Keywords:** why Vercel, frontend hosting, Vercel vs AWS, deployment, Next.js hosting
**Summary:** Vercel chosen for native Next.js support, instant deployments from git push, free tier for portfolio projects, and global CDN — ideal for a portfolio project that needs to be accessible to interviewers.

### XGBoost vs LightGBM vs Neural Networks
**Keywords:** why XGBoost, XGBoost vs LightGBM, XGBoost vs neural networks, ML model choice, tree-based models
**Summary:** XGBoost chosen for tabular data performance, interpretability via SHAP, fast training on small datasets (50k rows), and deterministic predictions. See section 5.1 for detailed comparison against all alternatives.

## 1.5 Key Design Principle: Agents Explain, Algorithms Compute
**Keywords:** agents explain algorithms compute, design principle, LLM role, what do agents do, what does LLM do, no computation in LLM, separation of concerns, why not LLM for math
**Summary:** The core design principle: XGBoost models and algorithmic modules do all computation (risk scores, EPH, zone classification). LangGraph agents only explain, synthesise, and communicate those pre-computed results in natural language. The LLM never does arithmetic — it reads scores and writes briefings.

## 1.6 Quick Reference — Commonly Asked Questions
**Keywords:** FAQ, full tech stack, complete stack, Next.js, all technologies, common questions, quick reference, interview questions, frequently asked, EPH explained, system_zone_pressure, WebSocket, qwen3-embedding, MCP tools, how many zones, 180 zones, 12 cities, zone count, how many cities, zones across cities, zone and city count, 12 Indian cities

### Q: Where did the idea for ARIA come from? What is the source of the problem?
**Keywords:** idea origin, where did this come from, problem source, Loadshare research, Arun Ravichandran, Medium article, inspiration, why Loadshare
**Summary:** ARIA's problem statement is grounded in a real Medium article: "How We Solved the Rider Retention Crisis" by Arun Ravichandran (Ex Senior Program Manager, Loadshare Networks), published January 2025. Loadshare is a real Indian logistics company. The April 2023 crisis documented in the article directly maps to every detection module ARIA implements.

### Q: What is MCP (Model Context Protocol) and how does Claude Desktop connect?
**Keywords:** what is MCP, Model Context Protocol, Claude Desktop, MCP tools, how Claude connects, MCP server, tool calling
**Summary:** MCP (Model Context Protocol) is Anthropic's standard for exposing tools that AI assistants like Claude Desktop can call. ARIA's FastAPI server uses fastapi-mcp to expose 14 tools covering zone health, rider earnings, restaurant risk, cycle briefings, and more. Claude Desktop connects via Cloudflare Tunnel URL.

### Q: What is the full tech stack — every service, language, library, and database?
**Keywords:** full tech stack, complete stack, all technologies, every library, languages, services, what technologies
**Summary:** Backend: Python 3.11, FastAPI, LangGraph, LangChain, XGBoost, asyncpg, APScheduler, structlog. LLM: vLLM + Qwen2.5-32B-Instruct-GPTQ-INT4. Embeddings: Ollama + qwen3-embedding (4096-dim). DB: PostgreSQL 16 + TimescaleDB + PostGIS + pgvector. Cache: Redis 7. Frontend: Next.js 14, TypeScript, Tailwind, React Flow, Leaflet.js. Infrastructure: Docker Compose, dual RTX 3090.

### Q: How does the WebSocket live dashboard update in real time?
**Keywords:** WebSocket, live dashboard, real-time updates, how frontend updates, WebSocket connection, live data, streaming dashboard, frontend real-time
**Summary:** The MCP server maintains a Redis pub/sub bridge that publishes cycle completion events. The Next.js frontend connects via WebSocket to receive live briefings. When a 15-minute cycle completes, the supervisor writes to cycle_briefings, Redis publishes the event, and the WebSocket bridge pushes it to all connected frontend clients instantly.

### Q: What is EPH and how does the Earnings Guardian decide when to escalate a rider?
**Keywords:** EPH, earnings per hour, earnings guardian escalation, watch intervene alert, churn risk, EPH threshold, rider earnings, escalation logic
**Summary:** EPH (Earnings Per Hour) = total fare earned / active session hours. The Earnings Guardian uses Model 4 to predict each rider's EPH trajectory. Escalation has two levels: watch (EPH projected to miss target by 10-20%) and intervene (EPH projected to miss by >20% or multi-session declining trend detected). Fleet alert fires if >15% of active riders are at risk.

### Q: How does Model 4 avoid the shortcut of using EPH target as label leakage?
**Keywords:** Model 4 label leakage, eph_target leakage, feature leakage, shortcut learning, Model 4 design, label leakage fix, eph_target injected but excluded
**Summary:** eph_target is injected into the Model 4 regressor features (so it knows the target threshold) but is deliberately excluded from the classifier features (which predict whether the rider will miss the target). If eph_target were in the classifier, the model would trivially compare current EPH to target — a shortcut that would break on unseen data.

### Q: How does the Supervisor episodic memory RAG ground its LLM prompt with past outcomes?
**Keywords:** episodic memory, RAG, past outcomes, cycle briefings, supervisor memory, how RAG works in supervisor, outcome grounded, previous cycles
**Summary:** The Supervisor embeds a canonical string (severity + patterns + KPIs, not the LLM summary) using qwen3-embedding. It retrieves the top-3 most similar past episodes from supervisor_episode_memory filtered by recency (30 days), severity adjacency, city overlap, and embedding_status=ok. Past episode snippets (capped 1200 chars total) are injected into the LLM prompt if similarity ≥ 0.65 and ≥ 2 episodes qualify.

### Q: What are the 4 ML models in ARIA, what do they predict, and how were they trained?
**Keywords:** 4 ML models, four models, all models, model list, what does each model predict, model training, Model 1 2 3 4
**Summary:** Model 1: Rider Persona Classifier (XGBoost binary, supplementary vs dedicated, synthetic data). Model 2: Delivery Duration Scorer (XGBoost regressor, real Kaggle data, 41,953 rows, 22 Indian cities). Model 3: Dead Zone Risk Predictor (two-stage: classifier + regressor, calibrated probabilities). Model 4: Earnings Trajectory Forecaster (two-stage: regressor predicts EPH, classifier predicts miss risk).

### Q: How does the documentation chatbot work — what is PageIndex and how does it differ from vector RAG?
**Keywords:** docs chatbot, PageIndex, vector RAG, how chatbot works, documentation retrieval, RAG modes, chatbot architecture
**Summary:** Two retrieval modes. Vector RAG embeds the query with qwen3-embedding (asymmetric: query gets instruction prefix, docs do not), runs pgvector cosine similarity, returns top-5 chunks. PageIndex parses the doc hierarchy — LLM navigates chapters then sections in a 3-level tree search — no embeddings at query time, structure-aware retrieval.

### Q: What is system_zone_pressure and how does ARIA detect a platform-wide crisis?
**Keywords:** system_zone_pressure, platform-wide crisis, zone pressure, crisis detection, dead zone percentage, 50% dead zones, fleet-wide alert, city-wide dead zone
**Summary:** system_zone_pressure is a compound signal fired when ≥50% of all active zones are classified as dead simultaneously. Agent 03 Zone Intelligence computes zone classifications each cycle; if dead_zone_count / total_zones ≥ 0.50, the Supervisor receives system_zone_pressure=True and escalates to severity CRITICAL regardless of other signals.

### Q: Why did you choose this overall design approach over alternatives?
**Keywords:** why this approach, design rationale, overall architecture choices, why agents why ML why simulation, design decisions, architectural philosophy
**Summary:** Key choices: (1) Algorithms + ML for computation — not LLM math. (2) LangGraph agents for explainability — not dashboards. (3) Simulation over waiting for real data — Arun Ravichandran's published statistics make synthetic data defensible. (4) XGBoost over neural nets — tabular data at 50k rows, SHAP interpretability required. (5) Local vLLM over API — cost and data privacy for a portfolio project.

---

# 2. Data Strategy

## 2.1 The Dataset Search: What Exists and What Doesn't
**Keywords:** dataset search, why no public data, Loadshare data, Indian logistics data, dataset availability, what data exists
**Summary:** No public dataset exists for Indian gig-economy rider operations at Loadshare's operational specificity. The search concluded that only food delivery time prediction data (Kaggle) was usable — for Model 2 only. Models 1, 3, 4 required synthetic data grounded in the published Loadshare statistics.

## 2.2 Real Kaggle Data (Model 2) — Source, Scope, Feature Engineering
**Keywords:** Kaggle data, real data, Model 2 training data, food delivery dataset, gauravmalik26, delivery duration, Kaggle dataset, 41953 rows, 22 cities
**Summary:** Model 2 (Delivery Duration Scorer) trained on Kaggle dataset "Food Delivery Time Prediction" by gauravmalik26 — 41,953 rows, 22 Indian cities, features include distance, traffic, weather, vehicle type, order time. Feature engineering added interaction terms and fixed target encoding leakage from the original dataset.

## 2.3 Why Synthetic Data for 3 of 4 Models
**Keywords:** why synthetic data, synthetic data justification, no real data, defensible synthetic data, Models 1 3 4, synthetic grounded in research, data defensibility, Arun Ravichandran statistics, 30% churn rate, EPH Rs 70-85 collapse, published research grounded, is the synthetic data valid, is synthetic data trustworthy, can synthetic data be defended
**Summary:** Models 1 (persona), 3 (dead zone risk), and 4 (earnings trajectory) have no public equivalent dataset. Synthetic data was generated using distributions grounded in Arun Ravichandran's published statistics: 30% churn rate, EPH Rs.70-85 collapse range, dead zone frequency. This makes the data statistically defensible — it reproduces documented real-world failure rates at documented rates, not arbitrary invention.

## 2.3 Target Encoding Leakage Bug and Fix
**Keywords:** target encoding leakage, MEstimateEncoder, label encoding, leakage fix, Model 2 bug, encoding bug, data leakage
**Summary:** The original Kaggle dataset processing used MEstimateEncoder (target encoding) which leaked label information into features. Fixed by replacing with simple label encoding for all categorical features, retrained Model 2. This was caught when cross-validation AUC was suspiciously high.

## 2.4 Seeding Strategy: 30-Day History from Day One
**Keywords:** seeding, warm start, 30-day history, historical baseline, seed data, initial data, day one data
**Summary:** The database is seeded with 30 days of synthetic historical data before live simulation starts. This ensures 28-day rolling baselines (zone dead rates, restaurant prep times, rider EPH history) are populated from day one — the agents and algorithms never operate on cold-start data.

## 2.5 Zone Coverage: 180 Zones, 12 Cities
**Keywords:** zones, 180 zones, 12 cities, Indian cities, zone coverage, Bangalore zones, city coverage, zone data, how many zones, how many cities, zone count, city count, many zones, many cities, cover, aria zones, aria covers, zone and city count
**Summary:** 180 zones across 12 Indian cities derived from the coordinates in the Kaggle food delivery dataset. Cities include major metros and tier-2 cities. Not Bangalore-specific — reflects the multi-city nature of Loadshare's operations.

## 2.6 Encoding Defaults and Safe Priors
**Keywords:** encoding defaults, default values, safe priors, ZONE_TYPE_ENC_DEFAULT, CITY_TIER_ENC_DEFAULT, default encoding, constants
**Summary:** Default encoding values act as safe priors when zone type or city tier is unknown: ZONE_TYPE_ENC_DEFAULT=2 (residential — conservative, not hub), CITY_TIER_ENC_DEFAULT=0 (metropolitan), historical_dead_rate default=0.3, ASSUMED_EPH_RS_PER_HR=82.0. These prevent cold-start errors without biasing predictions aggressively.

---

# 3. Simulation Engine

## 3.0 How the Simulation Engine Works — Complete Overview
**Keywords:** how simulation works, simulation engine overview, SimClock, time acceleration, TIME_SCALE, 300x speed, simulated time, order factory, dispatcher, zone snapshot engine, sim_now, simulation architecture, how ARIA simulates, event-stream service, simulation tick, dispatcher tick, DISPATCHER_TICK_SECS, order creation rate, rider online offline, how orders are generated, how riders behave, simulation flow, what is the simulation doing, simulation design
**Summary:** The simulation engine runs in the event-stream FastAPI service (port 8003). A SimClock ticks at configurable TIME_SCALE (default 300x: 1 real second = 5 simulated minutes). Each tick: the OrderFactory probabilistically generates new delivery orders based on time-of-day demand curves; the Dispatcher scans pending orders and assigns them to available riders (DISPATCHER_TICK_SECS=1 real second); riders transition online/offline based on Poisson-inspired probability matching peak hours; the ZoneSnapshotEngine computes density scores and writes to Redis + PostgreSQL. sim_now is the current simulated timestamp injected into all agent prompts.

## 3.1 SimClock: Time Acceleration and sim_now
**Keywords:** SimClock, time scale, TIME_SCALE, time acceleration, simulated time, 300x speed, clock tick, wall clock vs simulated time, sim_now injection, simulated timestamp
**Summary:** SimClock drives the entire simulation. TIME_SCALE=300 means 1 real second = 300 simulated seconds (5 simulated minutes). sim_now = wall_clock_start + (real_elapsed × TIME_SCALE). This allows a full 24-hour simulation day to complete in ~5 real minutes. sim_now is passed to all agents and formatted as a human-readable timestamp in LLM prompts.

## 3.2 Order Factory: How Orders Are Generated
**Keywords:** order factory, order generation, how orders are created, order demand curve, time of day demand, peak hour orders, order rate, synthetic orders, delivery orders
**Summary:** The OrderFactory generates orders probabilistically based on a demand curve that mirrors real food delivery patterns: low demand 6-10am, peak lunch 12-14h, moderate 15-18h, peak dinner 19-22h, low overnight. Each order is assigned a pickup zone, destination zone, estimated prep time (from restaurant baseline), and initial state=pending.

## 3.3 Dispatcher: Order Assignment Logic
**Keywords:** dispatcher, order assignment, assign orders to riders, pending to assigned, dispatch logic, rider assignment, order dispatch
**Summary:** The Dispatcher runs every DISPATCHER_TICK_SECS real seconds. It queries all pending orders and all available riders (online, no current active order), then assigns the nearest available rider to each pending order (Manhattan distance on zone coordinates). Each assignment transitions the order from pending → assigned and records the assigned_at timestamp.

## 3.4 Zone Density Snapshot Engine
**Keywords:** zone density, density snapshot, stress ratio, density score, zone snapshot, how zones are tracked, zone state, order delta
**Summary:** Every simulation tick, the zone density snapshot engine computes density_score (active orders / zone capacity), stress_ratio (current density vs 28-day historical baseline), and order_delta (change from last snapshot). Results are written to Redis with TTL=900s and to zone_stress_snapshots table for the frontend heatmap.

## 3.6 Rider Online Probability (Poisson-Inspired)
**Keywords:** rider online probability, Poisson, rider availability, how riders go online, session probability, rider online model
**Summary:** Rider online/offline transitions modelled with a Poisson-inspired probability that varies by time of day, matching peak hour patterns from the Loadshare research. Higher probability during lunch (12-14h) and dinner (19-22h) peaks — replicates real gig worker availability patterns.

## 3.7 Order State Machine
**Keywords:** order state machine, order states, order lifecycle, pending assigned delivered, order flow, order transitions, order status
**Summary:** Orders transition through states: pending → assigned → picked_up → delivered (or cancelled/stranded). The central dispatcher assigns pending orders to available riders each tick. State transitions trigger EPH updates and dead zone risk re-scoring.

## 3.9 Restart Semantics
**Keywords:** restart, warm start, restart behaviour, simulation restart, what happens on restart, UNIQUE constraint, DELETE before INSERT
**Summary:** On restart, warm_start.py runs DELETE on closed sessions before INSERT to avoid UNIQUE(rider_id, session_date) constraint violations. The sentinel UUID 00000000-0000-0000-0000-000000000000 marks warm-start health snapshots for identification.

---

# 4. Algorithmic Modules

## 4.1 Zone Module (`algorithms/zone.py`)
**Keywords:** zone module, zone classification, zone algorithm, dead zone, low zone, normal zone, stressed zone, zone states, how zones are classified, zone scoring, city-wide dead zone, density score, stress ratio, sister zone
**Summary:** The zone module classifies all 180 zones every cycle into four states based on stress_ratio: dead (<0.3 density vs baseline), low (0.3-0.7), normal (0.7-1.3), stressed (>1.3). Outputs zone risk scores, sister zone rankings for repositioning, and fires system_zone_pressure when ≥50% zones are dead.

### Dead / Low / Normal / Stressed Thresholds
**Keywords:** zone thresholds, dead zone threshold, stressed zone threshold, zone classification thresholds, 0.3 0.7 1.3, stress ratio thresholds
**Summary:** Zone classification thresholds on stress_ratio: dead < 0.30, low 0.30-0.70, normal 0.70-1.30, stressed > 1.30. These are calibrated to the Loadshare research distribution of zone states observed during the April 2023 crisis.

### Sister Zone Ranking
**Keywords:** sister zone, zone recommendation, repositioning, alternative zone, rider repositioning, where to go, zone suggestion
**Summary:** When a rider is in a dead or low zone, the algorithm ranks nearby zones by a composite score: distance penalty + demand score + historical performance. Top-3 sister zones are recommended with estimated travel time and expected EPH improvement.

### Zone Pressure and Platform-Wide Crisis Detection
**Keywords:** system_zone_pressure, platform-wide crisis, city-wide dead zones, crisis detection, 50% zones dead, zone pressure flag, fleet crisis
**Summary:** system_zone_pressure = True when dead_zone_count / total_zones ≥ 0.50. This is computed by Agent 03 and passed to the Supervisor as a critical compound signal. It indicates a city-wide supply collapse — all riders need repositioning, not just individuals.

## 4.2 Session Module (`algorithms/session.py`)
**Keywords:** session module, EPH computation, earnings per hour, how EPH is calculated, EPH formula, session earnings, health score, rider health, shortfall
**Summary:** The session module computes per-rider EPH = total_fare_earned / active_session_hours. Health score combines EPH trajectory (60%), session consistency (25%), and dead run exposure (15%). Health classifications: healthy, watch, at-risk, critical. Shortfall = (eph_target - current_eph) × remaining_hours_capped_at_2.

### Current EPH
**Keywords:** EPH formula, how EPH is computed, earnings per hour calculation, EPH = fare / hours, current EPH, EPH computation
**Summary:** Current EPH = total_fare_earned_this_session / active_session_hours. Each delivered order adds BASE_FARE_RS (Rs.25) + distance-based component. EPH target is Rs.90/hr for supplementary riders and Rs.100/hr for dedicated riders.

### Health Score: 3-Component Formula
**Keywords:** health score, rider health, health formula, 3 component health, health score calculation, 60% 25% 15%
**Summary:** Health score = 0.60 × EPH_component + 0.25 × consistency_component + 0.15 × dead_run_component. EPH component uses a sigmoid-like mapping from current EPH ratio to target. Scores below 40.0 trigger health alerts.

### Shortfall Calculation
**Keywords:** shortfall, earnings shortfall, EPH shortfall, how shortfall is calculated, projected shortfall, remaining hours
**Summary:** Shortfall = (eph_target - current_eph) × min(remaining_session_hours, 2.0). The 2-hour cap prevents over-alarming for riders early in long shifts. This is the primary signal the Earnings Guardian uses to decide intervention urgency.

### Churn Signal: Multi-Session Detection
**Keywords:** churn signal, churn detection, multi-session, consecutive sessions, churn risk, rider churn, repeat underperformance
**Summary:** Churn signal fires when a rider has ≥3 consecutive sessions with EPH below target. Single-session underperformance is noise; multi-session is structural churn signal. The CHURN_SIGNAL_SESSIONS threshold is configurable (default 3).

## 4.3 Restaurant Module (`algorithms/restaurant.py`)
**Keywords:** restaurant module, restaurant scoring, congestion score, restaurant algorithm, prep time, queue congestion, restaurant risk
**Summary:** The restaurant module scores each active restaurant's queue congestion using a z-score against its 28-day historical baseline for that hour. Congestion_score = (current_prep_time - historical_mean) / historical_std. Scores above threshold trigger rider alerts and operator notifications.

### What Exactly Is Being Z-Scored
**Keywords:** z-score restaurant, what is z-scored, congestion z-score, prep time z-score, restaurant congestion formula
**Summary:** The z-score is computed on the current queue-overflow component of prep time vs the per-restaurant per-hour historical baseline. Not raw prep time — only the anomalous component above the restaurant's own normal operating baseline is scored.

### Two-Tier Baseline: 28-Day vs 14-Day Window
**Keywords:** 28-day baseline, 14-day baseline, restaurant baseline, historical baseline, rolling window, baseline window
**Summary:** 28-day window for broad statistical stability; 14-day window for recent trend sensitivity. The module uses the 28-day window as primary but flags restaurants where 14-day trend diverges significantly from 28-day — indicating a restaurant that has recently gotten consistently worse.

---

# 5. ML Models

## 5.0 All Four ML Models — Summary
**Keywords:** Model 3 dead zone risk predictor, Model 4 eph_target label leakage, all 4 ML models, four models summary, Model 1 2 3 4 overview, what models does ARIA have, complete model list
**Summary:** Four XGBoost models: Model 1 Rider Persona Classifier (binary: supplementary/dedicated, synthetic data, EPH target setting). Model 2 Delivery Duration Scorer (regressor, real Kaggle data, 41k rows). Model 3 Dead Zone Risk Predictor (two-stage classifier+regressor, CalibratedClassifierCV). Model 4 Earnings Trajectory Forecaster (two-stage: Stage 1 regressor predicts EPH, Stage 2 classifier predicts miss. eph_target is injected into regressor features but deliberately EXCLUDED from classifier features to prevent shortcut learning / label leakage).

## 5.1 Shared: Why XGBoost — All Alternatives Considered
**Keywords:** why XGBoost, XGBoost vs neural networks, neural networks vs XGBoost, XGBoost vs all alternatives, XGBoost choice, tabular data, SHAP interpretability, LightGBM CatBoost RandomForest LogisticRegression NeuralNetwork comparison, lightgbm vs xgboost, why not lightgbm, xgboost vs lightgbm, ml models, training models, other ml models, model training, alternatives considered, why xgboost over other models
**Summary:** XGBoost chosen for: (1) consistently best performance on tabular data at ARIA's scale (≤50k rows), (2) SHAP support for interpretability, (3) fast training, (4) deterministic predictions. LightGBM and CatBoost were close but XGBoost's ecosystem and SHAP integration were decisive.

## 5.2 Shared: Hyperparameter Search Strategy
**Keywords:** hyperparameter search, hyperparameters, model hyperparameters, RandomizedSearchCV, hyperparameter tuning, model tuning, cross validation
**Summary:** RandomizedSearchCV with 5-fold stratified cross-validation across 50 iterations. Search space covers n_estimators, max_depth, learning_rate, subsample, colsample_bytree. Best params saved alongside model artifacts.

## 5.3 Shared: Train/Val/Test Splits
**Keywords:** train test split, validation split, data split, 70 15 15, train val test
**Summary:** 70% train, 15% validation (for early stopping), 15% held-out test. Stratified splits for classifiers to maintain class balance. Temporal order respected for Model 4 (earnings trajectory uses session sequences).

## 5.4 Shared: Evaluation Metrics
**Keywords:** evaluation metrics, model metrics, F1 score, AUC, RMSE, MAE, how models are evaluated
**Summary:** Classifiers: F1-score (primary), AUC-ROC, precision, recall. Regressors: RMSE (primary), MAE. Two-stage models evaluated at each stage independently and end-to-end. SHAP feature importances computed post-training for interpretability.

## 5.5 Shared: SHAP Importances — Pre-Computed, Not Runtime
**Keywords:** SHAP, SHAP importances, feature importance, SHAP pre-computed, not runtime, shap_importance.json, what is SHAP, how is SHAP used, SHAP in ARIA, SHAP used, SHAP explainability, SHAP explanation, used in ARIA
**Summary:** SHAP importances pre-computed at training time and saved to shap_importance.json alongside each model. Not recomputed at inference — SHAP at inference on 32B-param trees would be prohibitively slow. Agents read the pre-computed importances to explain which features drove each prediction.

## 5.6 Shared: XGBoost 2.x SHAP Compatibility Fix
**Keywords:** XGBoost 2.x bug, SHAP bug, _patch_xgb_booster, base_score bug, XGBoost SHAP fix, SHAP compatibility
**Summary:** XGBoost 2.x changed how base_score is stored, breaking SHAP TreeExplainer. Fixed with _patch_xgb_booster() in utils.py which strips brackets from the base_score string representation before SHAP computation. Applied at model load time.

## 5.9 Model 1 — Rider Persona Classifier
**Keywords:** Model 1, rider persona classifier, supplementary dedicated classifier, persona model, rider type classification, how Model 1 works, binary classifier, persona prediction
**Summary:** Binary XGBoost classifier predicting rider persona: supplementary (Rs.90/hr target, part-time) vs dedicated (Rs.100/hr target, full-time). Features: session frequency, average session hours, historical EPH, shift patterns. Trained on synthetic data with hard-swap strategy (20% of rows get opposite-class features) to prevent overfitting to F1=1.0. Output used to set per-rider EPH target.

## 5.10 Model 2 — Delivery Duration Scorer
**Keywords:** Model 2, delivery duration, time prediction, how long delivery takes, duration model, Kaggle model, food delivery time, distance weather traffic
**Summary:** XGBoost regressor predicting delivery duration in minutes. Trained on real Kaggle food delivery dataset (41,953 rows). Features: distance, traffic condition encoding, weather encoding, vehicle type, time of day, restaurant prep time. Used by Agent 02 Dead Run Prevention to estimate whether an order will strand a rider in a dead zone.

## 5.11 Model 3 — Dead Zone Risk Predictor
**Keywords:** Model 3, dead zone risk, two-stage model, dead zone predictor, order risk score, will order lead to dead zone, dead run prediction, calibrated classifier
**Summary:** Two-stage XGBoost model: Stage 1 classifier predicts probability the delivery destination zone will be dead at delivery time. Stage 2 regressor predicts severity (how dead). Uses CalibratedClassifierCV (isotonic regression) for probability calibration. Interaction features: peripheral_ld_risk (peripheral × historical_dead_rate), dist_x_dead_rate (distance × dead_rate).

## 5.12 Model 4 — Earnings Trajectory Forecaster
**Keywords:** Model 4, earnings trajectory, EPH prediction, will rider miss target, earnings forecaster, trajectory model, churn prediction model, eph_target leakage protection, two-stage earnings model
**Summary:** Two-stage XGBoost model: Stage 1 regressor predicts projected EPH at end of shift. Stage 2 classifier predicts whether rider will miss their EPH target. eph_target injected into regressor features but excluded from classifier to prevent label leakage shortcut. Momentum features: eph_slope (3-session trend), eph_acceleration (rate of change of slope).

---

# 6. LangGraph Agents

## 6.0 All Five Agents — Summary
**Keywords:** all 5 agents, five agents summary, agent list, LangGraph agents overview, what agents does ARIA have, agent pipeline, LangGraph, LangSmith, LangSmith observability, LangChain, agent framework, why LangGraph, how LangGraph is used, StateGraph, stateful agents, LangGraph nodes, LangGraph state
**Summary:** Five LangGraph agents run sequentially each 15-minute cycle: Agent 01 Restaurant Intelligence (queue congestion anomaly detection), Agent 02 Dead Run Prevention (Model 3 scoring of active orders), Agent 03 Zone Intelligence (180-zone classification + repositioning), Agent 04 Earnings Guardian (EPH trajectory via Model 4 + churn detection), Agent 05 Supervisor (cross-agent synthesis + episodic RAG + cycle_briefing).

## 6.1 Why LangGraph vs Raw Python Loops
**Keywords:** why LangGraph, LangGraph vs LangChain, LangChain vs LangGraph, why not just LangChain, LangGraph vs raw loops, LangGraph justification, stateful agents, LangSmith observability, per-node tracing, TypedDict state, observable node boundaries, agent framework, StateGraph
**Summary:** LangGraph extends LangChain (uses its model integrations) but adds StateGraph: typed TypedDict state, per-node LangSmith tracing, and clean error isolation between nodes. Plain LangChain LCEL lacks explicit named state and per-node trace boundaries. LangGraph is chosen over raw Python loops for the same reasons plus conditional routing and error isolation per node.

## 6.2 BaseAgent Contract
**Keywords:** BaseAgent, agent contract, agent interface, how agents are structured, agent base class, common agent pattern
**Summary:** All five agents implement a BaseAgent contract: __init__ receives injected dependencies (db pool, Redis client, ML client, sim_now), run() is the entry point returning a standardised result dict, and each node is a pure function taking/returning the graph state. No global state, no side effects outside the defined outputs.

## 6.3 Dependency Injection via Closure (not LangGraph State)
**Keywords:** dependency injection, closure, how dependencies are passed, db pool injection, not LangGraph state, closure pattern
**Summary:** Heavy dependencies (asyncpg pool, Redis client, httpx ML client) are injected via Python closures at agent construction time — not passed through LangGraph state (which is serialised). This avoids serialisation overhead and keeps the graph state lightweight (only domain data, not infrastructure objects).

## 6.4 Agent Design Principle: No Computation in Agents
**Keywords:** no computation in agents, agents don't compute, algorithms compute, LLM doesn't do math, agent role, what agents don't do
**Summary:** Agents never compute risk scores, EPH, or zone classifications — those come from algorithmic modules and ML models. Agents read pre-computed results and use the LLM to synthesise, explain, and generate natural language alerts. This keeps agent logic testable, deterministic, and free of floating-point LLM arithmetic errors.

## 6.5 LLM Call Budget: 4–6 Calls per 15-Minute Cycle
**Keywords:** LLM calls, how many LLM calls, LLM budget, 4-6 calls per cycle, LLM usage, LLM efficiency
**Summary:** The entire ARIA system makes 4-6 LLM calls per 15-minute cycle: one synthesise call per agent (5 agents) plus one Supervisor generation call. Each call is capped at 400-700 tokens output. PageIndex navigation adds 2 extra LLM calls (L0 chapter + L1 section selection) but only for doc chat, not in the main cycle.

## 6.6 sim_now Injection: How Simulated Time Reaches LLM Prompts
**Keywords:** sim_now, simulated time, LLM time injection, how time reaches LLM, simulation time, time injection
**Summary:** sim_now (current simulated timestamp) is passed through the closure injection pattern to each agent and formatted as a human-readable string in LLM prompts. This ensures the LLM's synthesised briefings reference the correct simulated time, not wall-clock time. Critical for time-aware reasoning about shift patterns.

## 6.7 Why Not Concurrent Agents: Single DB Connection Design
**Keywords:** why sequential agents, why not parallel agents, concurrent agents, sequential execution, single connection, DB connection limit
**Summary:** Agents run sequentially (not concurrently) because the asyncpg pool is sized for the operational workload, not for 5 simultaneous large query bursts. Sequential execution also ensures each agent can read outputs written by previous agents (e.g., Supervisor reads zone scores written by Agent 03).

## 6.8 Agent 01 — Restaurant Intelligence
**Keywords:** Agent 01, restaurant intelligence, restaurant agent, restaurant congestion, restaurant alerts, queue anomaly, operator alerts, restaurant monitoring
**Summary:** Agent 01 detects restaurants with anomalously high queue congestion vs their 28-day per-hour baseline (z-score threshold). It creates rider alerts for those waiting at flagged restaurants and operator alerts for persistent congestion. Output: restaurant_scores, above_threshold_count, operator_alerts list.

## 6.9 Agent 02 — Dead Run Prevention
**Keywords:** Agent 02, dead run prevention, dead run agent, order scoring, prevent dead run, order risk, before dispatch, proactive alert, Model 3 scoring
**Summary:** Agent 02 scores all active (pending + assigned) orders using Model 3 (Dead Zone Risk Predictor) to identify deliveries likely to strand the rider in a dead zone post-delivery. Fires proactive rider alerts with sister zone repositioning suggestions. Pipeline: fetch_orders → score_orders → write_scores → create_alerts → synthesise.

## 6.10 Agent 03 — Zone Intelligence
**Keywords:** Agent 03, zone intelligence, zone agent, zone classification, zone recommendations, repositioning, 180 zones, sister zone, zone snapshots
**Summary:** Agent 03 classifies all 180 active zones into dead/low/normal/stressed states every cycle, writes zone_stress_snapshots for the frontend heatmap, and generates per-rider repositioning recommendations. Direct implementation of Loadshare's "Sister Zone Revolution" — live-ranked alternative zones replacing static assignments.

## 6.11 Agent 04 — Earnings Guardian
**Keywords:** Agent 04, earnings guardian, EPH monitoring, churn detection, rider earnings, escalation, watch intervene, EPH target, earnings alert, rider at risk, churn signal, fleet churn alert
**Summary:** Agent 04 scores each active rider's EPH trajectory using Model 4 (Earnings Trajectory Forecaster). Escalation logic: alert_level=watch when projected EPH misses target by 10-20%, alert_level=intervene when >20% miss or multi-session declining trend. Fleet churn alert fires when >15% of active riders are simultaneously at risk. Reads churn signals from session module.

### Pipeline
**Keywords:** earnings guardian pipeline, Agent 04 pipeline, how earnings guardian works step by step, fetch riders score riders write escalate
**Summary:** Pipeline nodes: fetch_riders (all active sessions with EPH history) → score_riders (Model 4 inference + session module health score) → write_scores (rider_health_snapshots table) → create_alerts (watch/intervene/fleet-churn) → synthesise (LLM generates per-rider and fleet-level briefing).

## 6.11b How the Cycle Triggers the Frontend: WebSocket and Redis Pub/Sub
**Keywords:** WebSocket, frontend live update, Redis pub sub, cycle_complete, how frontend updates, WebSocket bridge, real-time dashboard, after cycle completes, supervisor publishes, live refresh, push to frontend
**Summary:** After the Supervisor writes the cycle_briefing, it publishes to Redis channel aria:cycle_complete. A WebSocket bridge task running in the MCP server subscribes to this Redis channel and immediately pushes the cycle_complete event to all connected frontend WebSocket clients. The Next.js frontend React state updates on receipt — no polling required. This is how all 5 live dashboard panels update within seconds of each 15-minute cycle completing.

## 6.12 Agent 05 — Supervisor
**Keywords:** Agent 05, Supervisor agent, cross-agent synthesis, cycle briefing, supervisor, compound patterns, severity level, RAG supervisor, episodic memory, pattern detection
**Summary:** The Supervisor synthesises results from all four prior agents into a single cycle_briefing. It runs deterministic pattern detection (churn_surge, dead_zone_pressure, system_zone_pressure, compound patterns), Phase 2 episodic RAG (retrieve similar past situations), and generates a severity-graded natural language briefing with actionable recommendations.

### Pipeline (6 nodes, strictly linear)
**Keywords:** Supervisor pipeline, 6 nodes, ground_past_outcomes, validate_inputs, analyze_patterns, retrieve_context, call_llm, write_and_publish
**Summary:** Six nodes: ground_past_outcomes (resolves past episode outcomes from cycle_briefings history), validate_inputs (classifies each sub_result ok/partial/failed/missing), analyze_patterns (deterministic KPI and compound pattern detection), retrieve_context (Phase 2 RAG retrieval), call_llm (prompt + RAG context → briefing generation), write_and_publish (writes cycle_briefings + new episode memory).

## 6.13 Zone Pressure and Platform-Wide Crisis Detection
**Keywords:** zone pressure, what triggers system_zone_pressure, system_zone_pressure triggers critical alert, 50 percent dead zones, dead zones critical alert, system_zone_pressure agent, crisis trigger, platform crisis, compound signal, critical severity, zone crisis in agents
**Summary:** When Agent 03 detects that ≥50% of zones are dead, it sets system_zone_pressure=True in its output. The Supervisor treats this as an automatic CRITICAL severity override — regardless of individual agent results — and names it as the primary pattern in the cycle briefing.

---

# 7. Supervisor Episodic Memory (Phase 2 RAG)

## 7.0 Episodic Memory and Embedding System — Complete Overview
**Keywords:** episodic memory overview, RAG complete overview, qwen3-embedding, embedding model, 4096 dim, Ollama embedding, asymmetric retrieval, query instruction prefix, supervisor memory system, how RAG works, pgvector episodic, supervisor episode memory, past outcomes, what embedding model, which embedding model, embedding details, retrieval system overview, memory architecture
**Summary:** The Supervisor's episodic memory system uses qwen3-embedding (4096-dim, served by Ollama, MTEB #1 as of 2025) to embed past cycle situations. Asymmetric retrieval: queries get instruction prefix "Instruct: Given a conversational user question, retrieve the most relevant technical documentation passage.\nQuery:" — documents embedded raw. Embeddings stored in supervisor_episode_memory table (pgvector, exact scan). Retrieval: SQL hybrid filter (recency ≤30 days + severity + city overlap + embedding_status=ok + outcome not null) → Python cosine similarity ≥0.65 → min_support=2 → top-3 by (similarity, effectiveness_score) → snippets capped 1200 chars total.

## 7.1 Why pgvector, Not a Separate Vector DB
**Keywords:** why pgvector, pgvector vs Pinecone, pgvector vs Chroma, vector database choice, no extra service, SQL hybrid, embedding storage
**Summary:** pgvector chosen to keep all data in one PostgreSQL instance — no extra service to deploy, maintain, or pay for. SQL hybrid filters (recency, severity, city overlap) are trivial with WHERE clauses. At ARIA's volume (<1000 episodes), pgvector exact scan is <1ms. Pinecone/Chroma would add operational complexity with zero performance benefit.

## 7.2 The `supervisor_episode_memory` Table
**Keywords:** supervisor_episode_memory, episode memory table, memory table schema, RAG table, embedding table, episode storage
**Summary:** Table stores: situation_summary (text), embed_input (canonical string used for embedding), embedding (vector 4096-dim, qwen3-embedding), pattern_types (TEXT[]), city (TEXT[]), severity (text), outcome_1cycle/outcome_3cycle (JSONB), embedding_status ('ok'/'failed'), created_at. GIN index on pattern_types and city arrays, HNSW dropped (4096 > 2000 dim limit), exact scan used.

## 7.3 Outcome Grounding: What Gets Measured
**Keywords:** outcome grounding, outcome measurement, 1 cycle outcome, 3 cycle outcome, was the intervention effective, outcome tracking, episode effectiveness
**Summary:** For each stored episode, outcome_1cycle is filled on the next cycle run (did the KPIs improve?), outcome_3cycle is filled 3 cycles later (did the intervention have lasting effect?). The Supervisor uses ORDER BY timestamp to find the N+1 and N+3 cycle_briefings for grounding — not time arithmetic.

## 7.4 The Canonical embed_input: Why Not the LLM Summary
**Keywords:** embed_input, canonical embedding, why not LLM summary, deterministic embedding, embedding input design, canonical string
**Summary:** embed_input is a deterministically constructed string from severity + pattern_types + KPI values — NOT the LLM-generated summary text. LLM summaries vary in phrasing across cycles, making embeddings inconsistent. The canonical string ensures that semantically identical situations produce similar embeddings regardless of how the LLM phrased them.

## 7.5 Retrieval Pipeline: All Eight Steps
**Keywords:** retrieval pipeline, RAG pipeline, how episodes are retrieved, 8 steps, retrieval steps, episode retrieval
**Summary:** 8-step pipeline: (1) build embed_input for current situation, (2) call qwen3-embedding, (3) SQL filter: recency ≤30 days + severity_adjacent + city overlap + embedding_status=ok + outcome IS NOT NULL, (4) fetch candidates, (5) Python pattern overlap filter, (6) cosine similarity ≥0.65, (7) min_support=2 (need ≥2 qualifying episodes), (8) take top-3 by (similarity, effectiveness_score), cap snippets at 1200 chars.

## 7.6 Embedding Client: Technical Details
**Keywords:** embedding client, qwen3-embedding, how embeddings work, Ollama embedding, embedding model, 4096 dim, asymmetric retrieval, query instruction prefix, embedding details
**Summary:** Ollama serves qwen3-embedding (4096-dim, MTEB #1 as of 2025). Asymmetric retrieval: queries get instruction prefix "Instruct: Given a conversational user question, retrieve the most relevant technical documentation passage.\nQuery: " — documents are embedded raw (no prefix). This improves informal query → formal documentation matching. 60s timeout, 4-connection pool.

## 7.7 Documentation Chatbot — PageIndex RAG vs Vector RAG
**Keywords:** documentation chatbot, PageIndex vs vector RAG, chatbot modes, RAG modes, docs chat, how chatbot retrieves, chatbot architecture
**Summary:** Two retrieval modes for the docs chatbot. Vector RAG: qwen3-embedding cosine similarity finds top-5 chunks from docs_chunks table. PageIndex: ARIA_NAV.md navigation index is used to route to the right chapter/section, then content is fetched from ARIA_DOCS.md. PageIndex uses the LLM for 3-level tree navigation (chapter → section → subsection), no query-time embeddings.

---

# 8. Database Schema

## 8.1 Extension Stack
**Keywords:** database extensions, PostgreSQL extensions, TimescaleDB PostGIS pgvector, extension stack, what extensions are used
**Summary:** Four PostgreSQL extensions: TimescaleDB (time-series hypertables + continuous aggregates), PostGIS (spatial zone queries, Haversine distance), pgvector (4096-dim embeddings for RAG), uuid-ossp (UUID generation). All loaded at schema creation time.

## 8.2 Hypertables
**Keywords:** hypertables, TimescaleDB hypertables, what is a hypertable, time-series table, partitioned table, delivery_events, rider_health_snapshots
**Summary:** delivery_events and rider_health_snapshots are TimescaleDB hypertables partitioned by time. Hypertables enable chunk-based pruning for time-range queries — 28-day baseline queries scan only the relevant 2 monthly chunks rather than the full table.

## 8.3 Continuous Aggregates
**Keywords:** continuous aggregates, TimescaleDB aggregates, pre-aggregated data, 28-day rolling, automatic aggregation, materialized view
**Summary:** Continuous aggregates materialise 28-day rolling statistics (zone dead rates, restaurant baseline prep times) automatically. The algorithmic modules read from these aggregates rather than computing rolling windows at query time — critical for sub-second response in the 15-minute cycle.

## 8.4 UNIQUE(rider_id, session_date) Constraint
**Keywords:** UNIQUE constraint, rider session constraint, one session per day, warm start constraint, UNIQUE rider_id session_date
**Summary:** rider_sessions has UNIQUE(rider_id, session_date) to prevent duplicate sessions per rider per day. warm_start.py must DELETE closed sessions before INSERT to avoid constraint violations on restart — a bug fixed in development.

## 8.5 Order State Machine
**Keywords:** order states, order state machine, order status, pending assigned picked_up delivered, order lifecycle in database
**Summary:** Orders table tracks state transitions: pending → assigned → picked_up → delivered (terminal) or → cancelled/stranded (terminal). Each transition is timestamped. The state machine in the database mirrors the event-stream simulation state machine exactly.

## 8.6 Notable Design Choices in Key Tables
**Keywords:** database design choices, table design, schema decisions, notable schema, key table decisions
**Summary:** cycle_briefings uses ON CONFLICT (cycle_id) DO UPDATE to allow safe re-runs. zone_stress_snapshots is a hypertable with 15-minute chunk interval matching the cycle cadence. supervisor_episode_memory stores both raw embed_input and the embedding for auditability.

## 8.7 Index Strategy
**Keywords:** database indexes, index strategy, B-tree indexes, GIN indexes, HNSW, pgvector indexes, query performance
**Summary:** B-tree indexes on all foreign keys and timestamp columns. GIN indexes on pattern_types and city TEXT[] arrays in supervisor_episode_memory for fast overlap queries. No HNSW index on embedding columns (4096 dims > pgvector's 2000-dim HNSW limit) — exact scan is <1ms at current volume.

---

# 9. Infrastructure

## 9.0 Infrastructure and Deployment — Complete Overview
**Keywords:** WebSocket, real-time live dashboard, Redis pub sub, WebSocket bridge, cycle_complete event, live updates, how frontend updates live, infrastructure overview, Docker Compose 8 services, Cloudflare Tunnel, Vercel frontend, APScheduler cycle, auth gate, MCP tools, deployment overview, how ARIA is deployed, what runs where, service map, port map
**Summary:** ARIA runs 8 Docker Compose services: PostgreSQL (with TimescaleDB/PostGIS/pgvector), Redis, vLLM (port 8000), ML server (port 8002, internal only), event-stream (port 8003), MCP server (port 8001), Ollama (embeddings), and Next.js frontend (Vercel-hosted). Real-time WebSocket: after each 15-min cycle, Supervisor publishes to Redis channel aria:cycle_complete → WebSocket bridge in MCP server → all connected frontend clients update instantly (no polling). External access via Cloudflare Tunnel. Auth: frontend JWT cookie + MCP server X-API-Key + ML server X-Internal-Key.

## 9.1 Why Docker Compose vs Kubernetes
**Keywords:** why Docker Compose, Docker Compose vs Kubernetes, container orchestration, why not Kubernetes, single machine deployment
**Summary:** Docker Compose chosen for a single-machine deployment on personal hardware. Kubernetes would add operational overhead (control plane, node management) without benefit when all 8 services run on one machine. Docker Compose gives declarative service definitions, health checks, and volume management at zero overhead.

## 9.2 Why TimescaleDB
**Keywords:** why TimescaleDB, TimescaleDB justification, time series database, continuous aggregates, TimescaleDB vs InfluxDB, TimescaleDB decision
**Summary:** TimescaleDB is a PostgreSQL extension — no separate service, full SQL compatibility, and continuous aggregates replace the custom rolling-window queries that would otherwise run on every cycle. The 28-day zone dead rate baselines and restaurant prep time baselines are maintained automatically by the aggregate refresh policy.

## 9.3 Why Redis: Dual Role
**Keywords:** why Redis, Redis used, redis in ARIA, how Redis is used, Redis used in ARIA, Redis dual role, Redis cache and pub sub, Redis for zone snapshots and WebSocket, Redis use cases, used in ARIA
**Summary:** Redis serves two roles: (1) zone density snapshot cache (TTL=900s, written by event-stream, read by MCP agents — cross-service shared state), (2) pub/sub channel for WebSocket bridge (Supervisor publishes cycle_complete events, WebSocket server subscribes and pushes to frontend). One Redis instance, two use cases.

## 9.4 asyncpg Pool: min=4, max=16
**Keywords:** asyncpg pool, connection pool, database connections, min 4 max 16, pool sizing
**Summary:** asyncpg connection pool sized min=4, max=16. Minimum 4 keeps connections warm for the APScheduler cycle. Maximum 16 supports 5 agents × up to 3 concurrent queries each with headroom. Sized conservatively — PostgreSQL default max_connections=100 is not a constraint.

## 9.5 Why vLLM
**Keywords:** hardware, dual RTX 3090, ARIA hardware, what hardware, run on GPU, GPU hardware, hardware specs, why vLLM, vLLM justification, LLM inference server, vLLM vs Ollama, PagedAttention, tensor parallelism, vLLM serve, vLLM serving, how vLLM serves, serve LLM requests, how vLLM works, vLLM in ARIA
**Summary:** vLLM provides PagedAttention (near-zero KV cache waste), continuous batching, and tensor parallelism across dual RTX 3090s. Exposes an OpenAI-compatible API so LangChain and the docs chatbot use the same client. Ollama lacks these production throughput optimisations needed when multiple agents make concurrent LLM calls.

## 9.9 APScheduler Cycle Orchestration
**Keywords:** APScheduler, cycle scheduling, 15 minute cycle, how cycles are triggered, scheduler, cycle interval, reschedule
**Summary:** APScheduler runs the 15-minute agent cycle as a background job in the MCP FastAPI process. The interval is configurable via CYCLE_INTERVAL_MINS env var. After a TIME_SCALE change, POST /cycle/reschedule must be called to update the real-time interval (cycle interval = 15 * 60 / TIME_SCALE seconds).

## 9.10 WebSocket and Real-Time Architecture
**Keywords:** WebSocket, real-time, live dashboard, WebSocket architecture, how frontend updates live, Redis pub sub WebSocket, live cycle updates, WebSocket bridge
**Summary:** After each cycle completes, the Supervisor writes the cycle_briefing then publishes to Redis channel aria:cycle_complete. A WebSocket bridge task in the MCP server subscribes to this channel and pushes the event to all connected frontend WebSocket clients. Frontend React state updates immediately on receipt — no polling.

## 9.11 Auth Design
**Keywords:** no host port, ML server no host port, why no host port, ML server internal-only, host port mapping, ML server isolated, X-Internal-Key, Docker network security, internal Docker network, authentication, auth, login, how auth works, API key, JWT, auth gate, who can access
**Summary:** ML server has no host port mapping — it is deliberately isolated to the internal Docker network and never exposed to the host machine. This prevents unauthorized direct access; only services on the same Docker network can reach it. X-Internal-Key authenticates inter-service calls. Frontend uses a simple auth gate (JWT in cookie). MCP server APIs require X-API-Key header. Cloudflare Tunnel provides the external HTTPS endpoint.

## 9.12 MCP Tool Design
**Keywords:** MCP tools, 14 tools, what MCP tools are available, tool design, Claude Desktop tools, MCP endpoints
**Summary:** 14 MCP tools exposed via fastapi-mcp: zone-health, zone-map (GeoJSON for Leaflet), rider-earnings, restaurant-risk, cycle-briefing, agent-status, trigger-cycle, simulation-status, and more. Each tool is a FastAPI endpoint decorated with @mcp_tool. Claude Desktop discovers them automatically via MCP protocol.

---

# 10. Key Bugs Fixed

## 10.1 DISPATCHER_TICK_SECS: 5→1 (EPH Collapse at High TIME_SCALE)
**Keywords:** dispatcher tick, DISPATCHER_TICK_SECS, EPH collapse, tick interval bug, 5 to 1, high time scale EPH
**Summary:** At TIME_SCALE=300x, DISPATCHER_TICK_SECS=5 real seconds = 25 simulated minutes idle per order. Riders waited with no orders, killing EPH. Fixed by changing to DISPATCHER_TICK_SECS=1. This was the primary cause of EPH=0 readings during early testing.

## 10.2 BASE_FARE_RS: 15→25 (Fare Calibration)
**Keywords:** BASE_FARE_RS, fare calibration, base fare, Rs 25, fare too low, EPH too low, fare fix
**Summary:** BASE_FARE_RS=15 produced average fare of Rs.26/order, yielding EPH of ~26 Rs/hr — far below the Rs.70-85 range from Loadshare research. Raised to Rs.25 bringing average fare to Rs.42/order and EPH to ~63-90 Rs/hr range, matching published statistics.

## 10.3 warm_start.py: UNIQUE Constraint (DELETE Before INSERT)
**Keywords:** warm start bug, UNIQUE constraint, warm_start fix, DELETE before INSERT, session constraint violation
**Summary:** warm_start.py initial run gave riders_activated=0 because UNIQUE(rider_id, session_date) blocked INSERT when closed sessions from a prior run existed for today's date. Fix: DELETE closed-only sessions first, then INSERT new open sessions.

## 10.5 Restaurant Threshold Mismatch
**Keywords:** restaurant threshold mismatch, operator alerts, above threshold, panel mismatch, 0.5 vs 0.65, restaurant alert threshold
**Summary:** operator_alerts fired at score ≥0.5 (in agent code) but the frontend panel showed ≥0.65 — causing count mismatches. Fixed by adding above_threshold_count field to the restaurant agent output using the 0.65 threshold consistently. Supervisor now uses this field.

## 10.6 Zone uuid[] asyncpg Bug
**Keywords:** uuid array bug, asyncpg uuid[], zone recommendations bug, PostgreSQL literal string, asyncpg expects Python list
**Summary:** Zone agent was passing PostgreSQL literal string '{uuid1,uuid2}' for $4::uuid[] parameter. asyncpg expects a Python list, not a PostgreSQL literal. Fixed in zone_agent.py by passing a Python list directly.

## 10.7 Model 1 F1=1.0 (Synthetic Data Overfitting)
**Keywords:** Model 1 overfitting, F1=1.0, synthetic data overfitting, hard swap strategy, persona classifier overfitting
**Summary:** Model 1 achieved F1=1.0 on test set — a sign of overfitting to perfectly separable synthetic patterns. Fixed with hard-swap strategy: 20% of training rows have their class label swapped with features from the other class, introducing realistic overlap that prevents the model from memorising perfect boundaries.

## 10.8 SHAP XGBoost 2.x: _patch_xgb_booster()
**Keywords:** SHAP bug, XGBoost 2.x SHAP, _patch_xgb_booster, base_score brackets, SHAP compatibility fix
**Summary:** XGBoost 2.x stores base_score as '[0.5]' (with brackets) instead of '0.5'. SHAP's TreeExplainer reads this as a string and crashes. Fixed with _patch_xgb_booster() which strips brackets before SHAP computation. Applied at model load time in loader.py.

## 10.15 APScheduler Interval Stuck at 30s After Restart
**Keywords:** APScheduler stuck, scheduler stuck, 30 seconds, reschedule after restart, cycle interval wrong after restart
**Summary:** After MCP server restart, APScheduler reads CYCLE_INTERVAL_MINS but doesn't know the current TIME_SCALE from the running event-stream. The real interval gets set to 15 * 60 / 1 = 900s instead of the correct scaled interval. Fix: always call POST /cycle/reschedule?time_scale=N after MCP restart.

---

# 11. Design Decisions — Why One Over Another

## 11.1 XGBoost vs All Alternatives
**Keywords:** XGBoost design decision, why XGBoost, XGBoost vs LightGBM CatBoost neural networks, model selection rationale
**Summary:** XGBoost selected over LightGBM (comparable performance, XGBoost SHAP ecosystem better), CatBoost (no significant advantage for this data), Random Forest (XGBoost dominates on tabular), Logistic/Linear Regression (too simple for interaction features), Neural Networks (overkill for ≤50k tabular rows, no interpretability).

## 11.2 Algorithms vs LLM for Computation
**Keywords:** algorithms vs LLM, why not LLM for computation, LLM hallucination math, deterministic computation, algorithms compute LLM explains
**Summary:** LLMs hallucinate on precise arithmetic, cannot reliably compute EPH or z-scores. Algorithmic modules guarantee determinism and auditability. Design principle: algorithms + ML models produce all numbers, LLM agents only interpret and explain those numbers in natural language.

## 11.3 Calibrated Classifier (Model 3) vs Raw Probability
**Keywords:** calibrated classifier, CalibratedClassifierCV, isotonic regression, probability calibration, Model 3 calibration, raw probability vs calibrated
**Summary:** Model 3 uses CalibratedClassifierCV with isotonic regression to convert raw XGBoost output scores to calibrated probabilities. Raw XGBoost probabilities are not well-calibrated (tend to cluster near 0 and 1). Calibrated probabilities are required for the risk threshold comparisons (DEAD_ZONE_RISK_THRESHOLD=0.60) to be meaningful. Model 4 is NOT calibrated because it uses a regression stage first.

## 11.4 pgvector vs Pinecone/Chroma
**Keywords:** pgvector vs Pinecone, pgvector vs Chroma, vector database choice, why pgvector, no extra service
**Summary:** pgvector keeps all data in the existing PostgreSQL instance — no extra service, no extra API key, full SQL hybrid filtering (WHERE recency AND severity AND city overlap), and at <1000 episodes the exact scan is <1ms. Pinecone and Chroma would add operational complexity with zero performance benefit at this volume.

## 11.5 TimescaleDB vs Plain PostgreSQL
**Keywords:** why TimescaleDB, TimescaleDB vs plain Postgres, continuous aggregates vs manual, time series choice
**Summary:** TimescaleDB's continuous aggregates are the decisive feature — they maintain 28-day rolling zone dead rates and restaurant baselines automatically without any cron jobs or manual materialised view refreshes. Plain PostgreSQL would require periodic batch jobs to maintain these aggregates.

## 11.6 Synthetic Data vs Waiting for Real Data
**Keywords:** synthetic data vs real data, why synthetic, wait for real data vs build now, data defensibility, Loadshare statistics
**Summary:** Waiting for real Loadshare data is not feasible for a portfolio project. Synthetic data grounded in Arun Ravichandran's published statistics (30% churn, EPH Rs.70-85 collapse, 180-zone city coverage) is statistically defensible. The simulation reproduces documented failure modes at documented rates — not arbitrary invention.

## 11.7 Deterministic Pattern Detection vs LLM Pattern Detection
**Keywords:** deterministic pattern detection, LLM pattern detection, why not LLM for patterns, rule-based detection, compound patterns, Supervisor pattern detection
**Summary:** Supervisor uses hard-coded ratio+floor triggers for pattern detection (e.g., churn_surge when >15% riders at risk AND absolute count ≥3). LLM pattern detection would be non-deterministic and unpredictable across cycles. Deterministic detection ensures the same conditions always produce the same pattern flags — critical for outcome grounding and RAG retrieval.

## 11.8 2-Hour Shortfall Cap vs Full Remaining Shift
**Keywords:** 2 hour cap, shortfall cap, 2 hour shortfall, why 2 hours not full shift, shortfall calculation cap
**Summary:** Shortfall uses min(remaining_hours, 2.0) cap. Full remaining shift would over-alarm for riders early in a 6-hour shift with one bad hour. The 2-hour cap focuses on the near-term recoverable window — if a rider can't recover EPH in 2 hours, that's a genuine structural problem, not a temporary dip.

## 11.9 Dual-Threshold Hysteresis vs Single Threshold (Zone Recommendations)
**Keywords:** hysteresis, dual threshold, zone recommendations, 0.50 vs 0.45, why two thresholds, zone oscillation prevention
**Summary:** Zone recommendations use 0.50 threshold for DB writes but 0.45 for recommendation trigger. The lower trigger threshold prevents oscillation: a zone at stress_ratio=0.48 would otherwise flip in/out of recommendation state every cycle. Hysteresis creates a stable band — zones enter recommendation mode at 0.45, exit only when stress_ratio rises back above 0.50.

## 11.10 Pre-Computed SHAP vs Runtime Computation
**Keywords:** pre-computed SHAP, SHAP at training time, why not runtime SHAP, SHAP performance, SHAP speed
**Summary:** SHAP TreeExplainer on a 500-estimator XGBoost model over 100+ riders takes 2-5 seconds per model — unacceptable in a 15-minute cycle where all 4 models run. Pre-computed importances at training time are stored as shap_importance.json and read in microseconds. The tradeoff: importances are global (not per-prediction), but sufficient for the explanation use case.

## 11.11 Sequential Agents vs Concurrent Agents
**Keywords:** sequential vs concurrent agents, why sequential, parallel agents, why not concurrent, agent execution order
**Summary:** Sequential execution chosen because: (1) later agents need outputs from earlier agents (Supervisor reads all 4 outputs), (2) asyncpg pool would be exhausted by 5 simultaneous bulk query bursts, (3) LangGraph StateGraph with sequential nodes is simpler to debug and trace. Future concurrent execution is possible if per-agent connection pools are introduced.

## 11.12 Outcome-Grounded RAG vs Pure Similarity RAG
**Keywords:** outcome grounded RAG, why outcome grounded, RAG with outcomes, effectiveness score, not pure similarity, RAG design
**Summary:** Pure similarity RAG retrieves past situations that look similar but may have had bad outcomes (wrong interventions, false alarms). Outcome-grounded RAG filters to episodes where outcomes were measured and uses effectiveness_score to rank — so the LLM learns from situations where the intervention actually worked.

## 11.13 Deterministic embed_input vs LLM-Generated embed_input
**Keywords:** deterministic embed_input, LLM embed_input, canonical embedding, why deterministic, embedding consistency, embed_input design
**Summary:** LLM-generated summaries vary in phrasing cycle-to-cycle (same situation described differently). Embedding these would make similar situations produce dissimilar vectors. Deterministic embed_input = severity + sorted pattern_types + rounded KPI values ensures that semantically identical situations always produce the same string and therefore similar embeddings.
