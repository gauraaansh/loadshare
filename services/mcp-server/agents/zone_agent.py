"""
ARIA — Zone Intelligence Agent
================================
LangGraph-powered agent. Runs every 15-minute cycle.

Responsibility:
  Classify all 180 active zones by demand state (dead/low/normal/stressed),
  write zone_stress_snapshots for the frontend heatmap, and generate
  per-rider zone repositioning recommendations for riders stuck in dead or
  low-density zones.

  This is the direct implementation of Loadshare's "Sister Zone Revolution"
  from their 2023 research — dynamic zone routing that replaced static
  assignments that were stranding riders in peripheral zones with no return orders.

Pipeline (5 nodes):
  fetch_state → classify_zones → write_zone_snapshots → write_recommendations → synthesize

Key design decisions:

  Single bulk fetch — no per-zone or per-rider DB queries:
    One LATERAL JOIN query fetches all 180 zones with latest snapshot + metadata
    (name, city, centroid, zone_type, sister_zone_ids, density, stress_ratio,
    order_count, order_delta). Sister zone ranking happens in Python from
    in-memory data — no per-zone compute_sister_zones() calls.
    compute_dead_zone_map() and compute_zone_stress() are per-zone DB functions
    useful for one-off queries (tools/router.py) but not for a 180-zone
    full-cycle pass. This agent replaces them with a single query.

  Redis for rider state, not DB:
    event-stream updates aria:rider_state:{rider_id} on every status change
    (idle → assigned → etc.) in real time. DB rider_sessions only gets a
    write at session open/close. For knowing if a rider is idle RIGHT NOW,
    Redis is the authoritative source.

  Freshness gate before classification:
    If a zone's snapshot_ts is older than STALE_SNAPSHOT_MINS (20 min),
    the zone is marked "unknown" — skipped for rider recommendations and
    zone_stress_snapshot writes. Prevents bad advice from stale data when
    the event-stream is down or behind.

  Two-threshold design (hysteresis-lite, stateless):
    DEAD_ZONE_STRESS_THRESHOLD (0.5) — used for is_dead_zone DB record.
      Matches algo module, consistent with rest of codebase.
    DEAD_ZONE_RECOMMENDATION_THRESHOLD (0.45) — stricter gate for triggering
      rider move recommendations. A zone at 0.47 is technically dead but
      borderline — don't flip-flop riders with move/stay recommendations
      every cycle. This requires no historical state, zero extra queries.

  Sister zone ranking — in-memory Python, not per-zone DB calls:
    For each dead/low zone: look up sister_zone_ids (already in memory),
    look up density for each sister (also in memory), filter by:
      - Same city (cross-city moves are never correct)
      - Sister not itself dead (stress_ratio > 0.45)
      - Freshness gate passes for sister
      - density_gain >= RECOMMENDATION_DENSITY_GAIN (0.10) after zone_type multiplier
      - distance_km <= RECOMMENDATION_MAX_KM (7.0km)
    Sort by adjusted_density (density × zone_type_multiplier). Take top 2.

  Zone type multipliers — smaller spread than intuitive:
    hub 1.15, commercial 1.05, residential 1.0, peripheral 0.9.
    Wider multipliers (1.3/1.1/1.0/0.8) can let a hub at density 0.3
    outrank a residential at 0.6 — wrong, the 0.6 zone has double the
    actual order activity. The tighter spread balances quality signal
    against real activity volume.

  Three-tier urgency for recommendations:
    "monitor"      — dead zone with order_delta > 0 (orders appearing, may recover)
    "immediate"    — idle rider in dead zone with order_delta <= 0
    "post_delivery" — engaged rider in dead zone, or low-zone idle rider

  Stressed zone alerts gate: stress_ratio > 1.2 AND order_delta > 0:
    A zone at 1.3 stress during evening peak every day isn't surging —
    it's just its structural state. order_delta > 0 means demand is
    actively growing right now. Filters structural high-stress from
    genuine surges. Better signal-to-noise for operator alerts.

  Recommendation rationale — template, not LLM per rider:
    Calling LLM 150 times (one per active rider) is not viable. The
    per-rider rationale is a populated template string with zone name,
    stress state, distance, and expected_impact (~N orders/hr gain).
    LLM is called once for the cycle-level operator briefing.

  No ML server call:
    Zone classification is entirely algorithmic — stress_ratio from the
    historical baseline (computed by zone_engine.py) is the signal.
    There is no ML model for zone intelligence. The Loadshare article's
    zone insight was about density patterns, not ML prediction.

  No rider_alerts for zone recommendations:
    Zone Intelligence writes to zone_recommendations (cycle-level advisory
    table, one row per rider). Not rider_alerts — zone moves are advisory,
    not urgent warnings. rider_alerts are for immediate risks (dead zone
    stranding, restaurant delay, churn). The frontend reads zone_recommendations
    for the repositioning suggestion panel.

From Loadshare 2023 research:
  Static sister zone assignments were a primary cause of rider stranding.
  Peripheral zones with no return orders forced dead runs. The "Sister Zone
  Revolution" was about live-ranking alternative zones by current density —
  this agent implements that ranking dynamically each cycle.
"""

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
from algorithms.zone import (
    DEAD_ZONE_STRESS_THRESHOLD,
    STALE_SNAPSHOT_MINS,
    STRESSED_ZONE_THRESHOLD,
)
from llm import call_llm
from redis_client import key_active_riders, key_rider_state

