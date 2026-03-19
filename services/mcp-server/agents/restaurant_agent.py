"""
ARIA — Restaurant Intelligence Agent
======================================
LangGraph-powered agent. Runs every 15-minute cycle.

Responsibility:
  Detect restaurants whose current delay pattern deviates significantly
  from their historical baseline (z-score signal). Alert riders waiting
  at those restaurants AND surface an operator-facing panel alert so
  dispatchers can see exactly what is happening in real time.

Pipeline (5 nodes):
  fetch_data → score_all → write_scores → create_alerts → synthesize

Key design decisions:
  - Z-score deviation is the signal, NOT ML prediction.
    sigmoid(z_score) → 0-1 risk_score. Fast, explainable, grounded in
    per-restaurant history (contextual: bad Friday 8PM, fine Tuesday 2PM).
  - Confidence gate: sample_count < 5 → severity capped at 'low'.
    New restaurants don't trigger production alerts.
  - Cooldown: skip alert if an unresolved alert for this restaurant
    was created < 30 minutes ago (< 2 cycles). Prevents alert fatigue.
  - Two alert targets:
      rider_alerts    — rider-facing "restaurant is delayed, expect wait"
      operator_alerts — ops panel "N restaurants high risk, action may be needed"
  - active_pickups_now is attached for display/sort but does NOT change
    the risk score. Risk is a function of historical pattern, not current queue.
  - LLM called once at the end for a concise operator-facing paragraph
    on the top 5 high-risk restaurants (max_tokens=150).
  - Supervisor reads restaurant_risk_scores from DB (not in-memory return).
"""

import json
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, StateGraph

from agents.base import BaseAgent
from algorithms.restaurant import (
    compute_restaurant_baseline,
    DELAY_Z_SCORE_THRESHOLD,
)
from config import RESTAURANT_RISK_THRESHOLD
from llm import call_llm

log = structlog.get_logger()

# ── Tuning ─────────────────────────────────────────────────────
_MIN_SAMPLE_CONFIDENCE = 5      # below this: severity forced to 'low'
_COOLDOWN_MINS         = 30     # skip alert if < 30 min since last unresolved alert
_TOP_N_TO_LLM          = 5      # pass top N high-risk restaurants to LLM for narrative

# Queue congestion formula — must match event-stream order_factory.py
_PREP_TIME_PER_SLOT    = 5.0    # minutes per capacity slot (matches event-stream config)


# ══════════════════════════════════════════════════════════════
# State schema
# ══════════════════════════════════════════════════════════════

class RestaurantState(TypedDict, total=False):
    cycle_id:        str
    sim_now:         datetime   # sim clock time passed from scheduler (uses sim hour for baseline)
    now:             datetime   # resolved from sim_now or real UTC

    # Node 1 output
    restaurants:     list[dict]       # all active restaurants with base_prep + zone info
    active_pickups:  list[dict]       # orders currently at pickup stage
    live_queues:     dict[str, int]   # restaurant_id → current queue length from Redis

    # Node 2 output
    scored:          list[dict]       # one dict per restaurant with risk fields

    # Node 3 output
    scores_written:  int              # how many rows inserted to restaurant_risk_scores

    # Node 4 output
    rider_alerts_created:    int
    operator_alerts_created: int
    cooldown_skipped:        int

    # Node 5 output
    llm_narrative:   str
    summary_text:    str
    alert_count:     int
    severity:        str
    status:          str


# ══════════════════════════════════════════════════════════════
# Node 1 — fetch_data
# ══════════════════════════════════════════════════════════════

