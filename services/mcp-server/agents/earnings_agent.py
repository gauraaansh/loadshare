"""
ARIA — Earnings Guardian Agent
================================
LangGraph-powered agent. Runs every 15-minute cycle.

Responsibility:
  Score all active riders by EPH trajectory, detect multi-session churn
  patterns before they materialise as app uninstalls, and surface the
  earnings shortfall to the Supervisor as a headline business metric.

  Grounded directly in the Loadshare 2023 research problem:
    Platform EPH:       Rs.70–85/hr
    Rider expectation:  Rs.90–100/hr (persona-specific targets)
    Retention crisis:   30% at peak churn
  ARIA's job is to catch the trajectory early and give ops a specific,
  cross-agent-informed intervention before the rider churns.

Pipeline (5 nodes):
  fetch_riders → score_riders → write_health_snapshots → create_alerts → synthesize

Key design decisions:

  Zero per-rider DB queries:
    compute_current_eph() and compute_churn_signal() both make per-rider
    DB calls — not used here. All logic replicated inline from 3 bulk
    queries + 1 Redis pipeline fetched in Node 1.

  Minimum observation window (MIN_OBS_MINS = 20):
    Early-session EPH is noisy — first order could be long-distance or
    a dead run. Intervention alerts (earnings_below_threshold, churn_risk)
    are gated behind 20 min. Health snapshots are always written regardless.
    Reduces false positives, makes demo output credible.

  Lag EPH from rider_health_snapshots (intra-session time windows):
    Model 4 needs eph_lag1_30min / eph_lag2_60min / eph_lag3_90min.
    At 15-min cycle cadence, the last 3 rider_health_snapshots represent
    EPH at ~15/30/45 min ago — good enough approximation (model cares
    about trajectory shape, not exact minute label).
    Staleness: if most recent snapshot > 30 min old (missed a cycle),
    pad all lags with current_eph (stable prior, slope = 0). Downgrade
    alert severity if stale. Never suppress snapshots — only alerts.

  Shortfall capped to actionable window (INTERVENTION_HORIZON_HRS = 2):
    Projecting shortfall over full remaining shift (up to 7 hrs) gives
    inflated numbers ops can't act on. Cap at 2 hours — the realistic
    intervention horizon. Supervisor KPI total_earnings_shortfall_rs
    becomes an actionable number, not a worst-case theoretical.

  Cross-agent context (churn-risk riders only):
    For the small subset of churn-risk riders, two IN queries read the
    most recent zone_recommendations (Zone agent) and order_risk_scores
    (Dead Run agent). Enriches intervention templates — agents are
    visibly aware of each other's actions. Reads most recent records,
    not current cycle (current cycle hasn't completed all agents yet).

  Churn escalation tiers:
    rider_churn_signals — written every cycle for all is_churn_risk riders.
    rider_alerts (churn_risk) — only when consecutive_bad >= CHURN_SIGNAL_SESSIONS.
    Severity: warning (signal 0.5–0.7) / high (signal > 0.7).
    Cooldown: 2 hours (churn is a multi-session signal, no cycle spam).

  Recovery detection:
    Previous cycle health_score available from lag snapshot query.
    at_risk/critical → watch/healthy = recovery. Writes low-severity
    rider_alert type earnings_recovery. Shows closed-loop impact in demo.

  Shortfall aggregation excludes ML failures:
    ML-failed riders get current_eph as projected_eph. Their shortfall
    is computed from current (not projected) — still included in fleet
    total. status="partial" if >50% of riders had ML failures.

  Quality KPIs in output:
    ml_failures, stale_lag_count, alerts_suppressed_by_cooldown,
    new_vs_repeat_at_risk — exposed as top-level output fields for
    Supervisor and demo/interview context.
"""

import asyncio
import json
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, TypedDict

import httpx
import structlog
from langgraph.graph import END, StateGraph

from agents.base import BaseAgent
from algorithms.session import (
    CHURN_SIGNAL_SESSIONS,
    EPH_TARGET_DEDICATED,
    EPH_TARGET_SUPPLEMENTARY,
    HEALTH_SCORE_THRESHOLD,
    compute_session_health_score,
)
from llm import call_llm
from ml_client import predict_earnings_trajectory
from config import EVENT_STREAM_HOST
from redis_client import key_zone_density

log = structlog.get_logger()

# ── Tuning constants ────────────────────────────────────────────
_MIN_OBS_MINS             = 20.0   # min session elapsed before intervention alerts
_TOTAL_SHIFT_MINS         = 480.0  # 8-hour default (no shift_hours column in riders table)
_INTERVENTION_HORIZON_HRS = 2.0    # shortfall capped to next 2 hours (actionable window)
_CHURN_SURGE_THRESHOLD    = 0.15   # fraction of active riders churn-risk → operator alert
_EARNINGS_COOLDOWN_MINS   = 30     # earnings_below_threshold alert cooldown
_CHURN_COOLDOWN_MINS      = 120    # churn_risk alert cooldown (2 hours, multi-session signal)
_LAG_STALE_MINS           = 30     # health snapshot age threshold before lags are considered stale
_TOP_AT_RISK_TO_LLM       = 3      # top N at-risk riders shown to LLM (anonymized)


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _persona_target(persona_type: str | None) -> tuple[float, int]:
    """Return (eph_target, persona_enc). NULL persona → supplementary (safer, lower target)."""
    if persona_type == "dedicated":
        return EPH_TARGET_DEDICATED, 1
    return EPH_TARGET_SUPPLEMENTARY, 0


def _health_classification(health_score: float) -> str:
    if health_score >= 75:
        return "healthy"
    if health_score >= 50:
        return "watch"
    if health_score >= HEALTH_SCORE_THRESHOLD:  # 40
        return "at_risk"
    return "critical"