log = structlog.get_logger()

# ── Tuning constants ──────────────────────────────────────────
# Separate from DEAD_ZONE_STRESS_THRESHOLD (0.5) — zones at 0.47 are
# technically dead but borderline; don't recommend moves from them.
_DEAD_ZONE_RECOMMENDATION_THRESHOLD = 0.45

# Minimum adjusted_density gain to justify recommending a sister zone move.
# Below this the benefit doesn't outweigh the travel cost.
_RECOMMENDATION_DENSITY_GAIN = 0.10

# Maximum distance for a sister zone recommendation.
# Matches the ~6-7km radius used by the v2 zone generator for sister seeding.
_RECOMMENDATION_MAX_KM = 7.0

# Stressed alert cooldown — one alert per zone per 30 min
_STRESSED_ALERT_COOLDOWN_MINS = 30

# Zone type multipliers for sister zone ranking (smaller spread — prevents
# zone_type from dominating over actual density signal)
_ZONE_TYPE_MULTIPLIERS: dict[str, float] = {
    "hub":         1.15,
    "commercial":  1.05,
    "residential": 1.0,
    "peripheral":  0.9,
}

# Top N zones per category shown to LLM
_TOP_DEAD_TO_LLM     = 3
_TOP_CITIES_TO_LLM   = 6


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


def _adjusted_density(zone: dict) -> float:
    """density_score × zone_type_multiplier for sister zone ranking."""
    mult = _ZONE_TYPE_MULTIPLIERS.get(zone.get("zone_type", "residential"), 1.0)
    return zone["density_score"] * mult


def _stress_level(stress_ratio: float) -> str:
    if stress_ratio < DEAD_ZONE_STRESS_THRESHOLD:   # < 0.5
        return "dead"
    if stress_ratio < 0.8:
        return "low"
    if stress_ratio < STRESSED_ZONE_THRESHOLD:       # < 1.2
        return "normal"
    return "stressed"


# ══════════════════════════════════════════════════════════════
# State schema
# ══════════════════════════════════════════════════════════════

class ZoneState(TypedDict, total=False):
    cycle_id:            str
    now:                 datetime

    # Node 1 output
    zones_dict:          dict    # zone_id → full zone data + latest snapshot fields
    active_rider_ids:    list    # rider_ids from Redis SMEMBERS
    rider_states:        dict    # rider_id → {home_zone_id, status}

    # Node 2 output (pure computation)
    classified_zones:    dict    # zone_id → zone data + stress_level + sister_ranking
    rider_recommendations: list  # [{rider_id, recommended_zone_ids, urgency, rationale}]
    stressed_zones:      list    # zones with stress_ratio > 1.2 AND order_delta > 0
    dead_zone_count:     int
    stale_zone_count:    int
    riders_in_dead_zones: int

    # Node 3 output
    snapshots_written:        int
    operator_alerts_written:  int

    # Node 4 output
    recommendations_written:  int

    # Node 5 output
    llm_narrative:        str
    summary_text:         str
    alert_count:          int
    severity:             str
    status:               str
    stressed_zone_count:  int
    riders_recommended:   int
    total_zones_classified: int
    cities_active:        list   # distinct city names with at least 1 classified zone


