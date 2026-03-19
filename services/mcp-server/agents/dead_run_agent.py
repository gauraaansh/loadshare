"""
ARIA — Dead Run Prevention Agent
==================================
LangGraph-powered agent. Runs every 15-minute cycle.

Responsibility:
  Score all active orders (assigned, rider_inbound, picked_up) against
  Model 3 (Dead Zone Risk Predictor) to identify which riders are heading
  toward low-demand destination zones that will strand them after delivery.
  Alert those riders proactively and surface zone-level risk to operators.

Pipeline (5 nodes):
  fetch_orders → score_orders → write_scores → create_alerts → synthesize

Key design decisions:

  Score window:
    assigned + rider_inbound → rider-alert eligible (rider committed but
      hasn't reached destination, alert is still actionable)
    picked_up → zone snapshot analytics only (rider in transit, alerting
      is too late but the destination zone data is valid signal)
    pending → excluded (event-stream dispatches in seconds; effectively
      no pending orders exist at 15-min cycle time)

  ML flagging gate:
    Use ml_result["is_high_risk"] directly — this is the ML server's
    calibrated judgment (isotonic regression at 0.55 threshold).
    Never re-threshold dead_zone_probability against DEAD_ZONE_RISK_THRESHOLD
    in the agent — that overrides the calibration.

  Bulk fetch (no per-order DB queries):
    Node 1 does all data loading:
      - Single JOIN query: orders + delivery zone + rider + home zone + session
      - Redis HGET pipeline: zone density for all unique delivery zones
      - Single IN query: historical dead rate for all unique delivery zones
    Node 2 assembles ml_inputs inline from pre-fetched data.

  Concurrent ML calls:
    asyncio.gather with Semaphore(20) matching httpx max_connections=20.
    return_exceptions=True — one ML failure doesn't abort the batch.
    ML failures write a safe fallback row (is_flagged=False) so the cycle
    record is complete even when the ML server is degraded.

  Zone aggregation:
    Multiple orders may target the same delivery zone. Aggregate per zone:
      risk_level = avg(dead_zone_probability)  — more stable than max
      expected_stranding_mins = max(expected_stranding_mins)  — worst case
    Evidence gate before writing dead_zone_snapshot + operator alert:
      max_risk >= HIGH_THRESHOLD (0.75)  OR
      (max_risk >= DEAD_ZONE_RISK_THRESHOLD AND flagged_order_count >= 2)
    Prevents a single outlier order from triggering zone-level noise.

  Cooldown keys:
    rider_alerts:    (rider_id, delivery_zone_id) — suppress same rider+zone
    operator_alerts: (zone_id) — one per zone per 30 min

  Session escalation:
    Riders with session_dead_runs >= 1 already this session get severity
    upgraded to "critical" and a stronger alert message.

  Persona-specific EPH:
    compute_dead_run_cost() called with persona-specific EPH target
    (Rs.100 dedicated, Rs.90 supplementary) not the flat Rs.82 default.

  System pressure detection:
    If flagged_orders / total_scored >= 0.50, write a single
    system_dead_zone_pressure operator alert — city-level collapse signal
    that bypasses per-zone evidence gating.

  Supervisor KPI:
    total_earnings_at_risk_rs — sum of earnings_lost_rs across all flagged
    orders. The one business-facing number that tells the Supervisor how
    much EPH is on the line this cycle.

  expected_cost_mins schema:
    Stores expected_stranding_mins (minutes — matches column name semantics).
    Earnings in Rs. is derivable: stranding_mins / 60 × EPH. Not stored
    per-order in DB; tracked as cycle-level aggregate in summary_text.

From Loadshare 2023 research:
  Dead runs were a primary driver of EPH collapse (Rs.70-85 actual vs
  Rs.90-100 rider expectation). Peripheral zones with no return orders
  forced riders to travel empty, compounding the earnings gap.
"""

import asyncio
import json
import math
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, StateGraph

from agents.base import BaseAgent
from algorithms.constants import (
    CITY_TIER_ENC,
    CITY_TIER_ENC_DEFAULT,
    ZONE_TYPE_ENC,
    ZONE_TYPE_ENC_DEFAULT,
)
from algorithms.restaurant import DEAD_ZONE_HISTORY_DAYS, DEAD_ZONE_STRESS_THRESHOLD
from algorithms.session import compute_dead_run_cost
from config import (
    DEAD_ZONE_RISK_THRESHOLD,
    EPH_TARGET_DEDICATED,
    EPH_TARGET_SUPPLEMENTARY,
)
from llm import call_llm
from ml_client import predict_dead_zone
from redis_client import key_zone_density

log = structlog.get_logger()