async def _fetch_data(state: RestaurantState, conn, redis) -> RestaurantState:
    """
    Fetch all active restaurants + their live queue lengths from Redis.
    Also fetch orders currently at pickup stage for rider alert targeting.

    Two-signal approach:
      Signal 1 — Historical baseline (restaurant_delay_hourly, queried in _score_all)
      Signal 2 — Live queue length from Redis (fetched here via MGET pipeline)
    """
    now = state.get("sim_now") or datetime.now(timezone.utc)

    # All active restaurants with their base prep time
    restaurant_rows = await conn.fetch(
        """
        SELECT
            id               AS restaurant_id,
            name             AS restaurant_name,
            zone_id,
            avg_prep_time_mins AS base_prep_mins
        FROM restaurants
        WHERE is_active = TRUE
        ORDER BY name
        """
    )

    restaurants = [
        {
            "restaurant_id":   str(row["restaurant_id"]),
            "restaurant_name": row["restaurant_name"],
            "zone_id":         str(row["zone_id"]),
            "base_prep_mins":  float(row["base_prep_mins"] or 20.0),
        }
        for row in restaurant_rows
    ]

    # Batch-fetch live queue lengths from Redis in one round-trip
    keys = [f"aria:restaurant_queue:{r['restaurant_id']}" for r in restaurants]
    values = await redis.mget(*keys) if keys else []
    live_queues: dict[str, int] = {
        r["restaurant_id"]: max(0, int(v or 0))   # clamp negatives from restart desync
        for r, v in zip(restaurants, values)
    }

    # Active pickups: used only for rider alert targeting and display
    pickup_rows = await conn.fetch(
        """
        SELECT
            o.id              AS order_id,
            o.restaurant_id,
            o.rider_id,
            o.status,
            o.rider_inbound_at,
            o.assigned_at,
            NOW()             AS now_ts
        FROM orders o
        WHERE o.status IN ('assigned', 'rider_inbound')
          AND o.assigned_at IS NOT NULL
        """
    )

    pickups: dict[str, list] = {}
    for row in pickup_rows:
        rid = str(row["restaurant_id"])
        if rid not in pickups:
            pickups[rid] = []
        anchor    = row["rider_inbound_at"] or row["assigned_at"]
        wait_mins = (row["now_ts"] - anchor).total_seconds() / 60 if anchor else 0.0
        pickups[rid].append({
            "order_id": str(row["order_id"]),
            "rider_id": str(row["rider_id"]) if row["rider_id"] else None,
            "wait_mins": round(max(wait_mins, 0.0), 1),
        })

    for r in restaurants:
        r["active_pickups"] = pickups.get(r["restaurant_id"], [])

    total_queued = sum(live_queues.values())
    log.info(
        "restaurant_fetch_done",
        total_restaurants=len(restaurants),
        total_queued_orders=total_queued,
        active_pickups=sum(len(r["active_pickups"]) for r in restaurants),
    )

    return {**state, "now": now, "restaurants": restaurants, "live_queues": live_queues}


# ══════════════════════════════════════════════════════════════
# Node 2 — score_all
# ══════════════════════════════════════════════════════════════

def _sigmoid(z: float) -> float:
    """Map z-score to (0, 1) risk probability."""
    return 1.0 / (1.0 + math.exp(-z))


def _congestion_extra_mins(base_prep: float, queue_len: int) -> float:
    """
    Deterministic expected extra minutes due to current queue congestion.
    Mirrors compute_prep_time() from event-stream/order_factory.py but without
    random noise — we want the expected value, not a sampled one.

      capacity        = max(2, round(base_prep / PREP_TIME_PER_SLOT))
      congestion_factor = 1 + max(0, (queue_len - capacity) / capacity)
      extra            = base_prep * (congestion_factor - 1)
                       = base_prep * max(0, (queue_len - capacity) / capacity)
    """
    capacity = max(2, round(base_prep / _PREP_TIME_PER_SLOT))
    extra    = base_prep * max(0.0, (queue_len - capacity) / capacity)
    return round(extra, 2)


async def _score_all(state: RestaurantState, conn) -> RestaurantState:
    """
    Two-signal scoring per restaurant:

      Signal 1 — Historical baseline (restaurant_delay_hourly, sim hour/day)
        Answers: "What is the normal delay for this restaurant at this time?"

      Signal 2 — Live queue length (Redis, fetched in _fetch_data)
        Answers: "How much extra wait is the current queue adding right now?"

      z_score   = (congestion_extra - baseline_avg) / baseline_std
      risk_score = sigmoid(z_score)

    A restaurant with a normally-clean baseline that suddenly has a large queue
    scores high. A restaurant that always has a queue scores lower (it's expected).
    An empty queue always drives the score toward baseline (normal or below).
    """
    now        = state["now"]
    hour       = now.hour
    dow        = now.weekday()
    live_queues = state.get("live_queues", {})

    scored = []
    for r in state["restaurants"]:
        baseline = await compute_restaurant_baseline(
            r["restaurant_id"], hour, dow, conn
        )

        if not baseline["has_baseline"]:
            continue

        queue_len      = live_queues.get(r["restaurant_id"], 0)
        congestion_extra = _congestion_extra_mins(r["base_prep_mins"], queue_len)

        avg = baseline["avg_delay_mins"]
        std = max(baseline["std_delay_mins"], 0.5)
        z   = (congestion_extra - avg) / std

        risk_score     = round(_sigmoid(z), 4)
        sample_count   = baseline["sample_count"]
        low_confidence = sample_count < _MIN_SAMPLE_CONFIDENCE

        if risk_score >= RESTAURANT_RISK_THRESHOLD:
            raw_severity = "critical"
        elif risk_score >= 0.5:
            raw_severity = "medium"
        else:
            raw_severity = "normal"

        severity = "low" if (low_confidence and raw_severity != "normal") else raw_severity

        scored.append({
            "restaurant_id":      r["restaurant_id"],
            "restaurant_name":    r["restaurant_name"],
            "zone_id":            r["zone_id"],
            "risk_score":         risk_score,
            "severity":           severity,
            "raw_severity":       raw_severity,
            "z_score":            round(z, 3),
            "deviation_mins":     round(congestion_extra - avg, 2),  # how far above/below baseline
            "baseline_avg_mins":  round(avg, 2),
            "baseline_std_mins":  round(std, 2),
            "sample_count":       sample_count,
            "low_confidence":     low_confidence,
            "queue_len":          queue_len,
            "congestion_extra_mins": congestion_extra,
            "base_prep_mins":     r["base_prep_mins"],
            "active_pickups":     r["active_pickups"],
            "active_pickups_now": len(r["active_pickups"]),
            "hour_of_day":        hour,
            "day_of_week":        dow,
        })

    # Only keep restaurants with meaningful scores (skip normal/empty-queue ones with no baseline signal)
    scored.sort(key=lambda x: x["risk_score"], reverse=True)

    log.info(
        "restaurant_score_done",
        total_scored=len(scored),
        high_risk=sum(1 for s in scored if s["severity"] in ("critical", "medium")),
        queued=sum(1 for s in scored if s["queue_len"] > 0),
    )

    return {**state, "scored": scored}