# ══════════════════════════════════════════════════════════════
# Node 1 — fetch_state
# ══════════════════════════════════════════════════════════════

async def _fetch_state(state: ZoneState, conn, redis) -> ZoneState:
    """
    Three operations — no per-zone or per-rider DB queries:

    DB LATERAL JOIN (single query):
      All active zones with latest zone_density_snapshot joined inline.
      Returns: zone metadata (name, city, centroid, zone_type, city_tier,
      sister_zone_ids) + snapshot fields (density_score, stress_ratio,
      order_count, order_delta, snapshot_ts). 180 rows, 1 round-trip.

    Redis SMEMBERS: aria:active_riders → set of online rider_ids.

    Redis pipeline: HGETALL aria:rider_state:{rider_id} for every active
      rider. Returns home_zone_id + current status. Single round-trip.
    """
    now = datetime.now(timezone.utc)

    zone_rows = await conn.fetch(
        """
        SELECT
            z.id                               AS zone_id,
            z.name,
            z.city,
            z.centroid_lat,
            z.centroid_lng,
            z.sister_zone_ids,
            z.boundary_geojson->>'zone_type'   AS zone_type,
            z.boundary_geojson->>'city_tier'   AS city_tier,
            COALESCE(d.density_score, 0.0)     AS density_score,
            COALESCE(d.stress_ratio,  0.0)     AS stress_ratio,
            COALESCE(d.order_count,   0)       AS order_count,
            COALESCE(d.order_delta,   0)       AS order_delta,
            d.timestamp                         AS snapshot_ts
        FROM zones z
        LEFT JOIN LATERAL (
            SELECT density_score, stress_ratio, order_count, order_delta, timestamp
            FROM zone_density_snapshots
            WHERE zone_id = z.id
            ORDER BY timestamp DESC
            LIMIT 1
        ) d ON TRUE
        WHERE z.is_active = TRUE
        """,
    )

    zones_dict: dict[str, dict] = {}
    for row in zone_rows:
        zid = str(row["zone_id"])
        zones_dict[zid] = {
            "zone_id":        zid,
            "name":           row["name"],
            "city":           row["city"],
            "centroid_lat":   float(row["centroid_lat"]),
            "centroid_lng":   float(row["centroid_lng"]),
            "sister_zone_ids": [str(s) for s in (row["sister_zone_ids"] or [])],
            "zone_type":      row["zone_type"] or "residential",
            "city_tier":      row["city_tier"] or "Metropolitan",
            "density_score":  float(row["density_score"]),
            "stress_ratio":   float(row["stress_ratio"]),
            "order_count":    int(row["order_count"]),
            "order_delta":    int(row["order_delta"]),
            "snapshot_ts":    row["snapshot_ts"],
        }

    # Active rider IDs from Redis SET
    raw_ids = await redis.smembers(key_active_riders())
    active_rider_ids = list(raw_ids)

    # Rider states via pipeline — real-time status, fresher than DB sessions
    rider_states: dict[str, dict] = {}
    if active_rider_ids:
        async with redis.pipeline(transaction=False) as pipe:
            for rid in active_rider_ids:
                pipe.hgetall(key_rider_state(rid))
            results = await pipe.execute()

        for rid, data in zip(active_rider_ids, results):
            if data:
                rider_states[rid] = {
                    "home_zone_id":    data.get("home_zone_id", ""),
                    "status":          data.get("status", "idle"),
                    "current_order_id": data.get("current_order_id", ""),
                }

    log.info(
        "zone_fetch_done",
        zones=len(zones_dict),
        active_riders=len(active_rider_ids),
        rider_states_found=len(rider_states),
    )

    return {
        **state,
        "now":             now,
        "zones_dict":      zones_dict,
        "active_rider_ids": active_rider_ids,
        "rider_states":    rider_states,
    }


# ══════════════════════════════════════════════════════════════
# Node 2 — classify_zones
# ══════════════════════════════════════════════════════════════