# ── Tuning constants ──────────────────────────────────────────
_ML_CONCURRENCY          = 20     # Semaphore cap — matches httpx max_connections
_COOLDOWN_MINS           = 30     # suppress repeat alert for (rider, zone) pair
_DEAD_ZONE_HIGH_THRESHOLD = 0.75  # single order triggers zone snapshot + alert
_DEAD_ZONE_MIN_ORDERS    = 2      # medium-risk zone needs N flagged orders to trigger
_SYSTEM_PRESSURE_RATIO   = 0.50   # flagged / total > this → systemic operator alert
_TOP_N_TO_LLM            = 5      # top N zones passed to LLM for narrative
_ALERT_STATUSES          = {"assigned", "rider_inbound"}  # only these get rider alerts


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _eph_for_persona(persona: str) -> float:
    return EPH_TARGET_DEDICATED if persona == "dedicated" else EPH_TARGET_SUPPLEMENTARY


def _rider_alert_message(s: dict) -> str:
    """
    Build a persona-aware, session-escalation-aware rider alert message.
    Dedicated riders get Rs.100/hr target framing.
    Riders who already have dead runs this session get an escalation note.
    """
    zone_name = s.get("dest_zone_name", "the delivery zone")
    stranding = s.get("expected_stranding_mins")
    earnings  = s.get("earnings_lost_rs", 0.0)
    dead_runs = s.get("session_dead_runs", 0)
    persona   = s.get("rider_persona", "supplementary")

    prior = (
        f" You've already had {dead_runs} dead run(s) this session — act on this."
        if dead_runs >= 1 else ""
    )
    target_str = (
        f"Rs.{EPH_TARGET_DEDICATED:.0f}/hr target"
        if persona == "dedicated"
        else f"Rs.{EPH_TARGET_SUPPLEMENTARY:.0f}/hr target"
    )

    if stranding:
        return (
            f"{zone_name} is high dead-zone risk (~{stranding:.0f}min expected stranding, "
            f"~Rs.{earnings:.0f} at stake vs your {target_str}).{prior} "
            f"Plan to reposition after drop-off."
        )
    return (
        f"{zone_name} shows elevated dead-zone risk.{prior} "
        f"Plan to reposition after drop-off."
    )


# ══════════════════════════════════════════════════════════════
# State schema
# ══════════════════════════════════════════════════════════════

class DeadRunState(TypedDict, total=False):
    cycle_id:              str
    now:                   datetime

    # Node 1 output
    orders:                list[dict]   # active orders with all context embedded
    zone_density_cache:    dict         # zone_id → density_score (Redis-first)
    zone_dead_rate_cache:  dict         # zone_id → historical dead rate fraction

    # Node 2 output
    scored:                list[dict]   # one dict per scored order
    ml_failures:           int          # count of exceptions / unavailable ML calls

    # Node 3 output
    scores_written:        int
    zone_snapshots_written: int
    flagged_zones:         dict         # zone_id → aggregate stats

    # Node 4 output
    rider_alerts_created:  int
    operator_alerts_created: int
    cooldown_skipped:      int
    system_pressure:       bool

    # Node 5 output
    llm_narrative:         str
    summary_text:          str
    alert_count:           int
    severity:              str
    status:                str
    total_earnings_at_risk_rs: float


# ══════════════════════════════════════════════════════════════
# Node 1 — fetch_orders
# ══════════════════════════════════════════════════════════════