# ══════════════════════════════════════════════════════════════
# Node 3 — write_scores
# ══════════════════════════════════════════════════════════════

async def _write_scores(state: RestaurantState, conn) -> RestaurantState:
    """
    Insert one row per restaurant into restaurant_risk_scores.
    Schema stores the core score + compact explanatory metadata.
    """
    cycle_id = state["cycle_id"]
    written  = 0

    for s in state["scored"]:
        key_factors = {
            "z_score":               s["z_score"],
            "queue_len":             s["queue_len"],
            "congestion_extra_mins": s["congestion_extra_mins"],
            "baseline_avg_mins":     s["baseline_avg_mins"],
            "deviation_mins":        s["deviation_mins"],
            "sample_count":          s["sample_count"],
            "active_pickups_now":    s["active_pickups_now"],
            "low_confidence":        s["low_confidence"],
        }
        try:
            await conn.execute(
                """
                INSERT INTO restaurant_risk_scores
                    (id, restaurant_id, cycle_id, delay_risk_score,
                     expected_delay_mins, confidence, key_factors_json, explanation, timestamp)
                VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, NOW())
                """,
                str(uuid.uuid4()),
                s["restaurant_id"],
                cycle_id,
                s["risk_score"],
                max(float(s["deviation_mins"]), 0.0),
                min(float(s["sample_count"]) / 20.0, 1.0),
                json.dumps(key_factors),
                (
                    f"queue={s['queue_len']}, congestion_extra={s['congestion_extra_mins']} min, "
                    f"z={s['z_score']}, baseline={s['baseline_avg_mins']} min, "
                    f"active_pickups={s['active_pickups_now']}, severity={s['severity']}"
                ),
            )
            written += 1
        except Exception as exc:
            log.warning("write_score_failed", restaurant_id=s["restaurant_id"], error=str(exc))

    log.info("restaurant_scores_written", count=written)
    return {**state, "scores_written": written}


# ══════════════════════════════════════════════════════════════
# Node 4 — create_alerts
# ══════════════════════════════════════════════════════════════

async def _check_cooldown(restaurant_id: str, conn) -> bool:
    """Return True if a cooldown is active (alert created < 30 min ago)."""
    row = await conn.fetchrow(
        """
        SELECT id FROM rider_alerts
        WHERE metadata_json->>'restaurant_id' = $1
          AND alert_type = 'restaurant_delay'
          AND is_resolved = FALSE
          AND created_at > NOW() - INTERVAL '30 minutes'
        LIMIT 1
        """,
        restaurant_id,
    )
    return row is not None