async def _classify_zones(state: ZoneState) -> ZoneState:
    """
    Pure computation — zero I/O. Everything from state.

    Phase A — Classify all zones:
      Apply freshness gate first. Stale zones marked "unknown", skipped
      for recommendations and snapshots. Non-stale zones get:
        stress_level: dead / low / normal / stressed
        is_dead_zone: stress_ratio < 0.50 (for DB record)
        is_stressed:  stress_ratio > 1.20 AND order_delta > 0 (surge signal)

    Phase B — Rank sister zones for dead/low zones (in-memory Python):
      For each dead/low zone: iterate sister_zone_ids, filter by
        same city, not dead (stress > 0.45), freshness, density_gain >= 0.10,
        distance <= 7km. Adjust density by zone_type multiplier.
        expected_impact = density_gain × 10 (≈ incremental orders/hr).
      Sort by adjusted_density descending. Top 2 kept.

    Phase C — Build rider recommendations:
      dead zone: recommend if stress_ratio < 0.45 (recommendation threshold)
        AND viable sisters exist.
      low zone: recommend only if rider is idle AND gain threshold passes.
      Urgency: monitor (dead + orders appearing) / immediate (idle, dead, worsening)
        / post_delivery (engaged rider or low zone).
    """
    zones_dict  = state["zones_dict"]
    rider_states = state["rider_states"]
    now          = state["now"]

    classified_zones: dict[str, dict] = {}
    stressed_zones:   list[dict]      = []
    dead_zone_count   = 0
    stale_zone_count  = 0

    # ── Phase A: Classify all zones ───────────────────────────
    for zid, z in zones_dict.items():
        # Freshness gate: snapshot_ts None = no data, age > 20 min = stale
        is_stale = True
        if z["snapshot_ts"] is not None:
            age_mins = (now.replace(tzinfo=timezone.utc) - z["snapshot_ts"].replace(tzinfo=timezone.utc)
                        if z["snapshot_ts"].tzinfo is None
                        else (now - z["snapshot_ts"])).total_seconds() / 60
            is_stale = age_mins > STALE_SNAPSHOT_MINS

        s_ratio    = z["stress_ratio"]
        is_dead    = s_ratio < DEAD_ZONE_STRESS_THRESHOLD and not is_stale
        is_stressed = (s_ratio > STRESSED_ZONE_THRESHOLD
                       and z["order_delta"] > 0
                       and not is_stale)
        sl = "unknown" if is_stale else _stress_level(s_ratio)

        cl = {
            **z,
            "stress_level":  sl,
            "is_dead_zone":  is_dead,
            "is_stale":      is_stale,
            "is_stressed":   is_stressed,
            "sister_ranking": [],
        }
        classified_zones[zid] = cl

        if is_stale:
            stale_zone_count += 1
        elif is_dead:
            dead_zone_count += 1

        if is_stressed:
            stressed_zones.append(cl)

    # ── Phase B: Rank sister zones (in-memory) ────────────────
    for zid, z in classified_zones.items():
        if z["is_stale"] or z["stress_level"] not in ("dead", "low"):
            continue
        if not z["sister_zone_ids"]:
            continue

        home_adj = _adjusted_density(z)
        sisters  = []

        for sid in z["sister_zone_ids"]:
            s = classified_zones.get(sid)
            if s is None or s["is_stale"]:
                continue
            if s["city"] != z["city"]:
                continue                             # never cross-city
            if s["stress_ratio"] < _DEAD_ZONE_RECOMMENDATION_THRESHOLD:
                continue                             # sister also dead
            s_adj = _adjusted_density(s)
            gain  = s_adj - home_adj
            if gain < _RECOMMENDATION_DENSITY_GAIN:
                continue
            dist = _haversine_km(
                z["centroid_lat"], z["centroid_lng"],
                s["centroid_lat"], s["centroid_lng"],
            )
            if dist > _RECOMMENDATION_MAX_KM:
                continue

            sisters.append({
                "zone_id":         sid,
                "name":            s["name"],
                "zone_type":       s["zone_type"],
                "density_score":   s["density_score"],
                "adj_density":     round(s_adj, 4),
                "stress_ratio":    s["stress_ratio"],
                "distance_km":     round(dist, 2),
                "density_gain":    round(gain, 4),
                "expected_impact": round(gain * 10, 1),
            })

        sisters.sort(key=lambda x: -x["adj_density"])
        z["sister_ranking"] = sisters[:2]

    # ── Phase C: Rider recommendations ───────────────────────
    rider_recommendations: list[dict] = []
    riders_in_dead_zones = 0

    for rid, rs in rider_states.items():
        home_zone_id = rs.get("home_zone_id", "")
        status       = rs.get("status", "idle")

        zone = classified_zones.get(home_zone_id)
        if zone is None or zone["is_stale"]:
            continue

        sl      = zone["stress_level"]
        is_idle = status == "idle"

        if sl == "dead":
            riders_in_dead_zones += 1

        # Recommendation eligibility
        should_recommend = False
        if sl == "dead" and zone["stress_ratio"] < _DEAD_ZONE_RECOMMENDATION_THRESHOLD:
            should_recommend = True
        elif sl == "low" and is_idle:
            should_recommend = True

        if not should_recommend:
            continue
        if not zone["sister_ranking"]:
            continue

        # Urgency
        if sl == "dead" and zone["order_delta"] > 0:
            urgency = "monitor"      # orders appearing — zone may recover, watch only
        elif is_idle and sl == "dead":
            urgency = "immediate"    # idle rider, dead zone, worsening
        else:
            urgency = "post_delivery"

        # Template rationale with expected_impact
        top = zone["sister_ranking"][0]
        rationale = (
            f"Zone {zone['name']} is {sl} "
            f"(stress={zone['stress_ratio']:.2f}, delta={zone['order_delta']:+d}). "
            f"Move to {top['name']} ({top['zone_type']}, "
            f"density={top['density_score']:.2f}, {top['distance_km']}km) "
            f"for ~+{top['expected_impact']} orders/hr."
        )
        if len(zone["sister_ranking"]) > 1:
            alt = zone["sister_ranking"][1]
            rationale += f" Alt: {alt['name']} ({alt['zone_type']}, {alt['distance_km']}km)."

        rider_recommendations.append({
            "rider_id":             rid,
            "home_zone_id":         home_zone_id,
            "home_zone_name":       zone["name"],
            "stress_level":         sl,
            "recommended_zone_ids": [s["zone_id"] for s in zone["sister_ranking"]],
            "urgency":              urgency,
            "rationale":            rationale,
        })

    log.info(
        "zone_classify_done",
        total=len(classified_zones),
        dead=dead_zone_count,
        stressed=len(stressed_zones),
        stale=stale_zone_count,
        riders_recommended=len(rider_recommendations),
        riders_in_dead=riders_in_dead_zones,
    )

    return {
        **state,
        "classified_zones":     classified_zones,
        "rider_recommendations": rider_recommendations,
        "stressed_zones":       stressed_zones,
        "dead_zone_count":      dead_zone_count,
        "stale_zone_count":     stale_zone_count,
        "riders_in_dead_zones": riders_in_dead_zones,
    }