def _compute_churn_inline(sessions: list[dict]) -> dict:
    """
    Replicate compute_churn_signal() logic without a DB call.
    sessions: completed session dicts (eph, below_threshold, health_score),
              most-recent first. Pre-fetched in bulk in Node 1.
    """
    if not sessions:
        return {
            "signal_strength":          0.0,
            "consecutive_bad_sessions": 0,
            "avg_eph_last_n":           None,
            "sessions_sampled":         0,
            "is_churn_risk":            False,
            "trigger_details":          [],
        }

    n = len(sessions)

    consecutive_bad = 0
    for s in sessions:
        if s.get("below_threshold"):
            consecutive_bad += 1
        else:
            break

    ephs    = [float(s["eph"]) for s in sessions if s.get("eph") is not None]
    avg_eph = sum(ephs) / len(ephs) if ephs else None

    trend_penalty = 0.0
    if len(ephs) >= 3:
        recent_avg  = sum(ephs[:2]) / 2
        older_slice = ephs[2:4]
        older_avg   = sum(older_slice) / len(older_slice)
        if older_avg > 0:
            trend_penalty = min(1.0, max(0.0, (older_avg - recent_avg) / older_avg))

    consec_score = min(1.0, consecutive_bad / max(CHURN_SIGNAL_SESSIONS, 1))
    eph_deficit  = (
        max(0.0, EPH_TARGET_SUPPLEMENTARY - avg_eph) / EPH_TARGET_SUPPLEMENTARY
        if avg_eph is not None else 0.5
    )
    signal_strength = round(
        min(1.0, 0.4 * consec_score + 0.4 * eph_deficit + 0.2 * trend_penalty), 4
    )
    is_churn_risk = signal_strength >= 0.5 or consecutive_bad >= CHURN_SIGNAL_SESSIONS

    trigger_details = []
    if consecutive_bad >= 2:
        trigger_details.append(
            f"{consecutive_bad} consecutive sessions below EPH threshold"
        )
    if avg_eph is not None and avg_eph < EPH_TARGET_SUPPLEMENTARY:
        trigger_details.append(
            f"avg EPH Rs.{avg_eph:.0f}/hr vs Rs.{EPH_TARGET_SUPPLEMENTARY:.0f} target"
        )
    if trend_penalty > 0.2:
        trigger_details.append("EPH declining across recent sessions")

    return {
        "signal_strength":          signal_strength,
        "consecutive_bad_sessions": consecutive_bad,
        "avg_eph_last_n":           round(avg_eph, 2) if avg_eph is not None else None,
        "sessions_sampled":         n,
        "is_churn_risk":            is_churn_risk,
        "trigger_details":          trigger_details,
    }


def _build_intervention_text(
    rider_data: dict,
    scoring:    dict,
    churn:      dict,
    cross:      dict,
) -> str:
    """
    Template intervention text with parameterized action slots.

    Slots filled from:
      cross["zone_rationale"]  — Zone agent's recommendation text (previous cycle)
      cross["dead_run_flag"]   — Dead Run agent flagged this rider's order (previous cycle)
      scoring                  — current EPH, projected EPH, shortfall, target
      churn                    — consecutive bad sessions count
    """
    current_eph = scoring["current_eph"]
    projected   = scoring["projected_eph"]
    target      = scoring["eph_target"]
    shortfall   = scoring["shortfall_rs"]
    consecutive = churn["consecutive_bad_sessions"]

    parts = []

    # Dead run hint (from Dead Run agent — cross-agent slot)
    if cross.get("dead_run_flag"):
        parts.append(
            "Dead Run agent flagged a high-risk zone assignment in your recent orders — "
            "request low-risk zones from the dispatcher."
        )

    # Zone repositioning hint (from Zone agent — cross-agent slot)
    if cross.get("zone_rationale"):
        parts.append(cross["zone_rationale"])

    # EPH trajectory slot
    if projected < target:
        parts.append(
            f"Projected EPH: Rs.{projected:.0f}/hr vs Rs.{target:.0f}/hr target "
            f"(Rs.{shortfall:.0f} shortfall over next 2 hrs). "
            "Focus on peak-zone orders during the next 2 hours to close the gap."
        )

    # Multi-session streak slot
    if consecutive >= 2:
        parts.append(
            f"{consecutive} consecutive sessions below EPH target. "
            "Consider adjusting your shift window to higher-demand periods "
            "(12pm–2pm or 7pm–9pm)."
        )

    return " ".join(parts) if parts else (
        f"Your EPH is Rs.{current_eph:.0f}/hr. "
        "Stay in high-demand zones and minimise idle time to improve trajectory."
    )


# ══════════════════════════════════════════════════════════════
# State schema
# ══════════════════════════════════════════════════════════════

class EarningsState(TypedDict, total=False):
    cycle_id: str
    now:      datetime

    # Node 1 outputs
    riders_dict:       dict   # rider_id → session + rider data
    lag_eph_dict:      dict   # rider_id → [{current_eph, health_score, below_threshold, timestamp}, ...]
    churn_history:     dict   # rider_id → [{eph, below_threshold, health_score}, ...]  most-recent first
    zone_density_dict: dict   # zone_id  → density_score

    # Node 2 outputs
    scored_riders:       list  # full scoring dict per rider
    churn_risk_ids:      list  # rider_ids with is_churn_risk=True
    ml_failures:         int
    stale_lag_count:     int
    new_vs_repeat_at_risk: int  # count of newly at-risk riders (were watch/healthy last cycle)

    # Node 3 outputs
    snapshots_written: int

    # Node 4 outputs
    churn_signals_written:         int
    interventions_written:         int
    earnings_alerts_written:       int
    churn_alerts_written:          int
    recovery_count:                int
    alerts_suppressed_by_cooldown: int
    operator_alerts_written:       int

    # Node 5 outputs
    llm_narrative:               str
    summary_text:                str
    severity:                    str
    status:                      str
    # Supervisor headline KPIs
    at_risk_count:               int
    churn_risk_count:            int
    avg_eph:                     float
    total_earnings_shortfall_rs: float
    riders_intervened:           int


# ══════════════════════════════════════════════════════════════
# Node 1 — fetch_riders
# ══════════════════════════════════════════════════════════════

