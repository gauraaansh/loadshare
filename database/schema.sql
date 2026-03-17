-- ============================================================
-- ARIA — Autonomous Rider Intelligence & Analytics System
-- PostgreSQL Schema
-- ============================================================
-- Run order matters. Extensions first, then reference tables,
-- then operational tables, then time-series tables, then
-- agent/output tables, then indexes.
-- ============================================================

-- ── EXTENSIONS ──────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS timescaledb;   -- time-series partitioning
CREATE EXTENSION IF NOT EXISTS postgis;        -- geo functions (zone boundary queries)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";    -- uuid_generate_v4()
CREATE EXTENSION IF NOT EXISTS vector;         -- pgvector: Supervisor episodic memory (Phase 2)

-- ============================================================
-- SECTION 1 — REFERENCE / STATIC TABLES
-- These change rarely. Seeded once, read constantly.
-- ============================================================

-- ── ZONES ───────────────────────────────────────────────────
-- Represents a geographic delivery zone in a city.
-- sister_zone_ids is a static seed; Zone Intelligence Agent
-- overrides recommendations dynamically at runtime.
CREATE TABLE zones (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                VARCHAR(100) NOT NULL,          -- e.g. "Koramangala 4B"
    city                VARCHAR(100) NOT NULL DEFAULT 'Bangalore',
    centroid_lat        DOUBLE PRECISION NOT NULL,
    centroid_lng        DOUBLE PRECISION NOT NULL,
    boundary_geojson    JSONB,                          -- GeoJSON polygon for map rendering
    area_km2            DOUBLE PRECISION,               -- for density normalisation
    sister_zone_ids     UUID[],                         -- static seed; agent overrides at runtime
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RESTAURANTS ─────────────────────────────────────────────
-- A pickup location. Risk scores updated each agent cycle.
CREATE TABLE restaurants (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                VARCHAR(200) NOT NULL,
    zone_id             UUID NOT NULL REFERENCES zones(id),
    lat                 DOUBLE PRECISION NOT NULL,
    lng                 DOUBLE PRECISION NOT NULL,
    avg_prep_time_mins  DOUBLE PRECISION,               -- rolling historical average
    last_risk_score     DOUBLE PRECISION,               -- 0.0–1.0, updated each cycle
    last_scored_at      TIMESTAMPTZ,
    is_blacklisted      BOOLEAN NOT NULL DEFAULT FALSE, -- hard blacklist (legacy flag)
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RIDERS ──────────────────────────────────────────────────
-- A delivery partner on the platform.
-- persona_type seeded from classifier; updated as more data comes in.
CREATE TABLE riders (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                VARCHAR(200) NOT NULL,
    phone               VARCHAR(20),
    home_zone_id        UUID NOT NULL REFERENCES zones(id),
    vehicle_type        VARCHAR(50) NOT NULL,           -- 'bike' | 'scooter' | 'bicycle'
    rating              DOUBLE PRECISION DEFAULT 5.0,
    persona_type        VARCHAR(30),                    -- 'supplementary' | 'dedicated' | NULL (unclassified)
    persona_confidence  DOUBLE PRECISION,               -- 0.0–1.0
    persona_updated_at  TIMESTAMPTZ,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    onboarded_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- SECTION 2 — OPERATIONAL / TRANSACTIONAL TABLES
-- High write frequency. Core business events.
-- ============================================================

-- ── ORDERS ──────────────────────────────────────────────────
-- One row per delivery order. Central fact table.
CREATE TABLE orders (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rider_id            UUID REFERENCES riders(id),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id),
    pickup_zone_id      UUID NOT NULL REFERENCES zones(id),
    delivery_zone_id    UUID NOT NULL REFERENCES zones(id),
    pickup_lat          DOUBLE PRECISION NOT NULL,
    pickup_lng          DOUBLE PRECISION NOT NULL,
    delivery_lat        DOUBLE PRECISION NOT NULL,
    delivery_lng        DOUBLE PRECISION NOT NULL,
    distance_km         DOUBLE PRECISION,
    is_long_distance    BOOLEAN NOT NULL DEFAULT FALSE, -- crosses zone boundary
    status              VARCHAR(30) NOT NULL DEFAULT 'pending',
    -- status flow: pending → assigned → rider_inbound → picked_up → en_route_delivery → delivered | failed
    weather_condition   VARCHAR(30),                    -- 'clear' | 'rain' | 'heavy_rain' | 'fog'
    traffic_density     VARCHAR(30),                    -- 'low' | 'medium' | 'high' | 'jam'
    expected_prep_mins  DOUBLE PRECISION,               -- restaurant's estimated prep time
    actual_prep_mins    DOUBLE PRECISION,               -- filled on pickup
    expected_delivery_mins DOUBLE PRECISION,
    actual_delivery_mins   DOUBLE PRECISION,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    assigned_at         TIMESTAMPTZ,
    rider_inbound_at    TIMESTAMPTZ,
    picked_up_at        TIMESTAMPTZ,
    delivered_at        TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    failure_reason      VARCHAR(200)
);

-- ── RIDER SESSIONS ──────────────────────────────────────────
-- One row per rider per working day (shift).
-- health_score and eph updated each agent cycle.
CREATE TABLE rider_sessions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rider_id            UUID NOT NULL REFERENCES riders(id),
    session_date        DATE NOT NULL,
    shift_start         TIMESTAMPTZ,
    shift_end           TIMESTAMPTZ,                    -- NULL if still active
    total_orders        INTEGER NOT NULL DEFAULT 0,
    total_earnings      DOUBLE PRECISION NOT NULL DEFAULT 0.0,  -- in INR
    total_distance_km   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    idle_time_mins      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    dead_runs_count     INTEGER NOT NULL DEFAULT 0,
    long_distance_count INTEGER NOT NULL DEFAULT 0,
    eph                 DOUBLE PRECISION,               -- earnings per hour, computed at session end
    health_score        DOUBLE PRECISION,               -- 0–100, updated each cycle
    below_threshold     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (rider_id, session_date)
);

-- ── RIDER LOCATION UPDATES ──────────────────────────────────
-- Append-only. Simulated event stream writes here every 2 min
-- per active rider. Agent reads latest row per rider.
-- Hypertable for time-series efficiency.
CREATE TABLE rider_location_updates (
    id                  UUID NOT NULL DEFAULT uuid_generate_v4(),
    rider_id            UUID NOT NULL REFERENCES riders(id),
    lat                 DOUBLE PRECISION NOT NULL,
    lng                 DOUBLE PRECISION NOT NULL,
    current_zone_id     UUID REFERENCES zones(id),
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Convert to hypertable (TimescaleDB) — partitioned by timestamp
SELECT create_hypertable('rider_location_updates', 'timestamp');

-- ============================================================
-- SECTION 3 — TIME-SERIES SNAPSHOT TABLES
-- All hypertables. Written each cycle, queried for windows.
-- ============================================================

-- ── ZONE DENSITY SNAPSHOTS ──────────────────────────────────
-- Written every 15 min by the event stream.
-- Zone Intelligence Agent queries this heavily.
CREATE TABLE zone_density_snapshots (
    zone_id             UUID NOT NULL REFERENCES zones(id),
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    order_count         INTEGER NOT NULL DEFAULT 0,
    active_rider_count  INTEGER NOT NULL DEFAULT 0,
    density_score       DOUBLE PRECISION,               -- orders / area_km2, normalised
    stress_ratio        DOUBLE PRECISION,               -- current_density / historical_baseline
    order_delta         INTEGER NOT NULL DEFAULT 0      -- order_count change vs previous snapshot (surge signal)
);
SELECT create_hypertable('zone_density_snapshots', 'timestamp');

-- ── RESTAURANT DELAY EVENTS ─────────────────────────────────
-- Written on every pickup completion.
-- Restaurant Intelligence Agent trains and queries this.
CREATE TABLE restaurant_delay_events (
    id                  UUID NOT NULL DEFAULT uuid_generate_v4(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id),
    order_id            UUID NOT NULL REFERENCES orders(id),
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expected_prep_mins  DOUBLE PRECISION NOT NULL,
    actual_prep_mins    DOUBLE PRECISION NOT NULL,
    delay_mins          DOUBLE PRECISION NOT NULL,      -- actual - expected (negative = early)
    weather_condition   VARCHAR(30),
    hour_of_day         SMALLINT NOT NULL,              -- 0-23, denormalised for fast ML queries
    day_of_week         SMALLINT NOT NULL               -- 0-6, denormalised
);
SELECT create_hypertable('restaurant_delay_events', 'timestamp');

-- ============================================================
-- SECTION 4 — AGENT OUTPUT TABLES
-- Written by agents each cycle. Read by frontend + supervisor.
-- ============================================================

-- ── AGENT MEMORY ────────────────────────────────────────────
-- Supervisor reads this to give each agent context from
-- the previous cycle. One row per agent per cycle.
CREATE TABLE agent_memory (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_name          VARCHAR(50) NOT NULL,
    cycle_id            UUID NOT NULL,
    output_json         JSONB NOT NULL,                 -- full agent output, queryable
    summary_text        TEXT,                           -- LLM-generated one-liner for supervisor
    execution_ms        INTEGER,                        -- how long agent took
    status              VARCHAR(20) NOT NULL DEFAULT 'success', -- 'success' | 'partial' | 'failed'
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── CYCLE BRIEFINGS ─────────────────────────────────────────
-- One row per full 15-min cycle. Frontend reads latest.
CREATE TABLE cycle_briefings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id            UUID NOT NULL UNIQUE,
    briefing_json       JSONB NOT NULL,                 -- full structured briefing
    alert_count         INTEGER NOT NULL DEFAULT 0,
    severity_level      VARCHAR(20) NOT NULL DEFAULT 'normal', -- 'normal' | 'warning' | 'critical'
    execution_ms        INTEGER,                        -- total cycle wall time
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── SUPERVISOR EPISODE MEMORY (Phase 2 — RAG) ────────────────
-- One row per completed cycle. Supervisor reads past episodes for
-- retrieval-augmented generation. Outcomes grounded 1 and 3 cycles later.
CREATE TABLE supervisor_episode_memory (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id            UUID NOT NULL UNIQUE,          -- FK to cycle_briefings.cycle_id (soft ref)
    situation_summary   TEXT NOT NULL,                 -- LLM narrative (display only, never embedded)
    embed_input         TEXT NOT NULL,                 -- canonical deterministic string (what is embedded)
    patterns_detected   JSONB NOT NULL,                -- full structured pattern list
    pattern_types       TEXT[] NOT NULL DEFAULT '{}',  -- top-level array for overlap filter (no JSONB path needed)
    actions_taken       TEXT[] NOT NULL DEFAULT '{}',  -- recommended_actions from LLM
    severity            VARCHAR(20) NOT NULL,           -- normal | warning | critical
    city                TEXT[] NOT NULL DEFAULT '{}',  -- active cities this cycle (array overlap filter)
    outcome_1cycle      JSONB,                         -- null until grounded after +1 cycle
    outcome_3cycle      JSONB,                         -- null until grounded after +3 cycles
    embedding           vector(768) NOT NULL,           -- nomic-embed-text output (768 dims)
    embedding_status    VARCHAR(10) NOT NULL DEFAULT 'ok', -- 'ok' | 'failed' (failed = zero vector, excluded from retrieval)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── ZONE RECOMMENDATIONS ────────────────────────────────────
-- Zone Intelligence Agent writes per-rider recommendations
-- each cycle. Frontend map + Earnings Guardian read this.
CREATE TABLE zone_recommendations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rider_id            UUID NOT NULL REFERENCES riders(id),
    cycle_id            UUID NOT NULL,
    recommended_zone_ids UUID[] NOT NULL,               -- ordered by priority
    rationale           TEXT,                           -- LLM-generated explanation
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── ZONE STRESS SNAPSHOTS ───────────────────────────────────
-- One row per zone per cycle. Feeds the frontend map overlay.
CREATE TABLE zone_stress_snapshots (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    zone_id             UUID NOT NULL REFERENCES zones(id),
    cycle_id            UUID NOT NULL,
    stress_ratio        DOUBLE PRECISION NOT NULL,      -- >1.2 = stressed, <0.5 = dead
    density_score       DOUBLE PRECISION NOT NULL,
    is_dead_zone        BOOLEAN NOT NULL DEFAULT FALSE,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── DEAD ZONE SNAPSHOTS ─────────────────────────────────────
-- Dead Run Prevention Agent writes this each cycle.
CREATE TABLE dead_zone_snapshots (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    zone_id             UUID NOT NULL REFERENCES zones(id),
    cycle_id            UUID NOT NULL,
    risk_level          DOUBLE PRECISION NOT NULL,      -- 0.0–1.0
    expected_stranding_mins DOUBLE PRECISION,           -- expected wait if stranded
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── ORDER RISK SCORES ───────────────────────────────────────
-- Dead Run Prevention Agent scores every pending order.
CREATE TABLE order_risk_scores (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id            UUID NOT NULL REFERENCES orders(id),
    cycle_id            UUID NOT NULL,
    dead_zone_risk      DOUBLE PRECISION NOT NULL,      -- 0.0–1.0
    expected_cost_mins  DOUBLE PRECISION,               -- estimated earnings loss
    is_flagged          BOOLEAN NOT NULL DEFAULT FALSE,
    rationale           TEXT,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RESTAURANT RISK SCORES ──────────────────────────────────
-- Restaurant Intelligence Agent writes per active restaurant.
CREATE TABLE restaurant_risk_scores (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id),
    cycle_id            UUID NOT NULL,
    delay_risk_score    DOUBLE PRECISION NOT NULL,      -- 0.0–1.0 from ML model
    expected_delay_mins DOUBLE PRECISION,
    confidence          DOUBLE PRECISION,
    key_factors_json    JSONB,                          -- top contributing features
    explanation         TEXT,                           -- LLM-generated explanation
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RIDER HEALTH SNAPSHOTS ──────────────────────────────────
-- Earnings Guardian writes per active rider each cycle.
-- Frontend rider panel reads latest per rider.
CREATE TABLE rider_health_snapshots (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rider_id            UUID NOT NULL REFERENCES riders(id),
    cycle_id            UUID NOT NULL,
    health_score        DOUBLE PRECISION NOT NULL,      -- 0–100
    current_eph         DOUBLE PRECISION,
    projected_eph       DOUBLE PRECISION,               -- ML model forecast
    persona_threshold   DOUBLE PRECISION,               -- target EPH for this persona
    below_threshold     BOOLEAN NOT NULL DEFAULT FALSE,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RIDER ALERTS ────────────────────────────────────────────
-- All agent-generated alerts. Frontend alerts feed reads this.
CREATE TABLE rider_alerts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rider_id            UUID NOT NULL REFERENCES riders(id),
    cycle_id            UUID,
    alert_type          VARCHAR(50) NOT NULL,
    -- 'restaurant_delay' | 'dead_zone_risk' | 'earnings_below_threshold'
    -- 'long_distance_warning' | 'churn_risk'
    message             TEXT NOT NULL,                  -- human-readable, rider-facing
    severity            VARCHAR(20) NOT NULL DEFAULT 'medium', -- 'low' | 'medium' | 'high' | 'critical'
    metadata_json       JSONB,                          -- e.g. {restaurant_id, risk_score, delay_mins}
    is_resolved         BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RIDER CHURN SIGNALS ─────────────────────────────────────
-- Earnings Guardian escalates multi-session patterns here.
-- Supervisor includes these in high-priority briefing section.
CREATE TABLE rider_churn_signals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rider_id            UUID NOT NULL REFERENCES riders(id),
    cycle_id            UUID NOT NULL,
    signal_strength     DOUBLE PRECISION NOT NULL,      -- 0.0–1.0
    consecutive_bad_sessions INTEGER NOT NULL DEFAULT 0,
    avg_eph_last_n      DOUBLE PRECISION,               -- avg EPH over lookback window
    trigger_reason      TEXT,                           -- LLM-generated explanation
    is_escalated        BOOLEAN NOT NULL DEFAULT FALSE, -- sent to supervisor
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── OPERATOR ALERTS ─────────────────────────────────────────
-- System-level alerts for the operations/dispatcher team.
-- Distinct from rider_alerts (which are rider-facing).
-- Written by agents for things the ops team needs to act on:
--   restaurant agent  → 'restaurant_high_risk'
--   zone agent        → 'dead_zone_cluster'
--   earnings agent    → 'churn_surge'
-- Frontend shows these in a dedicated Operations Center panel.
-- MCP tool: GET /tools/operator-alerts
CREATE TABLE operator_alerts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id            UUID,
    agent_name          VARCHAR(50) NOT NULL,
    alert_type          VARCHAR(50) NOT NULL,
    -- 'restaurant_high_risk' | 'dead_zone_cluster' | 'churn_surge' | 'zone_stressed'
    severity            VARCHAR(20) NOT NULL DEFAULT 'medium',
    -- 'low' | 'medium' | 'high' | 'critical'
    title               TEXT NOT NULL,                  -- short headline for ops panel
    message             TEXT NOT NULL,                  -- full operator-facing message
    metadata_json       JSONB,                          -- z_score, restaurant_id, zone_ids, etc.
    is_resolved         BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_operator_alerts_unresolved ON operator_alerts(is_resolved, created_at DESC) WHERE is_resolved = FALSE;
CREATE INDEX idx_operator_alerts_cycle ON operator_alerts(cycle_id);
CREATE INDEX idx_operator_alerts_type ON operator_alerts(alert_type, created_at DESC);

-- ── RIDER INTERVENTIONS ─────────────────────────────────────
-- Specific actionable recommendations per at-risk rider.
CREATE TABLE rider_interventions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rider_id            UUID NOT NULL REFERENCES riders(id),
    cycle_id            UUID NOT NULL,
    recommendation_text TEXT NOT NULL,                  -- LLM-generated, specific action
    recommended_zone_id UUID REFERENCES zones(id),      -- move to this zone
    priority            VARCHAR(20) NOT NULL DEFAULT 'medium',
    was_acted_on        BOOLEAN,                        -- NULL = unknown (no feedback loop yet)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- SECTION 5 — OBSERVABILITY TABLE
-- Every MCP tool call + agent reasoning step logged here.
-- ============================================================

CREATE TABLE observability_logs (
    id                  UUID NOT NULL DEFAULT uuid_generate_v4(),
    service             VARCHAR(50) NOT NULL,           -- 'mcp_server' | 'ml_server' | 'agent'
    agent_name          VARCHAR(50),
    tool_name           VARCHAR(100),
    cycle_id            UUID,
    input_json          JSONB,
    output_json         JSONB,
    duration_ms         INTEGER,
    status              VARCHAR(20) NOT NULL DEFAULT 'success',
    error_message       TEXT,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('observability_logs', 'timestamp');

-- ============================================================
-- SECTION 6 — INDEXES
-- Every query pattern we identified in data mapping gets an index.
-- ============================================================

-- zones
CREATE INDEX idx_zones_city ON zones(city);

-- restaurants
CREATE INDEX idx_restaurants_zone ON restaurants(zone_id);
CREATE INDEX idx_restaurants_active ON restaurants(is_active) WHERE is_active = TRUE;

-- riders
CREATE INDEX idx_riders_home_zone ON riders(home_zone_id);
CREATE INDEX idx_riders_persona ON riders(persona_type);
CREATE INDEX idx_riders_active ON riders(is_active) WHERE is_active = TRUE;

-- orders — most queried table
CREATE INDEX idx_orders_rider ON orders(rider_id);
CREATE INDEX idx_orders_restaurant ON orders(restaurant_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_pickup_zone ON orders(pickup_zone_id);
CREATE INDEX idx_orders_delivery_zone ON orders(delivery_zone_id);
CREATE INDEX idx_orders_created ON orders(created_at DESC);
-- composite: agent queries pending orders per zone
CREATE INDEX idx_orders_status_delivery_zone ON orders(status, delivery_zone_id);

-- rider_sessions
CREATE INDEX idx_sessions_rider ON rider_sessions(rider_id);
CREATE INDEX idx_sessions_date ON rider_sessions(session_date DESC);
CREATE INDEX idx_sessions_below_threshold ON rider_sessions(below_threshold) WHERE below_threshold = TRUE;

-- rider_location_updates — latest-per-rider is the key query
CREATE INDEX idx_location_rider_time ON rider_location_updates(rider_id, timestamp DESC);

-- zone_density_snapshots — two access patterns
-- 1. current density: WHERE zone_id=X ORDER BY timestamp DESC LIMIT 1
-- 2. historical baseline: WHERE zone_id=X AND timestamp > now()-'28 days'
CREATE INDEX idx_density_zone_time ON zone_density_snapshots(zone_id, timestamp DESC);

-- restaurant_delay_events — ML feature queries filter by restaurant + hour + day
CREATE INDEX idx_delay_restaurant ON restaurant_delay_events(restaurant_id);
CREATE INDEX idx_delay_restaurant_time ON restaurant_delay_events(restaurant_id, hour_of_day, day_of_week);

-- agent_memory — supervisor reads by cycle and agent name
CREATE INDEX idx_memory_cycle ON agent_memory(cycle_id);
CREATE INDEX idx_memory_agent_cycle ON agent_memory(agent_name, cycle_id);

-- cycle_briefings — frontend reads latest
CREATE INDEX idx_briefings_time ON cycle_briefings(timestamp DESC);

-- zone_stress_snapshots — frontend map reads latest per zone
CREATE INDEX idx_stress_zone_cycle ON zone_stress_snapshots(zone_id, timestamp DESC);

-- dead_zone_snapshots
CREATE INDEX idx_dead_zone_cycle ON dead_zone_snapshots(zone_id, timestamp DESC);

-- restaurant_risk_scores — frontend + agent read latest per restaurant
CREATE INDEX idx_restaurant_risk_time ON restaurant_risk_scores(restaurant_id, timestamp DESC);

-- rider_health_snapshots — frontend reads latest per rider
CREATE INDEX idx_health_rider_time ON rider_health_snapshots(rider_id, timestamp DESC);

-- rider_alerts — frontend reads unresolved, ordered by severity + time
CREATE INDEX idx_alerts_rider ON rider_alerts(rider_id);
CREATE INDEX idx_alerts_unresolved ON rider_alerts(is_resolved, created_at DESC) WHERE is_resolved = FALSE;

-- rider_churn_signals
CREATE INDEX idx_churn_rider ON rider_churn_signals(rider_id);
CREATE INDEX idx_churn_escalated ON rider_churn_signals(is_escalated) WHERE is_escalated = FALSE;

-- observability_logs — queried by service + time window
CREATE INDEX idx_obs_service_time ON observability_logs(service, timestamp DESC);
CREATE INDEX idx_obs_cycle ON observability_logs(cycle_id);

-- supervisor_episode_memory — Phase 2 RAG indexes
-- Recency + severity + outcome-grounded filter (B-tree)
CREATE INDEX idx_episode_time ON supervisor_episode_memory(created_at DESC);
CREATE INDEX idx_episode_severity ON supervisor_episode_memory(severity);
CREATE INDEX idx_episode_status ON supervisor_episode_memory(embedding_status);
-- GIN index on TEXT[] columns for fast && (array overlap) operator
CREATE INDEX idx_episode_pattern_types ON supervisor_episode_memory USING gin(pattern_types);
CREATE INDEX idx_episode_city ON supervisor_episode_memory USING gin(city);
-- HNSW index for approximate nearest-neighbour vector search (pgvector >= 0.5.0)
-- m=16 ef_construction=64 are pgvector defaults — good for <10k episodes
CREATE INDEX idx_episode_embedding ON supervisor_episode_memory
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ── DOCS CHUNKS (chatbot RAG) ─────────────────────────────────
-- Chunked ARIA_DOCS.md with nomic-embed-text embeddings (768-dim).
-- Populated by POST /docs-chat/ingest on the MCP server.
-- No HNSW index needed — <300 rows, sequential scan is instant.
CREATE TABLE IF NOT EXISTS docs_chunks (
    id         SERIAL PRIMARY KEY,
    section    TEXT NOT NULL,
    level      INT  NOT NULL,
    content    TEXT NOT NULL,
    embedding  vector(768),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- SECTION 7 — CONTINUOUS AGGREGATES (TimescaleDB)
-- Pre-computed hourly rollups for historical baseline queries.
-- Zone Intelligence Agent uses these for baseline comparison.
-- ============================================================

-- Hourly zone density average — used as baseline for stress ratio computation
CREATE MATERIALIZED VIEW zone_density_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', timestamp) AS bucket,
    zone_id,
    AVG(density_score)  AS avg_density,
    MAX(density_score)  AS max_density,
    AVG(order_count)    AS avg_order_count
FROM zone_density_snapshots
GROUP BY bucket, zone_id;

-- Hourly restaurant delay average — used as baseline for delay deviation
CREATE MATERIALIZED VIEW restaurant_delay_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', timestamp) AS bucket,
    restaurant_id,
    hour_of_day,
    day_of_week,
    AVG(delay_mins)     AS avg_delay,
    STDDEV(delay_mins)  AS std_delay,
    COUNT(*)            AS sample_count
FROM restaurant_delay_events
GROUP BY bucket, restaurant_id, hour_of_day, day_of_week;

-- ============================================================
-- END OF SCHEMA
-- ============================================================