# ══════════════════════════════════════════════════════════════
# Node 3 — write_zone_snapshots
# ══════════════════════════════════════════════════════════════

async def _check_operator_cooldown(zone_id: str, conn) -> bool:
    """Return True if a zone_stress operator alert for this zone exists < 30 min."""
    row = await conn.fetchrow(
        """
        SELECT id FROM operator_alerts
        WHERE metadata_json->>'zone_id' = $1
          AND alert_type                = 'zone_stress'
          AND is_resolved               = FALSE
          AND created_at                > NOW() - INTERVAL '30 minutes'
        LIMIT 1
        """,
        zone_id,
    )
    return row is not None


async def _write_zone_snapshots(state: ZoneState, conn) -> ZoneState:
    """
    zone_stress_snapshots: bulk executemany for all non-stale zones.
      Only non-stale zones written — stale zones have no fresh data to record.
      Frontend heatmap reads this; stale zones fall back to their last valid row.

    operator_alerts (type: zone_stress): one per stressed zone (stress > 1.2
      AND order_delta > 0). Cooldown by zone_id, 30 min.
      operator-facing only — no rider_alerts for stressed zones (riders in
      a stressed zone are about to get an order, no action needed from them).
    """
    cycle_id          = state["cycle_id"]
    classified_zones  = state["classified_zones"]
    stressed_zones    = state["stressed_zones"]

    dead_zone_count  = state.get("dead_zone_count", 0)
    total_zones      = len(classified_zones)

    # ── zone_stress_snapshots (bulk) ──────────────────────────
    rows = [
        (
            str(uuid.uuid4()),
            zid,
            cycle_id,
            z["stress_ratio"],
            z["density_score"],
            z["is_dead_zone"],
        )
        for zid, z in classified_zones.items()
        if not z["is_stale"]
    ]

    snapshots_written = 0
    if rows:
        try:
            await conn.executemany(
                """
                INSERT INTO zone_stress_snapshots
                    (id, zone_id, cycle_id, stress_ratio, density_score, is_dead_zone, timestamp)
                VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, NOW())
                """,
                rows,
            )
            snapshots_written = len(rows)
        except Exception as exc:
            log.warning("zone_snapshots_write_failed", error=str(exc))

    # ── Operator alerts for stressed zones ────────────────────
    operator_alerts_written = 0
    for z in stressed_zones:
        if await _check_operator_cooldown(z["zone_id"], conn):
            log.debug("zone_stress_cooldown", zone_id=z["zone_id"])
            continue

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
                "ZoneAgent",
                "zone_stress",
                "medium",
                f"Demand surge: {z['name']}",
                (
                    f"{z['name']} ({z['zone_type']}, {z['city']}): "
                    f"stress={z['stress_ratio']:.2f}, order_delta=+{z['order_delta']}, "
                    f"{z['order_count']} active orders. Supply may be insufficient."
                ),
                json.dumps({
                    "zone_id":      z["zone_id"],
                    "zone_name":    z["name"],
                    "city":         z["city"],
                    "zone_type":    z["zone_type"],
                    "stress_ratio": z["stress_ratio"],
                    "order_delta":  z["order_delta"],
                    "order_count":  z["order_count"],
                }),
            )
            operator_alerts_written += 1
        except Exception as exc:
            log.warning("zone_stress_alert_failed", zone_id=z["zone_id"], error=str(exc))

    # ── System zone pressure alert ─────────────────────────────
    # If >= 50% of all zones are dead, this is a supply-side network event —
    # one alert at the system level is more useful than N individual zone alerts.
    if total_zones > 0 and dead_zone_count / total_zones >= 0.50:
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
                "ZoneAgent",
                "system_zone_pressure",
                "critical",
                "System-wide zone pressure: supply shortage",
                (
                    f"{dead_zone_count}/{total_zones} zones are dead this cycle. "
                    "Widespread supply shortage — consider incentive deployment or "
                    "manual rider redistribution."
                ),
                json.dumps({
                    "dead_zone_count":  dead_zone_count,
                    "total_zones":      total_zones,
                    "dead_pct":         round(dead_zone_count / total_zones * 100, 1),
                }),
            )
            operator_alerts_written += 1
        except Exception as exc:
            log.warning("system_zone_pressure_alert_failed", error=str(exc))

    log.info(
        "zone_snapshots_written",
        snapshots=snapshots_written,
        op_alerts=operator_alerts_written,
        dead_pct=round(dead_zone_count / total_zones * 100, 1) if total_zones else 0,
    )

    return {
        **state,
        "snapshots_written":       snapshots_written,
        "operator_alerts_written": operator_alerts_written,
    }