async def _create_alerts(state: RestaurantState, conn) -> RestaurantState:
    """
    For each restaurant with severity 'critical' or 'warning' (not 'low'):
      1. Cooldown check — skip if alert < 30 min ago.
      2. rider_alerts: one alert per rider currently at that restaurant.
      3. operator_alerts: one alert per qualifying restaurant (system-level).
    """
    cycle_id = state["cycle_id"]
    rider_created    = 0
    operator_created = 0
    cooldown_skipped = 0

    # Only alert on restaurants that cleared the confidence gate
    alertable = [
        s for s in state["scored"]
        if s["severity"] in ("critical", "medium")
    ]

    for s in alertable:
        # Cooldown check
        if await _check_cooldown(s["restaurant_id"], conn):
            cooldown_skipped += 1
            log.debug("cooldown_active", restaurant_id=s["restaurant_id"])
            continue

        # ── rider_alerts ──────────────────────────────────────
        for pickup in s["active_pickups"]:
            rider_id = pickup.get("rider_id")
            if not rider_id:
                continue
            try:
                message = (
                    f"{s['restaurant_name']} is running "
                    f"{abs(s['deviation_mins']):.0f} min above its usual prep time. "
                    f"Expect a wait. This is {s['active_pickups_now']} order(s) affected."
                )
                await conn.execute(
                    """
                    INSERT INTO rider_alerts
                        (id, rider_id, cycle_id, alert_type, severity,
                         message, metadata_json, is_resolved, created_at)
                    VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, FALSE, NOW())
                    """,
                    str(uuid.uuid4()),
                    rider_id,
                    cycle_id,
                    "restaurant_delay",
                    s["severity"],
                    message,
                    json.dumps({
                        "restaurant_id":   s["restaurant_id"],
                        "restaurant_name": s["restaurant_name"],
                        "risk_score":      s["risk_score"],
                        "deviation_mins":  s["deviation_mins"],
                        "z_score":         s["z_score"],
                        "wait_mins":       pickup["wait_mins"],
                    }),
                )
                rider_created += 1
            except Exception as exc:
                log.warning("rider_alert_failed", rider_id=rider_id, error=str(exc))

        # ── operator_alerts ──────────────────────────────────
        try:
            ops_msg = (
                f"{s['restaurant_name']} is {s['deviation_mins']:.1f} min above "
                f"historical baseline (z={s['z_score']:.2f}, risk={s['risk_score']:.2f}). "
                f"{s['active_pickups_now']} rider(s) currently waiting. "
                f"Sample size: {s['sample_count']}."
            )
            await conn.execute(
                """
                INSERT INTO operator_alerts
                    (id, cycle_id, agent_name, alert_type, severity,
                     title, message, metadata_json, is_resolved, created_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, FALSE, NOW())
                """,
                str(uuid.uuid4()),
                cycle_id,
                "RestaurantAgent",
                "restaurant_high_risk",
                s["severity"],
                f"High-risk restaurant: {s['restaurant_name']}",
                ops_msg,
                json.dumps({
                    "restaurant_id":      s["restaurant_id"],
                    "restaurant_name":    s["restaurant_name"],
                    "zone_id":            s["zone_id"],
                    "risk_score":         s["risk_score"],
                    "z_score":            s["z_score"],
                    "deviation_mins":     s["deviation_mins"],
                    "baseline_avg_mins":  s["baseline_avg_mins"],
                    "sample_count":       s["sample_count"],
                    "active_pickups_now": s["active_pickups_now"],
                    "low_confidence":     s["low_confidence"],
                }),
            )
            operator_created += 1
        except Exception as exc:
            log.warning("operator_alert_failed", restaurant_id=s["restaurant_id"], error=str(exc))

    log.info(
        "restaurant_alerts_done",
        rider_created=rider_created,
        operator_created=operator_created,
        cooldown_skipped=cooldown_skipped,
    )

    return {
        **state,
        "rider_alerts_created":    rider_created,
        "operator_alerts_created": operator_created,
        "cooldown_skipped":        cooldown_skipped,
    }


# ══════════════════════════════════════════════════════════════
# Node 5 — synthesize
# ══════════════════════════════════════════════════════════════