async def _fetch_riders(state: EarningsState, conn, redis) -> EarningsState:
    """
    3 DB queries + 1 Redis pipeline. Zero per-rider queries.

    Query 1 (active sessions):
      JOIN rider_sessions + riders for all sessions open today.
      Source of truth for "who is actively earning right now."
      No Redis SMEMBERS needed — we care about session data (DB), not
      login status (Redis). Riders logged in but without an open session
      are irrelevant for EPH scoring.

    Query 2 (lag EPH):
      Last 3 rider_health_snapshots per active rider within 2 hours.
      Grouped in Python (first 3 per rider, most-recent first).
      Used for: eph_lag1/2/3 Model 4 inputs + previous classification
      for recovery detection + new_vs_repeat_at_risk.

    Query 3 (churn history):
      Last CHURN_SIGNAL_SESSIONS completed sessions per active rider.
      Provides: eph, below_threshold, health_score for inline churn signal.
      Grouped in Python (first CHURN_SIGNAL_SESSIONS per rider).

    Redis pipeline:
      HGET aria:zone_density:{home_zone_id} density_score
      for each unique home zone. Fallback to 0.5 if not cached.
    """
    # Use sim time for hours_elapsed so EPH is not distorted by TIME_SCALE.
    # Fallback to real UTC if event-stream is unreachable.
    try:
        async with httpx.AsyncClient(timeout=2.0) as _hc:
            _r = await _hc.get(f"{EVENT_STREAM_HOST}/simulation/status")
            _sim_time_str = _r.json().get("sim_time")
            now = datetime.fromisoformat(_sim_time_str) if _sim_time_str else datetime.now(timezone.utc)
    except Exception:
        now = datetime.now(timezone.utc)

    # ── Query 1: active sessions ────────────────────────────────
    # Use DISTINCT ON to get the most recent open session per rider.
    # session_date = CURRENT_DATE is intentionally omitted: with a high TIME_SCALE
    # the simulation advances past the real calendar date, so sessions carry a sim date
    # that differs from PostgreSQL's CURRENT_DATE. shift_end IS NULL reliably identifies
    # active sessions regardless of sim date.
    session_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (r.id)
            r.id               AS rider_id,
            r.persona_type,
            r.home_zone_id,
            rs.id              AS session_id,
            rs.shift_start,
            rs.total_earnings,
            rs.total_orders,
            rs.idle_time_mins,
            rs.dead_runs_count,
            rs.long_distance_count
        FROM rider_sessions rs
        JOIN riders r ON r.id = rs.rider_id
        WHERE rs.shift_end IS NULL
        ORDER BY r.id, rs.shift_start DESC
        """,
    )

    riders_dict: dict[str, dict] = {}
    for row in session_rows:
        rid = str(row["rider_id"])
        riders_dict[rid] = {
            "rider_id":            rid,
            "persona_type":        row["persona_type"],
            "home_zone_id":        str(row["home_zone_id"]),
            "session_id":          str(row["session_id"]),
            "shift_start":         row["shift_start"],
            "total_earnings":      float(row["total_earnings"]),
            "total_orders":        int(row["total_orders"]),
            "idle_time_mins":      float(row["idle_time_mins"]),
            "dead_runs_count":     int(row["dead_runs_count"]),
            "long_distance_count": int(row["long_distance_count"]),
        }

    if not riders_dict:
        log.info("earnings_no_active_riders")
        return {
            **state,
            "now":              now,
            "riders_dict":      {},
            "lag_eph_dict":     {},
            "churn_history":    {},
            "zone_density_dict": {},
        }

    rider_ids    = list(riders_dict.keys())
    rider_uuids  = [uuid.UUID(rid) for rid in rider_ids]

    # ── Query 2: lag EPH from rider_health_snapshots ────────────
    lag_rows = await conn.fetch(
        """
        SELECT rider_id, current_eph, health_score, below_threshold, timestamp
        FROM rider_health_snapshots
        WHERE rider_id = ANY($1)
          AND timestamp > NOW() - INTERVAL '2 hours'
        ORDER BY rider_id, timestamp DESC
        """,
        rider_uuids,
    )

    lag_eph_dict: dict[str, list] = defaultdict(list)
    for row in lag_rows:
        rid = str(row["rider_id"])
        if len(lag_eph_dict[rid]) < 3:
            lag_eph_dict[rid].append({
                "current_eph":    float(row["current_eph"])  if row["current_eph"]  is not None else None,
                "health_score":   float(row["health_score"]) if row["health_score"] is not None else None,
                "below_threshold": bool(row["below_threshold"]),
                "timestamp":      row["timestamp"],
            })

    # ── Query 3: churn history (completed sessions) ─────────────
    churn_rows = await conn.fetch(
        """
        SELECT rider_id, eph, below_threshold, health_score
        FROM rider_sessions
        WHERE rider_id = ANY($1)
          AND shift_end IS NOT NULL
        ORDER BY rider_id, session_date DESC
        """,
        rider_uuids,
    )

    churn_history: dict[str, list] = defaultdict(list)
    for row in churn_rows:
        rid = str(row["rider_id"])
        if len(churn_history[rid]) < CHURN_SIGNAL_SESSIONS:
            churn_history[rid].append({
                "eph":             float(row["eph"])          if row["eph"]          is not None else None,
                "below_threshold": bool(row["below_threshold"]),
                "health_score":    float(row["health_score"]) if row["health_score"] is not None else None,
            })

    # ── Redis pipeline: zone density for each unique home zone ──
    unique_zone_ids = list({r["home_zone_id"] for r in riders_dict.values()})
    zone_density_dict: dict[str, float] = {}

    if unique_zone_ids:
        async with redis.pipeline(transaction=False) as pipe:
            for zid in unique_zone_ids:
                pipe.hget(key_zone_density(zid), "density_score")
            results = await pipe.execute()

        for zid, val in zip(unique_zone_ids, results):
            zone_density_dict[zid] = float(val) if val is not None else 0.5

    log.info(
        "earnings_fetch_done",
        active_riders=len(riders_dict),
        with_lag_data=len(lag_eph_dict),
        with_churn_history=len(churn_history),
        unique_zones=len(unique_zone_ids),
    )

    return {
        **state,
        "now":               now,
        "riders_dict":       riders_dict,
        "lag_eph_dict":      dict(lag_eph_dict),
        "churn_history":     dict(churn_history),
        "zone_density_dict": zone_density_dict,
    }


# ══════════════════════════════════════════════════════════════
# Node 2 — score_riders
# ══════════════════════════════════════════════════════════════

async def _score_riders(state: EarningsState) -> EarningsState:
    """
    Pure computation + concurrent ML calls. Zero I/O except ML server.

    Per rider (all concurrent via asyncio.gather + Semaphore(20)):
      1. Compute current_eph inline from session fields (no DB call).
      2. Resolve lag EPH from health snapshots — stale check (> 30 min).
         Stale → pad all lags with current_eph (stable prior, slope = 0).
         Downgrade: stale_lags=True propagates to alert severity in Node 4.
      3. Detect previous cycle's classification for recovery detection
         and new_vs_repeat_at_risk tracking.
      4. Assemble EarningsRequest and call predict_earnings_trajectory.
         ML failure → current_eph as projected_eph, alert_level="watch"
         if below_threshold, status="partial" if >50% fail.
      5. compute_session_health_score() — pure function, no DB.
      6. _compute_churn_inline() — replicates compute_churn_signal()
         without DB call using pre-fetched churn_history.
      7. Shortfall: max(0, (eph_target - projected_eph) × min(remaining_hrs, 2.0))
    """
    riders_dict       = state.get("riders_dict", {})
    lag_eph_dict      = state.get("lag_eph_dict", {})
    churn_history     = state.get("churn_history", {})
    zone_density_dict = state.get("zone_density_dict", {})
    now               = state["now"]

    if not riders_dict:
        return {
            **state,
            "scored_riders":        [],
            "churn_risk_ids":       [],
            "ml_failures":          0,
            "stale_lag_count":      0,
            "new_vs_repeat_at_risk": 0,
        }

    sem = asyncio.Semaphore(20)  # matches httpx max_connections=20

    async def score_one(rider_data: dict) -> dict:
        rid = rider_data["rider_id"]
        eph_target, persona_enc = _persona_target(rider_data.get("persona_type"))

        # Current EPH — inline, no DB call
        shift_start   = rider_data["shift_start"]
        if shift_start.tzinfo is None:
            shift_start = shift_start.replace(tzinfo=timezone.utc)
        hours_elapsed  = max((now - shift_start).total_seconds() / 3600, 1 / 60)
        obs_point_mins = hours_elapsed * 60
        current_eph    = rider_data["total_earnings"] / hours_elapsed

        # Lag EPH — staleness check
        lags       = lag_eph_dict.get(rid, [])
        stale_lags = False
        if lags:
            most_recent_ts = lags[0]["timestamp"]
            if most_recent_ts.tzinfo is None:
                most_recent_ts = most_recent_ts.replace(tzinfo=timezone.utc)
            if (now - most_recent_ts).total_seconds() / 60 > _LAG_STALE_MINS:
                stale_lags = True
                lags = []  # fall back to stable-trajectory padding

        def get_lag_eph(idx: int) -> float:
            """Return lag EPH at position idx, or current_eph if not available (stable prior)."""
            if idx < len(lags) and lags[idx]["current_eph"] is not None:
                return float(lags[idx]["current_eph"])
            return current_eph

        eph_lag1 = get_lag_eph(0)
        eph_lag2 = get_lag_eph(1)
        eph_lag3 = get_lag_eph(2)

        # Previous cycle classification — for recovery detection + new_vs_repeat
        prev_classification = None
        if lags and lags[0].get("health_score") is not None:
            prev_classification = _health_classification(lags[0]["health_score"])

        # Zone density — from Redis pipeline, fallback 0.5
        zone_density = zone_density_dict.get(rider_data["home_zone_id"], 0.5)

        # ML inputs
        time_remaining_mins = max(0.0, _TOTAL_SHIFT_MINS - obs_point_mins)
        ml_inputs = {
            "persona_enc":         persona_enc,
            "hour_of_day":         now.hour,
            "orders_completed":    rider_data["total_orders"],
            "earnings_so_far":     rider_data["total_earnings"],
            "current_eph":         round(current_eph, 2),
            "idle_time_mins":      rider_data["idle_time_mins"],
            "dead_runs_count":     rider_data["dead_runs_count"],
            "zone_density":        zone_density,
            "obs_point_mins":      round(obs_point_mins, 1),
            "time_remaining_mins": round(time_remaining_mins, 1),
            "total_shift_mins":    _TOTAL_SHIFT_MINS,
            "eph_lag1_30min":      round(eph_lag1, 2),
            "eph_lag2_60min":      round(eph_lag2, 2),
            "eph_lag3_90min":      round(eph_lag3, 2),
        }

        # ML call
        ml_failed = False
        async with sem:
            ml_result = await predict_earnings_trajectory(ml_inputs)

        if ml_result is None:
            ml_failed       = True
            projected_eph   = current_eph
            below_threshold = current_eph < eph_target
            alert_level     = "watch" if below_threshold else "none"
            eph_trend       = "unknown"
        else:
            projected_eph   = float(ml_result["projected_final_eph"])
            below_threshold = bool(ml_result["below_threshold"])
            alert_level     = ml_result["alert_level"]
            eph_trend       = ml_result["eph_trend"]

        # Health score — pure function (no DB)
        health = compute_session_health_score(
            current_eph=current_eph,
            projected_eph=projected_eph,
            dead_runs_count=rider_data["dead_runs_count"],
            idle_time_mins=rider_data["idle_time_mins"],
            hours_elapsed=hours_elapsed,
            persona_type=rider_data.get("persona_type") or "supplementary",
        )
        health_score   = health["health_score"]
        classification = health["classification"]

        # Shortfall — capped to actionable window
        time_remaining_hrs = time_remaining_mins / 60
        shortfall_rs = max(
            0.0,
            (eph_target - projected_eph) * min(time_remaining_hrs, _INTERVENTION_HORIZON_HRS),
        )

        # Churn signal — inline, no DB call
        churn = _compute_churn_inline(churn_history.get(rid, []))

        # Recovery: was at_risk/critical, now watch/healthy
        is_recovery = (
            prev_classification in ("at_risk", "critical")
            and classification in ("watch", "healthy")
        )

        # New vs repeat at-risk (was not at-risk last cycle)
        is_new_at_risk = (
            classification in ("at_risk", "critical")
            and prev_classification not in ("at_risk", "critical")
        )

        return {
            "rider_id":          rid,
            "persona_type":      rider_data.get("persona_type") or "supplementary",
            "home_zone_id":      rider_data["home_zone_id"],
            "session_id":        rider_data["session_id"],
            "current_eph":       round(current_eph, 2),
            "projected_eph":     round(projected_eph, 2),
            "eph_target":        eph_target,
            "below_threshold":   below_threshold,
            "health_score":      health_score,
            "classification":    classification,
            "alert_level":       alert_level,
            "eph_trend":         eph_trend,
            "shortfall_rs":      round(shortfall_rs, 2),
            "obs_point_mins":    round(obs_point_mins, 1),
            "churn":             churn,
            "is_recovery":       is_recovery,
            "is_new_at_risk":    is_new_at_risk,
            "stale_lags":        stale_lags,
            "ml_failed":         ml_failed,
        }

    raw = await asyncio.gather(
        *[score_one(rd) for rd in riders_dict.values()],
        return_exceptions=True,
    )

    scored_riders:      list[dict] = []
    churn_risk_ids:     list[str]  = []
    ml_failures         = 0
    stale_lag_count     = 0
    new_vs_repeat_count = 0

    for r in raw:
        if isinstance(r, Exception):
            ml_failures += 1
            log.warning("earnings_score_exception", error=str(r))
            continue
        scored_riders.append(r)
        if r["ml_failed"]:
            ml_failures += 1
        if r["stale_lags"]:
            stale_lag_count += 1
        if r["churn"]["is_churn_risk"]:
            churn_risk_ids.append(r["rider_id"])
        if r["is_new_at_risk"]:
            new_vs_repeat_count += 1

    log.info(
        "earnings_score_done",
        total=len(scored_riders),
        at_risk=sum(1 for r in scored_riders if r["classification"] in ("at_risk", "critical")),
        churn_risk=len(churn_risk_ids),
        ml_failures=ml_failures,
        stale_lags=stale_lag_count,
    )

    return {
        **state,
        "scored_riders":        scored_riders,
        "churn_risk_ids":       churn_risk_ids,
        "ml_failures":          ml_failures,
        "stale_lag_count":      stale_lag_count,
        "new_vs_repeat_at_risk": new_vs_repeat_count,
    }


# ══════════════════════════════════════════════════════════════
# Node 3 — write_health_snapshots
# ══════════════════════════════════════════════════════════════

async def _write_health_snapshots(state: EarningsState, conn) -> EarningsState:
    """
    rider_health_snapshots: one row per active rider per cycle.
    executemany bulk insert — no per-rider DB calls.

    Written for ALL active riders regardless of:
      - observation window (MIN_OBS_MINS gate is for alerts only)
      - ML failure (current_eph used as projected_eph fallback)
      - stale lags (snapshot quality noted in output KPIs, not gated here)

    Frontend rider panel reads the latest row per rider.
    Also used as lag source for next cycle's eph_lag inputs.
    """
    scored_riders = state.get("scored_riders", [])
    cycle_id      = state["cycle_id"]

    if not scored_riders:
        return {**state, "snapshots_written": 0}

    rows = [
        (
            str(uuid.uuid4()),
            r["rider_id"],
            cycle_id,
            r["health_score"],
            r["current_eph"],
            r["projected_eph"],
            r["eph_target"],
            r["below_threshold"],
        )
        for r in scored_riders
    ]

    written = 0
    try:
        await conn.executemany(
            """
            INSERT INTO rider_health_snapshots
                (id, rider_id, cycle_id, health_score, current_eph,
                 projected_eph, persona_threshold, below_threshold, timestamp)
            VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, NOW())
            """,
            rows,
        )
        written = len(rows)
    except Exception as exc:
        log.warning("earnings_snapshots_write_failed", error=str(exc))

    log.info("earnings_snapshots_written", count=written)
    return {**state, "snapshots_written": written}


# ══════════════════════════════════════════════════════════════
# Node 4 — create_alerts
# ══════════════════════════════════════════════════════════════

async def _create_alerts(state: EarningsState, conn) -> EarningsState:
    """
    Alert logic. DB queries in this node:
      1. Bulk cooldown pre-fetch (one query for all active riders).
      2. Cross-agent context: zone_recommendations + order_risk_scores
         (two IN queries, churn-risk riders only — small subset).

    Per rider:
      - earnings_recovery alert  (no cooldown — positive, rare)
      - earnings_below_threshold (gated: obs >= 20min, alert_level in watch/intervene,
                                  stale_lags=False, 30-min cooldown)
      - rider_churn_signals      (every cycle for is_churn_risk=True)
      - churn_risk alert         (only if consecutive_bad >= CHURN_SIGNAL_SESSIONS,
                                  2-hr cooldown, two severity tiers)
      - rider_interventions      (for churn-risk riders with escalated alert,
                                  template text with cross-agent action slots)

    System-level:
      - churn_surge operator alert (if churn_risk_count / total >= 15%)
    """
    scored_riders     = state.get("scored_riders", [])
    churn_risk_ids    = state.get("churn_risk_ids", [])
    zone_density_dict = state.get("zone_density_dict", {})
    cycle_id          = state["cycle_id"]
    now               = state["now"]

    # Pre-sort zones by density — used to pick repositioning target for interventions
    best_zones_by_density = sorted(zone_density_dict.items(), key=lambda x: x[1], reverse=True)

    if not scored_riders:
        return {
            **state,
            "churn_signals_written":         0,
            "interventions_written":         0,
            "earnings_alerts_written":       0,
            "churn_alerts_written":          0,
            "recovery_count":                0,
            "alerts_suppressed_by_cooldown": 0,
            "operator_alerts_written":       0,
        }

    rider_uuids = [uuid.UUID(r["rider_id"]) for r in scored_riders]

    # ── Zone metadata for recommended zone selection ────────────
    # Fetch city + zone_type for all zones that have density data.
    # Used to: (a) filter same-city zones, (b) exclude dead zones, (c) prefer stressed zones.
    zone_meta: dict[str, dict] = {}
    if best_zones_by_density:
        zone_uuids_for_meta = [uuid.UUID(zid) for zid, _ in best_zones_by_density]
        zone_meta_rows = await conn.fetch(
            "SELECT id::text AS zone_id, city, name FROM zones "
            "WHERE id = ANY($1) AND is_active = TRUE",
            zone_uuids_for_meta,
        )
        for row in zone_meta_rows:
            zone_meta[row["zone_id"]] = {"city": row["city"], "name": row["name"]}

    # Pre-build: zone_id of rider's home zone → city (for same-city matching)
    # scored_riders has home_zone_id
    rider_home_city: dict[str, str] = {
        r["rider_id"]: zone_meta.get(r.get("home_zone_id", ""), {}).get("city", "")
        for r in scored_riders
    }

    # ── Bulk cooldown pre-fetch ─────────────────────────────────
    # One query for all active riders, both cooldown types.
    # Build sets in Python — O(1) membership lookup per rider.
    recent_alert_rows = await conn.fetch(
        """
        SELECT rider_id, alert_type, created_at
        FROM rider_alerts
        WHERE rider_id = ANY($1)
          AND alert_type IN ('earnings_below_threshold', 'churn_risk')
          AND is_resolved = FALSE
          AND created_at > NOW() - INTERVAL '2 hours'
        """,
        rider_uuids,
    )

    earnings_cooldown: set[str] = set()
    churn_cooldown:    set[str] = set()
    for row in recent_alert_rows:
        rid = str(row["rider_id"])
        ts  = row["created_at"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_mins = (now - ts).total_seconds() / 60
        if row["alert_type"] == "earnings_below_threshold" and age_mins < _EARNINGS_COOLDOWN_MINS:
            earnings_cooldown.add(rid)
        elif row["alert_type"] == "churn_risk" and age_mins < _CHURN_COOLDOWN_MINS:
            churn_cooldown.add(rid)

    # ── Cross-agent context (churn-risk riders only) ────────────
    # Reads most-recent records — not scoped to current cycle_id because
    # Zone and Dead Run agents may not have run yet this cycle.
    cross_agent_ctx: dict[str, dict] = {}
    if churn_risk_ids:
        churn_uuids = [uuid.UUID(rid) for rid in churn_risk_ids]

        zone_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (rider_id)
                rider_id, rationale, recommended_zone_ids
            FROM zone_recommendations
            WHERE rider_id = ANY($1)
            ORDER BY rider_id, timestamp DESC
            """,
            churn_uuids,
        )
        dr_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (o.rider_id)
                o.rider_id,
                ors.is_flagged
            FROM order_risk_scores ors
            JOIN orders o ON o.id = ors.order_id
            WHERE o.rider_id = ANY($1)
            ORDER BY o.rider_id, o.created_at DESC
            """,
            churn_uuids,
        )

        for row in zone_rows:
            rid = str(row["rider_id"])
            cross_agent_ctx.setdefault(rid, {})["zone_rationale"] = row["rationale"]
            rec_ids = row["recommended_zone_ids"]
            if rec_ids:
                cross_agent_ctx[rid]["zone_rec_id"] = str(rec_ids[0])
        for row in dr_rows:
            rid = str(row["rider_id"])
            cross_agent_ctx.setdefault(rid, {})["dead_run_flag"] = bool(row["is_flagged"])

    # ── Per-rider alert writes ──────────────────────────────────
    earnings_alerts = 0
    churn_alerts    = 0
    recovery_count  = 0
    suppressed      = 0
    churn_signals   = 0
    interventions   = 0

    for r in scored_riders:
        rid   = r["rider_id"]
        churn = r["churn"]

        # Recovery alert — no cooldown (positive event, low-severity, rare)
        if r["is_recovery"]:
            try:
                await conn.execute(
                    """
                    INSERT INTO rider_alerts
                        (id, rider_id, cycle_id, alert_type, message,
                         severity, metadata_json, is_resolved, created_at)
                    VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, FALSE, NOW())
                    """,
                    str(uuid.uuid4()), rid, cycle_id,
                    "earnings_recovery",
                    (
                        f"Your earnings trajectory has recovered! "
                        f"Current EPH: Rs.{r['current_eph']:.0f}/hr → "
                        f"projected Rs.{r['projected_eph']:.0f}/hr. Keep it up."
                    ),
                    "low",
                    json.dumps({
                        "current_eph":   r["current_eph"],
                        "projected_eph": r["projected_eph"],
                        "health_score":  r["health_score"],
                        "eph_trend":     r["eph_trend"],
                    }),
                )
                recovery_count += 1
            except Exception as exc:
                log.warning("recovery_alert_failed", rider_id=rid, error=str(exc))

        # Earnings below threshold — gated by observation window + staleness + cooldown
        should_alert_earnings = (
            r["obs_point_mins"] >= _MIN_OBS_MINS
            and r["alert_level"] in ("watch", "intervene")
            and not r["stale_lags"]
        )
        if should_alert_earnings:
            if rid in earnings_cooldown:
                suppressed += 1
            else:
                # Downgrade severity one level if stale lags (belt-and-suspenders —
                # stale_lags=True already blocks this branch, but guard for clarity)
                severity = "high" if r["alert_level"] == "intervene" else "medium"
                try:
                    await conn.execute(
                        """
                        INSERT INTO rider_alerts
                            (id, rider_id, cycle_id, alert_type, message,
                             severity, metadata_json, is_resolved, created_at)
                        VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, FALSE, NOW())
                        """,
                        str(uuid.uuid4()), rid, cycle_id,
                        "earnings_below_threshold",
                        (
                            f"EPH alert: Rs.{r['current_eph']:.0f}/hr current, "
                            f"Rs.{r['projected_eph']:.0f}/hr projected "
                            f"vs Rs.{r['eph_target']:.0f}/hr target. "
                            f"Rs.{r['shortfall_rs']:.0f} shortfall over next 2 hrs. "
                            f"Trend: {r['eph_trend']}."
                        ),
                        severity,
                        json.dumps({
                            "current_eph":   r["current_eph"],
                            "projected_eph": r["projected_eph"],
                            "eph_target":    r["eph_target"],
                            "shortfall_rs":  r["shortfall_rs"],
                            "eph_trend":     r["eph_trend"],
                            "alert_level":   r["alert_level"],
                            "health_score":  r["health_score"],
                        }),
                    )
                    earnings_alerts += 1
                except Exception as exc:
                    log.warning("earnings_alert_failed", rider_id=rid, error=str(exc))
        elif r["obs_point_mins"] < _MIN_OBS_MINS and r["alert_level"] in ("watch", "intervene"):
            suppressed += 1  # observation window gate — count for quality KPI

        # ── Churn signal handling ───────────────────────────────
        if churn["is_churn_risk"]:

            # rider_churn_signals — written every cycle (regardless of cooldown)
            is_escalated = churn["consecutive_bad_sessions"] >= CHURN_SIGNAL_SESSIONS
            try:
                await conn.execute(
                    """
                    INSERT INTO rider_churn_signals
                        (id, rider_id, cycle_id, signal_strength,
                         consecutive_bad_sessions, avg_eph_last_n,
                         trigger_reason, is_escalated, created_at)
                    VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, NOW())
                    """,
                    str(uuid.uuid4()), rid, cycle_id,
                    churn["signal_strength"],
                    churn["consecutive_bad_sessions"],
                    churn["avg_eph_last_n"],
                    ", ".join(churn["trigger_details"]),
                    is_escalated,
                )
                churn_signals += 1
            except Exception as exc:
                log.warning("churn_signal_write_failed", rider_id=rid, error=str(exc))

            # rider_alerts (churn_risk) — only after persistence threshold
            if is_escalated:
                if rid in churn_cooldown:
                    suppressed += 1
                else:
                    severity = "high" if churn["signal_strength"] >= 0.7 else "medium"
                    try:
                        await conn.execute(
                            """
                            INSERT INTO rider_alerts
                                (id, rider_id, cycle_id, alert_type, message,
                                 severity, metadata_json, is_resolved, created_at)
                            VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, FALSE, NOW())
                            """,
                            str(uuid.uuid4()), rid, cycle_id,
                            "churn_risk",
                            (
                                f"{churn['consecutive_bad_sessions']} consecutive sessions "
                                f"below EPH target. Signal strength: {churn['signal_strength']:.2f}. "
                                f"Avg EPH: Rs.{churn['avg_eph_last_n'] or 0:.0f}/hr."
                            ),
                            severity,
                            json.dumps(churn),
                        )
                        churn_alerts += 1
                    except Exception as exc:
                        log.warning("churn_alert_failed", rider_id=rid, error=str(exc))

            # rider_interventions — all churn-risk riders (escalated=high, else medium)
            cross = cross_agent_ctx.get(rid, {})
            intervention_text = _build_intervention_text(
                {"persona_type": r["persona_type"]}, r, churn, cross
            )
            priority  = "high" if is_escalated else "medium"
            home_zone = r.get("home_zone_id", "")
            city      = rider_home_city.get(rid, "")

            # Prefer the Zone agent's already-computed recommendation (same zone shown in text).
            # Fall back to density-based selection if Zone agent has no context for this rider.
            rec_zone_id = cross.get("zone_rec_id")
            if not rec_zone_id:
                rec_zone_id = next(
                    (
                        zid for zid, density in best_zones_by_density
                        if zid != home_zone
                        and density > 0.15
                        and (not city or zone_meta.get(zid, {}).get("city") == city)
                    ),
                    None,
                )
            # Second pass: relax city constraint if nothing found (rider in isolated city)
            if not rec_zone_id:
                rec_zone_id = next(
                    (zid for zid, density in best_zones_by_density
                     if zid != home_zone and density > 0.15),
                    None,
                )
            try:
                await conn.execute(
                    """
                    INSERT INTO rider_interventions
                        (id, rider_id, cycle_id, recommendation_text,
                         recommended_zone_id, priority, was_acted_on, created_at)
                    VALUES ($1, $2::uuid, $3::uuid, $4, $5::uuid, $6, NULL, NOW())
                    """,
                    str(uuid.uuid4()), rid, cycle_id,
                    intervention_text,
                    rec_zone_id,
                    priority,
                )
                interventions += 1
            except Exception as exc:
                log.warning("intervention_write_failed", rider_id=rid, error=str(exc))

    # ── Operator alert: churn_surge ─────────────────────────────
    # If >= 15% of active riders show churn signals → platform-level signal
    operator_alerts  = 0
    churn_risk_count = len(churn_risk_ids)
    total_active     = len(scored_riders)

    if total_active > 0 and churn_risk_count / total_active >= _CHURN_SURGE_THRESHOLD:
        try:
            await conn.execute(
                """
                INSERT INTO operator_alerts
                    (id, cycle_id, agent_name, alert_type, severity,
                     title, message, metadata_json, is_resolved, created_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, FALSE, NOW())
                """,
                str(uuid.uuid4()), cycle_id,
                "EarningsAgent",
                "churn_surge",
                "critical",
                f"Churn surge: {churn_risk_count}/{total_active} riders at risk",
                (
                    f"{churn_risk_count} of {total_active} active riders "
                    f"({churn_risk_count / total_active * 100:.0f}%) are showing churn "
                    "risk signals. Consider platform-level incentive review."
                ),
                json.dumps({
                    "churn_risk_count": churn_risk_count,
                    "total_active":     total_active,
                    "churn_pct":        round(churn_risk_count / total_active * 100, 1),
                }),
            )
            operator_alerts += 1
        except Exception as exc:
            log.warning("churn_surge_alert_failed", error=str(exc))

    log.info(
        "earnings_alerts_done",
        earnings=earnings_alerts,
        churn=churn_alerts,
        recovery=recovery_count,
        suppressed=suppressed,
        churn_signals=churn_signals,
        interventions=interventions,
        op_alerts=operator_alerts,
    )

    return {
        **state,
        "churn_signals_written":         churn_signals,
        "interventions_written":         interventions,
        "earnings_alerts_written":       earnings_alerts,
        "churn_alerts_written":          churn_alerts,
        "recovery_count":                recovery_count,
        "alerts_suppressed_by_cooldown": suppressed,
        "operator_alerts_written":       operator_alerts,
    }


# ══════════════════════════════════════════════════════════════
# Node 5 — synthesize
# ══════════════════════════════════════════════════════════════

async def _synthesize(state: EarningsState) -> EarningsState:
    """
    LLM called once with cohort-level context (not per-rider).

    Groups riders by classification. Surfaces top N at-risk riders
    (anonymized — persona, EPH, shortfall, trend, consecutive bad sessions)
    to the LLM for a 2-3 sentence operator briefing.

    Supervisor headline KPIs — all top-level, predictable reads:
      at_risk_count, churn_risk_count, avg_eph,
      total_earnings_shortfall_rs (2hr actionable window),
      riders_intervened, recovery_count

    Quality KPIs:
      ml_failures, stale_lag_count,
      alerts_suppressed_by_cooldown, new_vs_repeat_at_risk
    """
    scored_riders    = state.get("scored_riders", [])
    churn_risk_ids   = state.get("churn_risk_ids", [])
    ml_failures      = state.get("ml_failures", 0)

    if not scored_riders:
        return {
            **state,
            "llm_narrative":               "No active rider sessions this cycle.",
            "summary_text":                "EarningsAgent: No active riders.",
            "severity":                    "normal",
            "status":                      "success",
            "at_risk_count":               0,
            "churn_risk_count":            0,
            "avg_eph":                     0.0,
            "total_earnings_shortfall_rs": 0.0,
            "riders_intervened":           0,
        }

    # Cohort aggregates
    classification_counts: dict[str, int] = defaultdict(int)
    total_eph       = 0.0
    total_shortfall = 0.0

    # Only include riders past the minimum observation window in the fleet
    # avg_eph.  At startup, sessions have seconds of data and earnings=0,
    # which would pull the average to 0 even when the fleet is healthy.
    eph_eligible = [r for r in scored_riders if r["obs_point_mins"] >= _MIN_OBS_MINS]
    if not eph_eligible:
        eph_eligible = scored_riders   # fallback: all riders (better than 0.0)

    for r in scored_riders:
        classification_counts[r["classification"]] += 1
        total_shortfall += r["shortfall_rs"]

    for r in eph_eligible:
        total_eph += r["current_eph"]

    total_active     = len(scored_riders)
    avg_eph          = total_eph / len(eph_eligible)
    at_risk_count    = classification_counts["at_risk"] + classification_counts["critical"]
    churn_risk_count = len(churn_risk_ids)

    # Top at-risk riders for LLM (sorted by shortfall descending, anonymized)
    top_at_risk = sorted(
        [r for r in scored_riders if r["classification"] in ("at_risk", "critical")],
        key=lambda r: r["shortfall_rs"],
        reverse=True,
    )[:_TOP_AT_RISK_TO_LLM]

    # ── LLM narrative ─────────────────────────────────────────
    if top_at_risk or churn_risk_count > 0:
        rider_lines = [
            (
                f"Rider #{i + 1} ({r['persona_type']}): "
                f"EPH Rs.{r['current_eph']:.0f}/hr → projected Rs.{r['projected_eph']:.0f}/hr, "
                f"shortfall Rs.{r['shortfall_rs']:.0f}, trend={r['eph_trend']}, "
                f"{r['churn']['consecutive_bad_sessions']} bad sessions"
            )
            for i, r in enumerate(top_at_risk)
        ]
        cohort_line = (
            f"{total_active} active riders: "
            f"{classification_counts['healthy']} healthy, "
            f"{classification_counts['watch']} watch, "
            f"{at_risk_count} at-risk/critical. "
            f"Avg EPH: Rs.{avg_eph:.0f}/hr. "
            f"Total 2-hr shortfall: Rs.{total_shortfall:.0f}. "
            f"{churn_risk_count} churn-risk riders."
        )
        prompt = (
            "You are an operations assistant for a food delivery platform. "
            "Write a 2-3 sentence earnings intelligence briefing for the ops team.\n\n"
            f"Fleet snapshot: {cohort_line}\n\n"
            "Top at-risk riders:\n" + "\n".join(rider_lines)
            + "\n\nBe specific, professional, action-oriented. No bullet points."
        )
        narrative = await call_llm(prompt, max_tokens=150, temperature=0.2)

        if not narrative:
            narrative = (
                f"{at_risk_count} rider(s) at-risk or critical this cycle. "
                f"Avg fleet EPH: Rs.{avg_eph:.0f}/hr. "
                f"Total 2-hr earnings shortfall: Rs.{total_shortfall:.0f}."
            )
    else:
        narrative = (
            f"All {total_active} active riders are within healthy EPH parameters. "
            f"Avg EPH: Rs.{avg_eph:.0f}/hr."
        )

    # Severity
    if classification_counts["critical"] > 0 or state.get("operator_alerts_written", 0) > 0:
        severity = "critical"
    elif at_risk_count > 0 or churn_risk_count > 0:
        severity = "warning"
    else:
        severity = "normal"

    # Status — partial if majority of ML calls failed
    status = "partial" if ml_failures > total_active * 0.5 else "success"

    stale_note = (
        f", {state.get('stale_lag_count', 0)} stale-lag riders"
        if state.get("stale_lag_count", 0) > 0 else ""
    )
    summary_text = (
        f"EarningsAgent: {total_active} riders scored, "
        f"{at_risk_count} at-risk, {churn_risk_count} churn-risk, "
        f"avg EPH Rs.{avg_eph:.0f}/hr, "
        f"2hr shortfall Rs.{total_shortfall:.0f}"
        f"{stale_note}"
    )

    return {
        **state,
        "llm_narrative":               narrative,
        "summary_text":                summary_text,
        "severity":                    severity,
        "status":                      status,
        # Supervisor headline KPIs
        "at_risk_count":               at_risk_count,
        "churn_risk_count":            churn_risk_count,
        "avg_eph":                     round(avg_eph, 2),
        "total_earnings_shortfall_rs": round(total_shortfall, 2),
        "riders_intervened":           state.get("interventions_written", 0),
    }


# ══════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════

def _build_graph(conn, redis):
    """
    conn and redis injected via closure.
    score_riders and synthesize have no external I/O — pure state computation
    (plus ML server calls in score_riders, gated by semaphore).
    """
    g = StateGraph(EarningsState)

    async def fetch_riders(state):          return await _fetch_riders(state, conn, redis)
    async def write_health_snapshots(state): return await _write_health_snapshots(state, conn)
    async def create_alerts(state):          return await _create_alerts(state, conn)

    g.add_node("fetch_riders",           fetch_riders)
    g.add_node("score_riders",           _score_riders)
    g.add_node("write_health_snapshots", write_health_snapshots)
    g.add_node("create_alerts",          create_alerts)
    g.add_node("synthesize",             _synthesize)

    g.set_entry_point("fetch_riders")
    g.add_edge("fetch_riders",           "score_riders")
    g.add_edge("score_riders",           "write_health_snapshots")
    g.add_edge("write_health_snapshots", "create_alerts")
    g.add_edge("create_alerts",          "synthesize")
    g.add_edge("synthesize",             END)

    return g.compile()


# ══════════════════════════════════════════════════════════════
# Agent class
# ══════════════════════════════════════════════════════════════

class EarningsAgent(BaseAgent):

    async def run(self, cycle_id: str, **kwargs) -> dict[str, Any]:
        t = time.monotonic()

        try:
            graph = _build_graph(self.conn, self.redis)
            initial_state: EarningsState = {"cycle_id": cycle_id}
            final = await graph.ainvoke(initial_state)

            result = {
                "status":                        final.get("status",                      "partial"),
                "summary_text":                  final.get("summary_text",                "EarningsAgent completed"),
                "severity":                      final.get("severity",                    "normal"),
                "alert_count":                   (
                    final.get("earnings_alerts_written",   0)
                    + final.get("churn_alerts_written",    0)
                    + final.get("operator_alerts_written", 0)
                ),
                # Supervisor headline KPIs
                "at_risk_count":                 final.get("at_risk_count",               0),
                "churn_risk_count":              final.get("churn_risk_count",            0),
                "avg_eph":                       final.get("avg_eph",                     0.0),
                "total_earnings_shortfall_rs":   final.get("total_earnings_shortfall_rs", 0.0),
                "riders_intervened":             final.get("riders_intervened",           0),
                "recovery_count":                final.get("recovery_count",              0),
                # Quality KPIs
                "ml_failures":                   final.get("ml_failures",                 0),
                "stale_lag_count":               final.get("stale_lag_count",             0),
                "alerts_suppressed_by_cooldown": final.get("alerts_suppressed_by_cooldown", 0),
                "new_vs_repeat_at_risk":         final.get("new_vs_repeat_at_risk",       0),
                # Write counts
                "snapshots_written":             final.get("snapshots_written",           0),
                "churn_signals_written":         final.get("churn_signals_written",       0),
                "interventions_written":         final.get("interventions_written",       0),
                # Narrative
                "llm_narrative":                 final.get("llm_narrative",               ""),
            }
            status = final.get("status", "partial")

        except Exception as exc:
            self.log.error("earnings_agent_failed", error=str(exc), exc_info=True)
            result = {
                "status":                        "failed",
                "summary_text":                  f"EarningsAgent failed: {exc}",
                "severity":                      "normal",
                "alert_count":                   0,
                "at_risk_count":                 0,
                "churn_risk_count":              0,
                "avg_eph":                       0.0,
                "total_earnings_shortfall_rs":   0.0,
                "riders_intervened":             0,
                "recovery_count":                0,
                "ml_failures":                   0,
                "stale_lag_count":               0,
                "alerts_suppressed_by_cooldown": 0,
                "new_vs_repeat_at_risk":         0,
                "snapshots_written":             0,
                "churn_signals_written":         0,
                "interventions_written":         0,
                "llm_narrative":                 "",
            }
            status = "failed"

        await self._log_to_db(
            cycle_id,
            result,
            result["summary_text"],
            int((time.monotonic() - t) * 1000),
            status,
        )
        return result