# ══════════════════════════════════════════════════════════════
# Node 4 — write_recommendations
# ══════════════════════════════════════════════════════════════

async def _write_recommendations(state: ZoneState, conn) -> ZoneState:
    """
    zone_recommendations: one row per rider per cycle (cycle-level advisory
    snapshot, not an alert). recommended_zone_ids is a PostgreSQL UUID[].

    No cooldown — this is a fresh cycle record, not a deduped alert.
    If a rider's home zone is still dead next cycle, they get a fresh row —
    that's correct, the situation hasn't changed.

    Bulk insert via executemany. rationale is a template string (not LLM —
    calling LLM per rider is not viable at 150 active riders).
    """
    cycle_id = state["cycle_id"]
    recs     = state["rider_recommendations"]

    if not recs:
        return {**state, "recommendations_written": 0}

    now = datetime.now(timezone.utc)
    rows = []
    for rec in recs:
        rows.append((
            str(uuid.uuid4()),
            rec["rider_id"],
            cycle_id,
            rec["recommended_zone_ids"],   # pass as Python list; asyncpg maps to uuid[]
            rec["rationale"],
            now,
        ))

    written = 0
    try:
        await conn.executemany(
            """
            INSERT INTO zone_recommendations
                (id, rider_id, cycle_id, recommended_zone_ids, rationale, timestamp)
            VALUES ($1, $2::uuid, $3::uuid, $4::uuid[], $5, $6)
            """,
            rows,
        )
        written = len(rows)
    except Exception as exc:
        log.warning("zone_recommendations_write_failed", error=str(exc))

    log.info("zone_recommendations_written", count=written)
    return {**state, "recommendations_written": written}