async def _synthesize(state: RestaurantState) -> RestaurantState:
    """
    Call the LLM once to produce a concise operator-facing narrative
    on the top N high-risk restaurants.

    Falls back to a template string if LLM returns empty (vLLM down, etc.).
    """
    high_risk = [s for s in state["scored"] if s["severity"] in ("critical", "medium")]
    top        = high_risk[:_TOP_N_TO_LLM]

    alert_count = state["rider_alerts_created"] + state["operator_alerts_created"]

    # Determine overall severity
    if any(s["severity"] == "critical" for s in top):
        overall_severity = "critical"
    elif top:
        overall_severity = "warning"
    else:
        overall_severity = "normal"

    if top:
        # Build a compact context block for the LLM
        lines = []
        for i, s in enumerate(top, 1):
            lines.append(
                f"{i}. {s['restaurant_name']}: risk={s['risk_score']:.2f}, "
                f"queue={s['queue_len']} orders, +{s['congestion_extra_mins']:.0f}min congestion "
                f"(baseline {s['baseline_avg_mins']:.1f}min), "
                f"{s['active_pickups_now']} rider(s) waiting"
            )
        context = "\n".join(lines)

        prompt = (
            "You are an operations assistant for a food delivery platform. "
            "Write a 2-3 sentence briefing for the dispatch team about these high-risk restaurants.\n\n"
            f"{context}\n\n"
            "Be specific, professional, and action-oriented. No bullet points."
        )

        narrative = await call_llm(prompt, max_tokens=150, temperature=0.2)

        if not narrative:
            # Fallback template
            names = ", ".join(s["restaurant_name"] for s in top[:3])
            narrative = (
                f"{len(high_risk)} restaurant(s) showing elevated queue congestion. "
                f"Top: {names}. "
                f"Riders at these locations may experience increased wait times this cycle."
            )
    else:
        narrative = "No restaurants showing significant delay patterns this cycle."

    summary_text = (
        f"RestaurantAgent: {len(high_risk)} high-risk, "
        f"{alert_count} alerts ({state['rider_alerts_created']} rider, "
        f"{state['operator_alerts_created']} operator), "
        f"{state.get('cooldown_skipped', 0)} cooldown-skipped"
    )

    return {
        **state,
        "llm_narrative":  narrative,
        "summary_text":   summary_text,
        "alert_count":    alert_count,
        "severity":       overall_severity,
        "status":         "success",
    }


# ══════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════

def _build_graph(conn, redis):
    """
    Build and compile the LangGraph StateGraph.

    Nodes are wrapped to inject DB connection + Redis (LangGraph nodes
    receive only state — external deps injected via closure).
    """
    g = StateGraph(RestaurantState)

    async def fetch_data(state):  return await _fetch_data(state, conn, redis)
    async def score_all(state):   return await _score_all(state, conn)
    async def write_scores(state): return await _write_scores(state, conn)
    async def create_alerts(state): return await _create_alerts(state, conn)

    g.add_node("fetch_data",     fetch_data)
    g.add_node("score_all",      score_all)
    g.add_node("write_scores",   write_scores)
    g.add_node("create_alerts",  create_alerts)
    g.add_node("synthesize",     _synthesize)

    g.set_entry_point("fetch_data")
    g.add_edge("fetch_data",    "score_all")
    g.add_edge("score_all",     "write_scores")
    g.add_edge("write_scores",  "create_alerts")
    g.add_edge("create_alerts", "synthesize")
    g.add_edge("synthesize",    END)

    return g.compile()


# ══════════════════════════════════════════════════════════════
# Agent class
# ══════════════════════════════════════════════════════════════

class RestaurantAgent(BaseAgent):

    async def run(self, cycle_id: str, sim_now: datetime | None = None, **kwargs) -> dict[str, Any]:
        t = time.monotonic()

        try:
            graph = _build_graph(self.conn, self.redis)
            initial_state: RestaurantState = {
                "cycle_id": cycle_id,
                **({"sim_now": sim_now} if sim_now else {}),
            }
            final_state = await graph.ainvoke(initial_state)

            # above_threshold_count = restaurants with delay_risk_score >= RESTAURANT_RISK_THRESHOLD.
            # This is what the frontend panel displays, so the supervisor should use this
            # (not operator_alerts_created, which fires at risk_score >= 0.5 / medium severity).
            scored = final_state.get("scored", [])
            above_threshold_count = sum(
                1 for s in scored if s["risk_score"] >= RESTAURANT_RISK_THRESHOLD
            )

            result = {
                "status":               final_state.get("status",       "partial"),
                "summary_text":         final_state.get("summary_text", "RestaurantAgent completed"),
                "alert_count":          final_state.get("alert_count",  0),
                "severity":             final_state.get("severity",     "normal"),
                "scores_written":       final_state.get("scores_written", 0),
                "llm_narrative":        final_state.get("llm_narrative", ""),
                "rider_alerts":         final_state.get("rider_alerts_created", 0),
                "operator_alerts":      final_state.get("operator_alerts_created", 0),
                "above_threshold_count": above_threshold_count,
                "cooldown_skipped":     final_state.get("cooldown_skipped", 0),
            }
            status = "success"

        except Exception as exc:
            self.log.error("restaurant_agent_failed", error=str(exc), exc_info=True)
            result = {
                "status":       "failed",
                "summary_text": f"RestaurantAgent failed: {exc}",
                "alert_count":  0,
                "severity":     "normal",
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