async def _fetch_orders(state: DeadRunState, conn, redis) -> DeadRunState:
    """
    Single bulk-fetch phase. Three queries + one Redis pipeline — no per-order queries.

    Query 1 (main JOIN):
      orders + delivery zone metadata + rider home zone + active session dead_runs_count.
      Tags each order: for_rider_alert = status in ('assigned', 'rider_inbound').

    Redis pipeline:
      HGET density_score for all unique delivery zone IDs in one round-trip.
      Falls back to zone_density_snapshots DB table for zones not in cache.

    Query 2 (historical dead rate):
      COUNT(snapshots where stress_ratio < threshold) / COUNT(all) per zone,
      over last DEAD_ZONE_HISTORY_DAYS days. Single IN query for all zones.
    """
    now = datetime.now(timezone.utc)

    order_rows = await conn.fetch(
        """
        SELECT
            o.id                               AS order_id,
            o.rider_id,
            o.delivery_zone_id,
            o.distance_km,
            o.is_long_distance,
            o.weather_condition,
            o.traffic_density,
            o.status,
            o.assigned_at,
            -- delivery zone
            dz.name                            AS dest_zone_name,
            dz.city                            AS dest_city,
            dz.centroid_lat                    AS dest_lat,
            dz.centroid_lng                    AS dest_lng,
            dz.boundary_geojson->>'zone_type'  AS dest_zone_type,
            dz.boundary_geojson->>'city_tier'  AS dest_city_tier,
            -- rider
            r.home_zone_id,
            r.persona_type                     AS rider_persona,
            hz.centroid_lat                    AS home_lat,
            hz.centroid_lng                    AS home_lng,
            -- active session: dead_runs_count updated live by event-stream
            COALESCE(rs.dead_runs_count, 0)    AS session_dead_runs
        FROM orders o
        JOIN zones   dz ON dz.id = o.delivery_zone_id
        JOIN riders  r  ON r.id  = o.rider_id
        JOIN zones   hz ON hz.id = r.home_zone_id
        LEFT JOIN LATERAL (
            SELECT dead_runs_count FROM rider_sessions
            WHERE rider_id = o.rider_id AND shift_end IS NULL
            ORDER BY shift_start DESC LIMIT 1
        ) rs ON TRUE
        WHERE o.status IN ('assigned', 'rider_inbound', 'picked_up')
          AND o.rider_id IS NOT NULL
        ORDER BY o.assigned_at ASC
        """,
    )

    if not order_rows:
        log.info("dead_run_fetch_done", orders=0)
        return {
            **state,
            "now":                 now,
            "orders":              [],
            "zone_density_cache":  {},
            "zone_dead_rate_cache": {},
        }

    orders = []
    for row in order_rows:
        orders.append({
            "order_id":          str(row["order_id"]),
            "rider_id":          str(row["rider_id"]),
            "delivery_zone_id":  str(row["delivery_zone_id"]),
            "distance_km":       float(row["distance_km"] or 0.0),
            "is_long_distance":  bool(row["is_long_distance"]),
            "weather_condition": row["weather_condition"],
            "traffic_density":   row["traffic_density"],
            "status":            row["status"],
            "for_rider_alert":   row["status"] in _ALERT_STATUSES,
            "dest_zone_name":    row["dest_zone_name"],
            "dest_city":         row["dest_city"],
            "dest_lat":          float(row["dest_lat"]),
            "dest_lng":          float(row["dest_lng"]),
            "dest_zone_type":    row["dest_zone_type"] or "",
            "dest_city_tier":    row["dest_city_tier"] or "",
            "home_zone_id":      str(row["home_zone_id"]),
            "rider_persona":     row["rider_persona"] or "supplementary",
            "home_lat":          float(row["home_lat"]),
            "home_lng":          float(row["home_lng"]),
            "session_dead_runs": int(row["session_dead_runs"]),
        })

    unique_zone_ids = list({o["delivery_zone_id"] for o in orders})

    # ── Redis pipeline: zone density for all unique delivery zones ──
    zone_density_cache: dict[str, float] = {}
    async with redis.pipeline(transaction=False) as pipe:
        for zid in unique_zone_ids:
            pipe.hget(key_zone_density(zid), "density_score")
        redis_results = await pipe.execute()

    for zid, val in zip(unique_zone_ids, redis_results):
        if val is not None:
            try:
                zone_density_cache[zid] = float(val)
            except (ValueError, TypeError):
                pass

    # ── DB fallback for zones not found in Redis ────────────────
    missing = [zid for zid in unique_zone_ids if zid not in zone_density_cache]
    if missing:
        db_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (zone_id) zone_id, density_score
            FROM zone_density_snapshots
            WHERE zone_id = ANY($1::uuid[])
            ORDER BY zone_id, timestamp DESC
            """,
            missing,
        )
        for row in db_rows:
            zone_density_cache[str(row["zone_id"])] = float(row["density_score"])

    # ── Historical dead rate: single IN query for all zones ─────
    dead_rate_rows = await conn.fetch(
        """
        SELECT
            zone_id,
            COUNT(*) FILTER (WHERE stress_ratio < $2) AS dead_count,
            COUNT(*)                                   AS total_count
        FROM zone_density_snapshots
        WHERE zone_id = ANY($1::uuid[])
          AND timestamp > NOW() - ($3 || ' days')::INTERVAL
        GROUP BY zone_id
        """,
        unique_zone_ids,
        DEAD_ZONE_STRESS_THRESHOLD,
        str(DEAD_ZONE_HISTORY_DAYS),
    )

    zone_dead_rate_cache: dict[str, float] = {}
    for row in dead_rate_rows:
        zid = str(row["zone_id"])
        if row["total_count"] and int(row["total_count"]) > 0:
            zone_dead_rate_cache[zid] = (
                float(row["dead_count"]) / float(row["total_count"])
            )

    log.info(
        "dead_run_fetch_done",
        orders=len(orders),
        unique_zones=len(unique_zone_ids),
        redis_hits=len(zone_density_cache) - len(missing),
        db_fallbacks=len(missing),
    )

    return {
        **state,
        "now":                 now,
        "orders":              orders,
        "zone_density_cache":  zone_density_cache,
        "zone_dead_rate_cache": zone_dead_rate_cache,
    }


# ══════════════════════════════════════════════════════════════
# Node 2 — score_orders
# ══════════════════════════════════════════════════════════════

def _build_ml_inputs(
    order: dict,
    hour: int,
    dow: int,
    is_weekend: int,
    zone_density_cache: dict,
    zone_dead_rate_cache: dict,
) -> dict:
    """
    Assemble Model 3 (DeadZoneRequest) feature dict from pre-fetched data.
    Mirrors score_assignment() but uses cached zone density + dead rate —
    no DB round-trips per order.
    """
    zid = order["delivery_zone_id"]
    dist_from_home = _haversine_km(
        order["home_lat"], order["home_lng"],
        order["dest_lat"], order["dest_lng"],
    )
    return {
        "dest_zone_type_enc":     ZONE_TYPE_ENC.get(order["dest_zone_type"], ZONE_TYPE_ENC_DEFAULT),
        "city_tier_enc":          CITY_TIER_ENC.get(order["dest_city_tier"], CITY_TIER_ENC_DEFAULT),
        "hour_of_day":            hour,
        "day_of_week":            dow,
        "is_weekend":             is_weekend,
        "is_ld_order":            1 if order["is_long_distance"] else 0,
        "dist_from_home_zone_km": round(dist_from_home, 2),
        "current_density_ratio":  round(zone_density_cache.get(zid, 0.0), 4),
        "historical_dead_rate":   round(zone_dead_rate_cache.get(zid, 0.3), 4),
    }


async def _score_orders(state: DeadRunState) -> DeadRunState:
    """
    Build ml_inputs for each order inline from cached state.
    Fire all ML calls concurrently under Semaphore(20).

    is_high_risk comes directly from the ML server — do NOT re-threshold
    dead_zone_probability. The ML server uses isotonic-calibrated 0.55
    threshold; overriding it here defeats the calibration.

    For high-risk orders: compute_dead_run_cost() with persona-specific EPH
    (dedicated = Rs.100, supplementary = Rs.90) for accurate cost estimates.

    ML failures (None return or exception): write safe fallback
    (dead_zone_probability=0.0, is_high_risk=False, ml_failed=True).
    Failures are counted — if >50% fail, synthesize marks status="partial".
    """
    orders             = state["orders"]
    now                = state["now"]
    zone_density_cache  = state["zone_density_cache"]
    zone_dead_rate_cache = state["zone_dead_rate_cache"]

    if not orders:
        return {**state, "scored": [], "ml_failures": 0}

    hour       = now.hour
    dow        = now.weekday()
    is_weekend = 1 if dow >= 5 else 0
    sem        = asyncio.Semaphore(_ML_CONCURRENCY)

    async def _score_one(order: dict) -> dict:
        async with sem:
            ml_inputs = _build_ml_inputs(
                order, hour, dow, is_weekend,
                zone_density_cache, zone_dead_rate_cache,
            )
            ml_result = await predict_dead_zone(ml_inputs)

            if ml_result is None:
                # ML server unavailable — safe fallback, no alert generated
                return {
                    **order,
                    "ml_inputs":               ml_inputs,
                    "dead_zone_probability":   0.0,
                    "is_high_risk":            False,
                    "expected_stranding_mins": None,
                    "expected_eph_loss":       None,
                    "earnings_lost_rs":        None,
                    "key_factors":             [],
                    "ml_failed":               True,
                }

            # Use is_high_risk directly — calibrated gate from ML server
            is_high_risk   = ml_result["is_high_risk"]
            stranding_mins = ml_result.get("expected_stranding_mins")
            earnings_lost_rs = None

            if is_high_risk and stranding_mins is not None:
                persona_eph      = _eph_for_persona(order["rider_persona"])
                cost             = compute_dead_run_cost(stranding_mins, assumed_eph=persona_eph)
                earnings_lost_rs = cost["earnings_lost_rs"]

            return {
                **order,
                "ml_inputs":               ml_inputs,
                "dead_zone_probability":   ml_result["dead_zone_probability"],
                "is_high_risk":            is_high_risk,
                "expected_stranding_mins": stranding_mins,
                "expected_eph_loss":       ml_result.get("expected_eph_loss"),
                "earnings_lost_rs":        earnings_lost_rs,
                "key_factors":             ml_result.get("key_factors", []),
                "ml_failed":               False,
            }

    raw_results = await asyncio.gather(
        *[_score_one(o) for o in orders],
        return_exceptions=True,
    )

    scored: list[dict] = []
    ml_failures = 0
    for item in raw_results:
        if isinstance(item, Exception):
            ml_failures += 1
            log.warning("dead_run_ml_exception", error=str(item))
            continue
        if item.get("ml_failed"):
            ml_failures += 1
        scored.append(item)

    log.info(
        "dead_run_score_done",
        total=len(scored),
        high_risk=sum(1 for s in scored if s["is_high_risk"]),
        ml_failures=ml_failures,
    )

    return {**state, "scored": scored, "ml_failures": ml_failures}


# ══════════════════════════════════════════════════════════════
# Node 3 — write_scores
# ══════════════════════════════════════════════════════════════

async def _write_scores(state: DeadRunState, conn) -> DeadRunState:
    """
    Two writes:

    order_risk_scores (one row per scored order, all statuses):
      dead_zone_risk      = dead_zone_probability (0–1)
      expected_cost_mins  = expected_stranding_mins (minutes — column semantics)
      is_flagged          = is_high_risk (ML calibrated gate)
      rationale           = prob + risk label + SHAP key_factors string

    dead_zone_snapshots (one row per zone per cycle, evidence-gated):
      risk_level               = avg(dead_zone_probability) across all orders to zone
      expected_stranding_mins  = max across flagged orders (worst-case)
      Gate: max_risk >= 0.75  OR  (max_risk >= threshold AND flagged_count >= 2)
      Prevents single-order noise from polluting the zone snapshot record.
      Zones that pass are stored in state["flagged_zones"] for create_alerts + synthesize.
    """
    cycle_id = state["cycle_id"]
    scored   = state["scored"]

    # ── order_risk_scores ──────────────────────────────────────
    scores_written = 0
    for s in scored:
        rationale = (
            "ml_unavailable" if s.get("ml_failed") else
            (
                f"prob={s['dead_zone_probability']:.3f}, "
                f"{'HIGH_RISK' if s['is_high_risk'] else 'normal'}, "
                f"factors={json.dumps(s['key_factors'])}"
            )
        )
        try:
            await conn.execute(
                """
                INSERT INTO order_risk_scores
                    (id, order_id, cycle_id, dead_zone_risk,
                     expected_cost_mins, is_flagged, rationale, timestamp)
                VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, NOW())
                """,
                str(uuid.uuid4()),
                s["order_id"],
                cycle_id,
                s["dead_zone_probability"],
                s.get("expected_stranding_mins"),   # minutes — matches column name
                s["is_high_risk"],
                rationale,
            )
            scores_written += 1
        except Exception as exc:
            log.warning("order_risk_score_write_failed",
                        order_id=s["order_id"], error=str(exc))

    # ── Zone aggregation ───────────────────────────────────────
    zone_agg: dict[str, dict] = defaultdict(lambda: {
        "probs":         [],
        "stranding_list": [],
        "flagged_count": 0,
        "total_count":   0,
        "zone_name":     "",
        "dest_city":     "",
    })

    for s in scored:
        zid = s["delivery_zone_id"]
        zone_agg[zid]["probs"].append(s["dead_zone_probability"])
        zone_agg[zid]["total_count"]  += 1
        zone_agg[zid]["zone_name"]     = s["dest_zone_name"]
        zone_agg[zid]["dest_city"]     = s["dest_city"]
        if s["is_high_risk"]:
            zone_agg[zid]["flagged_count"] += 1
            if s.get("expected_stranding_mins") is not None:
                zone_agg[zid]["stranding_list"].append(s["expected_stranding_mins"])

    # ── dead_zone_snapshots (evidence-gated) ──────────────────
    snapshots_written = 0
    flagged_zones: dict[str, dict] = {}

    for zid, agg in zone_agg.items():
        max_risk  = max(agg["probs"])
        avg_risk  = sum(agg["probs"]) / len(agg["probs"])
        max_strand = max(agg["stranding_list"]) if agg["stranding_list"] else None

        passes_gate = (
            max_risk >= _DEAD_ZONE_HIGH_THRESHOLD
            or (max_risk >= DEAD_ZONE_RISK_THRESHOLD and agg["flagged_count"] >= _DEAD_ZONE_MIN_ORDERS)
        )

        if not passes_gate:
            continue

        flagged_zones[zid] = {
            "zone_id":       zid,
            "zone_name":     agg["zone_name"],
            "dest_city":     agg["dest_city"],
            "avg_risk":      round(avg_risk, 4),
            "max_risk":      round(max_risk, 4),
            "flagged_count": agg["flagged_count"],
            "total_count":   agg["total_count"],
            "max_stranding": max_strand,
        }

        try:
            await conn.execute(
                """
                INSERT INTO dead_zone_snapshots
                    (id, zone_id, cycle_id, risk_level, expected_stranding_mins, timestamp)
                VALUES ($1, $2::uuid, $3::uuid, $4, $5, NOW())
                """,
                str(uuid.uuid4()),
                zid,
                cycle_id,
                avg_risk,
                max_strand,
            )
            snapshots_written += 1
        except Exception as exc:
            log.warning("dead_zone_snapshot_failed", zone_id=zid, error=str(exc))

    log.info(
        "dead_run_scores_written",
        order_rows=scores_written,
        zone_snapshots=snapshots_written,
        flagged_zones=len(flagged_zones),
    )

    return {
        **state,
        "scores_written":        scores_written,
        "zone_snapshots_written": snapshots_written,
        "flagged_zones":         flagged_zones,
    }


# ══════════════════════════════════════════════════════════════
# Node 4 — create_alerts
# ══════════════════════════════════════════════════════════════

async def _check_rider_cooldown(rider_id: str, zone_id: str, conn) -> bool:
    """Return True if an unresolved dead_zone_risk alert for this (rider, zone) < 30 min."""
    row = await conn.fetchrow(
        """
        SELECT id FROM rider_alerts
        WHERE rider_id                          = $1::uuid
          AND metadata_json->>'delivery_zone_id' = $2
          AND alert_type                         = 'dead_zone_risk'
          AND is_resolved                        = FALSE
          AND created_at                         > NOW() - INTERVAL '30 minutes'
        LIMIT 1
        """,
        rider_id, zone_id,
    )
    return row is not None


async def _check_operator_cooldown(zone_id: str, conn) -> bool:
    """Return True if an unresolved dead_zone_risk operator alert for this zone < 30 min."""
    row = await conn.fetchrow(
        """
        SELECT id FROM operator_alerts
        WHERE metadata_json->>'zone_id' = $1
          AND alert_type                = 'dead_zone_risk'
          AND is_resolved               = FALSE
          AND created_at                > NOW() - INTERVAL '30 minutes'
        LIMIT 1
        """,
        zone_id,
    )
    return row is not None


async def _create_alerts(state: DeadRunState, conn) -> DeadRunState:
    """
    rider_alerts (type: dead_zone_risk):
      - Only for is_high_risk orders where for_rider_alert=True (assigned, rider_inbound)
      - Cooldown key: (rider_id, delivery_zone_id) — suppresses same rider+zone,
        but different zones on the same rider each get their own alert
      - Severity: "critical" if session_dead_runs >= 1, else "warning"
      - Persona-aware message with session escalation note

    operator_alerts (type: dead_zone_risk):
      - One per evidence-gated zone (from flagged_zones built in write_scores)
      - Cooldown key: (zone_id)
      - Aggregate stats: flagged/total orders, avg risk, max stranding

    system_dead_zone_pressure (type: system_dead_zone_pressure):
      - Single operator alert if flagged / total_scored >= 0.50
      - Bypasses per-zone evidence gate — city-level collapse signal
      - Written regardless of zone-level cooldowns
    """
    cycle_id      = state["cycle_id"]
    scored        = state["scored"]
    flagged_zones = state["flagged_zones"]

    rider_created    = 0
    operator_created = 0
    cooldown_skipped = 0

    # ── Rider alerts (assigned + rider_inbound, high-risk, no ML failure) ──
    alertable = [
        s for s in scored
        if s["is_high_risk"] and s["for_rider_alert"] and not s.get("ml_failed")
    ]

    for s in alertable:
        if await _check_rider_cooldown(s["rider_id"], s["delivery_zone_id"], conn):
            cooldown_skipped += 1
            log.debug("rider_cooldown_active",
                      rider_id=s["rider_id"], zone_id=s["delivery_zone_id"])
            continue

        severity = "critical" if s["session_dead_runs"] >= 1 else "medium"

        try:
            await conn.execute(
                """
                INSERT INTO rider_alerts
                    (id, rider_id, cycle_id, alert_type, severity,
                     message, metadata_json, is_resolved, created_at)
                VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, FALSE, NOW())
                """,
                str(uuid.uuid4()),
                s["rider_id"],
                cycle_id,
                "dead_zone_risk",
                severity,
                _rider_alert_message(s),
                json.dumps({
                    "order_id":                s["order_id"],
                    "delivery_zone_id":        s["delivery_zone_id"],
                    "dest_zone_name":          s["dest_zone_name"],
                    "dead_zone_probability":   s["dead_zone_probability"],
                    "expected_stranding_mins": s.get("expected_stranding_mins"),
                    "earnings_lost_rs":        s.get("earnings_lost_rs"),
                    "session_dead_runs":       s["session_dead_runs"],
                    "rider_persona":           s["rider_persona"],
                }),
            )
            rider_created += 1
        except Exception as exc:
            log.warning("rider_alert_failed", rider_id=s["rider_id"], error=str(exc))

    # ── Operator alerts: one per evidence-gated zone ─────────
    for zid, agg in flagged_zones.items():
        if await _check_operator_cooldown(zid, conn):
            cooldown_skipped += 1
            log.debug("operator_cooldown_active", zone_id=zid)
            continue

        zone_severity = (
            "critical" if agg["avg_risk"] >= _DEAD_ZONE_HIGH_THRESHOLD else "warning"
        )
        ops_msg = (
            f"{agg['zone_name']} ({agg['dest_city']}): "
            f"{agg['flagged_count']}/{agg['total_count']} orders flagged, "
            f"avg risk={agg['avg_risk']:.2f}"
            + (f", max stranding ~{agg['max_stranding']:.0f}min"
               if agg["max_stranding"] else "")
            + "."
        )

        try:
            await conn.execute(
                """
                INSERT INTO operator_alerts
                    (id, cycle_id, agent_name, alert_type, severity,
                     title, message, metadata_json, is_resolved, created_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, FALSE, NOW())
                """,
                str(uuid.uuid4()),
                cycle_id,
                "DeadRunAgent",
                "dead_zone_risk",
                zone_severity,
                f"Dead zone risk: {agg['zone_name']}",
                ops_msg,
                json.dumps(agg),
            )
            operator_created += 1
        except Exception as exc:
            log.warning("operator_alert_failed", zone_id=zid, error=str(exc))

    # ── System pressure detection ─────────────────────────────
    total_scored  = len(scored)
    flagged_count = sum(1 for s in scored if s["is_high_risk"])
    system_pressure = (
        total_scored > 0
        and flagged_count / total_scored >= _SYSTEM_PRESSURE_RATIO
    )

    if system_pressure:
        try:
            await conn.execute(
                """
                INSERT INTO operator_alerts
                    (id, cycle_id, agent_name, alert_type, severity,
                     title, message, metadata_json, is_resolved, created_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, FALSE, NOW())
                """,
                str(uuid.uuid4()),
                cycle_id,
                "DeadRunAgent",
                "system_dead_zone_pressure",
                "critical",
                "Systemic dead zone pressure detected",
                (
                    f"{flagged_count}/{total_scored} active orders flagged for dead zone risk "
                    f"across {len(flagged_zones)} zone(s). "
                    f"Platform-wide demand may be critically low."
                ),
                json.dumps({
                    "flagged_orders": flagged_count,
                    "total_orders":   total_scored,
                    "flagged_ratio":  round(flagged_count / total_scored, 3),
                    "affected_zones": len(flagged_zones),
                }),
            )
            operator_created += 1
            log.warning(
                "system_dead_zone_pressure",
                flagged=flagged_count, total=total_scored, zones=len(flagged_zones),
            )
        except Exception as exc:
            log.warning("system_pressure_alert_failed", error=str(exc))

    log.info(
        "dead_run_alerts_done",
        rider_created=rider_created,
        operator_created=operator_created,
        cooldown_skipped=cooldown_skipped,
        system_pressure=system_pressure,
    )

    return {
        **state,
        "rider_alerts_created":   rider_created,
        "operator_alerts_created": operator_created,
        "cooldown_skipped":        cooldown_skipped,
        "system_pressure":         system_pressure,
    }


# ══════════════════════════════════════════════════════════════
# Node 5 — synthesize
# ══════════════════════════════════════════════════════════════

async def _synthesize(state: DeadRunState) -> DeadRunState:
    """
    Compute total_earnings_at_risk_rs — sum of persona-adjusted earnings_lost_rs
    across all high-risk orders. This is the Supervisor's headline KPI.

    LLM called once with top-N zones in compact format (zone-level, not order-level).
    Falls back to template string if LLM returns empty.

    status="partial" if ML failures exceed 50% of scored orders.

    output_json is structured for predictable Supervisor reads — all key metrics
    are top-level fields, not buried in narrative strings.
    """
    scored        = state["scored"]
    flagged_zones = state["flagged_zones"]
    ml_failures   = state.get("ml_failures", 0)

    total_earnings_at_risk = sum(
        s["earnings_lost_rs"]
        for s in scored
        if s["is_high_risk"] and s.get("earnings_lost_rs") is not None
    )
    flagged_orders  = sum(1 for s in scored if s["is_high_risk"])
    alert_count     = (state.get("rider_alerts_created", 0)
                       + state.get("operator_alerts_created", 0))
    system_pressure = state.get("system_pressure", False)

    # Overall severity
    if system_pressure or any(
        v["avg_risk"] >= _DEAD_ZONE_HIGH_THRESHOLD for v in flagged_zones.values()
    ):
        overall_severity = "critical"
    elif flagged_zones:
        overall_severity = "warning"
    else:
        overall_severity = "normal"

    # ── LLM narrative ─────────────────────────────────────────
    top_zones = sorted(
        flagged_zones.values(), key=lambda z: z["avg_risk"], reverse=True
    )[:_TOP_N_TO_LLM]

    if top_zones:
        lines = []
        for i, z in enumerate(top_zones, 1):
            line = (
                f"{i}. {z['zone_name']} ({z['dest_city']}): "
                f"avg_risk={z['avg_risk']:.2f}, "
                f"{z['flagged_count']}/{z['total_count']} orders flagged"
            )
            if z["max_stranding"]:
                line += f", max stranding ~{z['max_stranding']:.0f}min"
            lines.append(line)

        pressure_note = (
            " NOTE: systemic dead zone pressure detected this cycle."
            if system_pressure else ""
        )
        prompt = (
            "You are an operations assistant for a food delivery platform. "
            f"Write a 2-3 sentence dispatch briefing about dead zone risk this cycle.{pressure_note}\n\n"
            + "\n".join(lines)
            + "\n\nBe specific, professional, and action-oriented. No bullet points."
        )

        narrative = await call_llm(prompt, max_tokens=150, temperature=0.2)

        if not narrative:
            names     = ", ".join(z["zone_name"] for z in top_zones[:3])
            narrative = (
                f"{flagged_orders} order(s) flagged across {len(flagged_zones)} zone(s). "
                f"Highest-risk: {names}. "
                f"Rs.{total_earnings_at_risk:.0f} total EPH at risk this cycle."
            )
    else:
        narrative = "No significant dead zone risk detected this cycle."

    ml_note = f", {ml_failures} ML failures" if ml_failures > 0 else ""
    summary_text = (
        f"DeadRunAgent: {len(scored)} scored, {flagged_orders} flagged, "
        f"{len(flagged_zones)} zones, Rs.{total_earnings_at_risk:.0f} EPH at risk"
        f"{ml_note}"
    )

    return {
        **state,
        "llm_narrative":           narrative,
        "summary_text":            summary_text,
        "alert_count":             alert_count,
        "severity":                overall_severity,
        "status":                  "partial" if ml_failures > len(scored) * 0.5 else "success",
        "total_earnings_at_risk_rs": round(total_earnings_at_risk, 2),
    }


# ══════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════

def _build_graph(conn, redis):
    """
    Compile the LangGraph StateGraph.
    conn and redis injected into nodes via closure.
    score_orders and synthesize have no external deps beyond state + ml_client/llm.
    """
    g = StateGraph(DeadRunState)

    async def fetch_orders(state):  return await _fetch_orders(state, conn, redis)
    async def write_scores(state):  return await _write_scores(state, conn)
    async def create_alerts(state): return await _create_alerts(state, conn)

    g.add_node("fetch_orders",  fetch_orders)
    g.add_node("score_orders",  _score_orders)
    g.add_node("write_scores",  write_scores)
    g.add_node("create_alerts", create_alerts)
    g.add_node("synthesize",    _synthesize)

    g.set_entry_point("fetch_orders")
    g.add_edge("fetch_orders",  "score_orders")
    g.add_edge("score_orders",  "write_scores")
    g.add_edge("write_scores",  "create_alerts")
    g.add_edge("create_alerts", "synthesize")
    g.add_edge("synthesize",    END)

    return g.compile()


# ══════════════════════════════════════════════════════════════
# Agent class
# ══════════════════════════════════════════════════════════════

class DeadRunAgent(BaseAgent):

    async def run(self, cycle_id: str, **kwargs) -> dict[str, Any]:
        t = time.monotonic()

        try:
            graph = _build_graph(self.conn, self.redis)
            initial_state: DeadRunState = {"cycle_id": cycle_id}
            final_state = await graph.ainvoke(initial_state)

            scored = final_state.get("scored", [])
            result = {
                "status":                    final_state.get("status",          "partial"),
                "summary_text":              final_state.get("summary_text",    "DeadRunAgent completed"),
                "alert_count":               final_state.get("alert_count",     0),
                "severity":                  final_state.get("severity",        "normal"),
                # Supervisor headline KPI
                "total_earnings_at_risk_rs": final_state.get("total_earnings_at_risk_rs", 0.0),
                # Counts
                "total_orders_scored":       len(scored),
                "flagged_orders":            sum(1 for s in scored if s.get("is_high_risk")),
                "flagged_zones":             len(final_state.get("flagged_zones", {})),
                "scores_written":            final_state.get("scores_written",  0),
                "zone_snapshots_written":    final_state.get("zone_snapshots_written", 0),
                "rider_alerts_created":      final_state.get("rider_alerts_created",   0),
                "operator_alerts_created":   final_state.get("operator_alerts_created", 0),
                "cooldown_skipped":          final_state.get("cooldown_skipped",        0),
                # Diagnostic
                "ml_failures":               final_state.get("ml_failures",     0),
                "system_pressure":           final_state.get("system_pressure", False),
                "llm_narrative":             final_state.get("llm_narrative",   ""),
            }
            status = final_state.get("status", "partial")

        except Exception as exc:
            self.log.error("dead_run_agent_failed", error=str(exc), exc_info=True)
            result = {
                "status":                    "failed",
                "summary_text":              f"DeadRunAgent failed: {exc}",
                "alert_count":               0,
                "severity":                  "normal",
                "total_earnings_at_risk_rs": 0.0,
                "total_orders_scored":       0,
                "flagged_orders":            0,
                "flagged_zones":             0,
                "scores_written":            0,
                "zone_snapshots_written":    0,
                "rider_alerts_created":      0,
                "operator_alerts_created":   0,
                "cooldown_skipped":          0,
                "ml_failures":               0,
                "system_pressure":           False,
                "llm_narrative":             "",
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