# ══════════════════════════════════════════════════════════════
# Node 5 — synthesize
# ══════════════════════════════════════════════════════════════

async def _synthesize(state: ZoneState) -> ZoneState:
    """
    LLM called once with city-level summary + top 3 dead zones + top stressed zone.
    City grouping helps the Supervisor see geographic patterns (e.g. Mumbai
    peripheral zones clustering dead during off-peak). Top 3 specific zones
    keep the briefing actionable.

    Supervisor headline metrics:
      riders_in_dead_zones — count of active riders whose home zone is dead
        (the "stranded riders" number — directly from Loadshare research context)
      riders_recommended — riders who received repositioning advice this cycle
      dead_zone_count, stressed_zone_count — fleet health overview
    """
    classified_zones  = state["classified_zones"]
    stressed_zones    = state["stressed_zones"]
    recs              = state["rider_recommendations"]
    dead_zone_count   = state.get("dead_zone_count",     0)
    stale_zone_count  = state.get("stale_zone_count",    0)
    riders_in_dead    = state.get("riders_in_dead_zones", 0)

    riders_recommended    = len(recs)
    stressed_zone_count   = len(stressed_zones)
    total_zones           = len(classified_zones)
    alert_count           = state.get("operator_alerts_written", 0)

    # City-level stats for LLM
    city_stats: dict[str, dict] = defaultdict(
        lambda: {"dead": 0, "low": 0, "normal": 0, "stressed": 0, "unknown": 0}
    )
    for z in classified_zones.values():
        city_stats[z["city"]][z["stress_level"]] += 1

    city_lines = [
        f"{city}: {s['dead']} dead, {s['low']} low, {s['normal']} normal, {s['stressed']} stressed"
        for city, s in sorted(
            city_stats.items(),
            key=lambda x: -(x[1]["dead"] + x[1]["stressed"])
        )
    ][:_TOP_CITIES_TO_LLM]

    # Top 3 worst dead zones
    top_dead = sorted(
        [z for z in classified_zones.values() if z["is_dead_zone"] and not z["is_stale"]],
        key=lambda z: z["stress_ratio"],
    )[:_TOP_DEAD_TO_LLM]

    # Top stressed zone (highest stress_ratio among surging zones)
    top_stressed = (
        max(stressed_zones, key=lambda z: z["stress_ratio"])
        if stressed_zones else None
    )

    # ── LLM narrative ─────────────────────────────────────────
    if top_dead or top_stressed:
        dead_lines = [
            f"{i}. {z['name']} ({z['zone_type']}, {z['city']}): "
            f"stress={z['stress_ratio']:.2f}, delta={z['order_delta']:+d}"
            for i, z in enumerate(top_dead, 1)
        ]
        stressed_line = ""
        if top_stressed:
            stressed_line = (
                f"\nTop surge: {top_stressed['name']} ({top_stressed['zone_type']}, "
                f"{top_stressed['city']}): stress={top_stressed['stress_ratio']:.2f}, "
                f"delta=+{top_stressed['order_delta']}"
            )

        prompt = (
            "You are an operations assistant for a food delivery platform. "
            "Write a 2-3 sentence zone intelligence briefing for the dispatch team.\n\n"
            "City summary:\n" + "\n".join(city_lines) + "\n\n"
            "Top dead zones:\n" + "\n".join(dead_lines)
            + stressed_line
            + "\n\nBe specific, professional, action-oriented. No bullet points."
        )

        narrative = await call_llm(prompt, max_tokens=150, temperature=0.2)

        if not narrative:
            names = ", ".join(z["name"] for z in top_dead[:2]) if top_dead else "none"
            narrative = (
                f"{dead_zone_count} zone(s) dead this cycle. "
                f"Worst: {names}. "
                f"{riders_recommended} rider(s) advised to reposition."
            )
    else:
        narrative = "All zones operating within normal density parameters this cycle."

    # Overall severity
    if dead_zone_count > 10 or (top_stressed and top_stressed["stress_ratio"] > 1.5):
        overall_severity = "critical"
    elif dead_zone_count > 0 or stressed_zone_count > 0:
        overall_severity = "warning"
    else:
        overall_severity = "normal"

    # status="partial" if more than 30% of zones had stale snapshots —
    # Redis cache is lagging behind event-stream, cycle data is incomplete.
    if total_zones > 0 and stale_zone_count / total_zones > 0.30:
        cycle_status = "partial"
    else:
        cycle_status = "success"

    stale_note = f", {stale_zone_count} stale" if stale_zone_count > 0 else ""
    summary_text = (
        f"ZoneAgent: {total_zones} classified, {dead_zone_count} dead, "
        f"{stressed_zone_count} stressed, {riders_recommended} riders recommended"
        f"{stale_note}"
    )

    return {
        **state,
        "llm_narrative":         narrative,
        "summary_text":          summary_text,
        "alert_count":           alert_count,
        "severity":              overall_severity,
        "status":                cycle_status,
        "stressed_zone_count":   stressed_zone_count,
        "riders_recommended":    riders_recommended,
        "total_zones_classified": total_zones,
        "cities_active":         sorted(city_stats.keys()),
    }


# ══════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════

def _build_graph(conn, redis):
    """
    conn and redis injected via closure.
    classify_zones and synthesize have no external deps — pure state computation.
    """
    g = StateGraph(ZoneState)

    async def fetch_state(state):          return await _fetch_state(state, conn, redis)
    async def write_zone_snapshots(state): return await _write_zone_snapshots(state, conn)
    async def write_recommendations(state): return await _write_recommendations(state, conn)

    g.add_node("fetch_state",           fetch_state)
    g.add_node("classify_zones",        _classify_zones)
    g.add_node("write_zone_snapshots",  write_zone_snapshots)
    g.add_node("write_recommendations", write_recommendations)
    g.add_node("synthesize",            _synthesize)

    g.set_entry_point("fetch_state")
    g.add_edge("fetch_state",           "classify_zones")
    g.add_edge("classify_zones",        "write_zone_snapshots")
    g.add_edge("write_zone_snapshots",  "write_recommendations")
    g.add_edge("write_recommendations", "synthesize")
    g.add_edge("synthesize",            END)

    return g.compile()


# ══════════════════════════════════════════════════════════════
# Agent class
# ══════════════════════════════════════════════════════════════

class ZoneAgent(BaseAgent):

    async def run(self, cycle_id: str, **kwargs) -> dict[str, Any]:
        t = time.monotonic()

        try:
            graph = _build_graph(self.conn, self.redis)
            initial_state: ZoneState = {"cycle_id": cycle_id}
            final_state = await graph.ainvoke(initial_state)

            result = {
                "status":                 final_state.get("status",              "partial"),
                "summary_text":           final_state.get("summary_text",        "ZoneAgent completed"),
                "alert_count":            final_state.get("alert_count",         0),
                "severity":               final_state.get("severity",            "normal"),
                # Supervisor headline KPIs
                "dead_zone_count":        final_state.get("dead_zone_count",     0),
                "stressed_zone_count":    final_state.get("stressed_zone_count", 0),
                "stale_zone_count":       final_state.get("stale_zone_count",    0),
                "riders_in_dead_zones":   final_state.get("riders_in_dead_zones", 0),
                "riders_recommended":     final_state.get("riders_recommended",  0),
                "total_zones_classified": final_state.get("total_zones_classified", 0),
                # Cities active this cycle (for Supervisor RAG city filter)
                "cities_active":          final_state.get("cities_active",       []),
                # Counts
                "snapshots_written":      final_state.get("snapshots_written",   0),
                "recommendations_written": final_state.get("recommendations_written", 0),
                "operator_alerts_written": final_state.get("operator_alerts_written", 0),
                # Narrative
                "llm_narrative":          final_state.get("llm_narrative",       ""),
            }
            status = final_state.get("status", "partial")

        except Exception as exc:
            self.log.error("zone_agent_failed", error=str(exc), exc_info=True)
            result = {
                "status":                 "failed",
                "summary_text":           f"ZoneAgent failed: {exc}",
                "alert_count":            0,
                "severity":               "normal",
                "dead_zone_count":        0,
                "stressed_zone_count":    0,
                "stale_zone_count":       0,
                "riders_in_dead_zones":   0,
                "riders_recommended":     0,
                "total_zones_classified": 0,
                "cities_active":          [],
                "snapshots_written":      0,
                "recommendations_written": 0,
                "operator_alerts_written": 0,
                "llm_narrative":          "",
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
