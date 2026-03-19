"""
ARIA — Algorithmic Module: Zone Computation
============================================
Pure Python. No ML. No side effects (read-only).

Functions:
  compute_zone_density    — current density snapshot for a zone
  compute_sister_zones    — density-ranked sister zones for a zone
  compute_zone_stress     — stress ratio vs same-hour historical baseline
  compute_dead_zone_map   — dead zone classification for all active zones

Design:
  Algorithms handle all numerical computation here.
  The Zone Intelligence Agent calls these tools, reads the results,
  and uses the LLM only for: ambiguous zone tradeoffs, explanation
  generation, and context-aware recommendations.

Constants sourced from Loadshare 2023 research:
  - 5km psychological barrier for "long distance"
  - Sister zones = 2-3 dense zones within 6-7km radius
  - stress_ratio > 1.2 = stressed, < 0.5 = dead zone
"""

import math
import os
from datetime import datetime, timezone

import structlog

log = structlog.get_logger()

# ── Constants ─────────────────────────────────────────────────
# stress_ratio < this → zone is a dead zone (no return orders likely)
DEAD_ZONE_STRESS_THRESHOLD = float(os.getenv("DEAD_ZONE_STRESS_THRESHOLD", "0.5"))
# stress_ratio > this → zone is over-demanded (riders competing for orders)
STRESSED_ZONE_THRESHOLD    = float(os.getenv("STRESSED_ZONE_THRESHOLD", "1.2"))
# Snapshots older than this are flagged as stale (cycle fires every 15 min)
STALE_SNAPSHOT_MINS        = 20


# ── Helpers ───────────────────────────────────────────────────

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine great-circle distance in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _stress_level(stress_ratio: float) -> str:
    if stress_ratio < DEAD_ZONE_STRESS_THRESHOLD:
        return "dead"
    if stress_ratio < 0.8:
        return "low"
    if stress_ratio < STRESSED_ZONE_THRESHOLD:
        return "normal"
    return "high"


# ══════════════════════════════════════════════════════════════
# FUNCTION 1 — compute_zone_density
# ══════════════════════════════════════════════════════════════

async def compute_zone_density(zone_id: str, conn) -> dict:
    """
    Get the most recent density snapshot for a zone.

    Returns the latest row from zone_density_snapshots.
    Marks is_stale=True if the snapshot is older than STALE_SNAPSHOT_MINS.

    Returns:
        zone_id, density_score, order_count, active_rider_count,
        stress_ratio, timestamp_iso, is_stale
    """
    row = await conn.fetchrow(
        """
        SELECT density_score, order_count, active_rider_count,
               stress_ratio, timestamp
        FROM zone_density_snapshots
        WHERE zone_id = $1
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        zone_id,
    )

    if row is None:
        log.debug("no density snapshot found", zone_id=zone_id)
        return {
            "zone_id":            zone_id,
            "density_score":      0.0,
            "order_count":        0,
            "active_rider_count": 0,
            "stress_ratio":       0.0,
            "stress_level":       "dead",
            "timestamp_iso":      None,
            "is_stale":           True,
        }

    now = datetime.now(timezone.utc)
    age_mins = (now - row["timestamp"]).total_seconds() / 60
    density   = float(row["density_score"]  or 0.0)
    s_ratio   = float(row["stress_ratio"]   or 0.0)

    return {
        "zone_id":            zone_id,
        "density_score":      round(density, 4),
        "order_count":        int(row["order_count"]),
        "active_rider_count": int(row["active_rider_count"]),
        "stress_ratio":       round(s_ratio, 4),
        "stress_level":       _stress_level(s_ratio),
        "timestamp_iso":      row["timestamp"].isoformat(),
        "is_stale":           age_mins > STALE_SNAPSHOT_MINS,
    }


# ══════════════════════════════════════════════════════════════
# FUNCTION 2 — compute_sister_zones
# ══════════════════════════════════════════════════════════════

async def compute_sister_zones(zone_id: str, conn) -> list[dict]:
    """
    Return density-ranked sister zones for a given zone.

    Reads static sister_zone_ids from the zones table (seeded in
    seed_zones.sql), then fetches the latest density snapshot for
    each. Ranks by density descending (most orders = best for rider),
    with distance as a tie-breaker (closer = better).

    This is what replaces Loadshare's static Sister Zone Revolution —
    the ranking is live, not hardcoded.

    Returns:
        List of sister zone dicts ordered by rank, each with:
        zone_id, name, city, density_score, order_count,
        stress_ratio, stress_level, distance_km, rank
    """
    zone_row = await conn.fetchrow(
        """
        SELECT centroid_lat, centroid_lng, sister_zone_ids
        FROM zones
        WHERE id = $1 AND is_active = TRUE
        """,
        zone_id,
    )

    if zone_row is None or not zone_row["sister_zone_ids"]:
        log.debug("no sister zones configured", zone_id=zone_id)
        return []

    sister_ids  = zone_row["sister_zone_ids"]
    home_lat    = float(zone_row["centroid_lat"])
    home_lng    = float(zone_row["centroid_lng"])

    # Fetch sister zone details + latest density in one query
    rows = await conn.fetch(
        """
        SELECT z.id, z.name, z.city, z.centroid_lat, z.centroid_lng,
               COALESCE(d.density_score, 0.0)  AS density_score,
               COALESCE(d.order_count, 0)       AS order_count,
               COALESCE(d.stress_ratio, 0.0)    AS stress_ratio
        FROM zones z
        LEFT JOIN LATERAL (
            SELECT density_score, order_count, stress_ratio
            FROM zone_density_snapshots
            WHERE zone_id = z.id
            ORDER BY timestamp DESC
            LIMIT 1
        ) d ON TRUE
        WHERE z.id = ANY($1) AND z.is_active = TRUE
        """,
        sister_ids,
    )

    sisters = []
    for row in rows:
        dist_km = _haversine_km(
            home_lat, home_lng,
            float(row["centroid_lat"]), float(row["centroid_lng"]),
        )
        s_ratio = float(row["stress_ratio"])
        sisters.append({
            "zone_id":       str(row["id"]),
            "name":          row["name"],
            "city":          row["city"],
            "density_score": round(float(row["density_score"]), 4),
            "order_count":   int(row["order_count"]),
            "stress_ratio":  round(s_ratio, 4),
            "stress_level":  _stress_level(s_ratio),
            "distance_km":   round(dist_km, 2),
        })

    # Sort: highest density first, distance as tie-breaker
    sisters.sort(key=lambda z: (-z["density_score"], z["distance_km"]))
    for i, s in enumerate(sisters):
        s["rank"] = i + 1

    return sisters


# ══════════════════════════════════════════════════════════════
# FUNCTION 3 — compute_zone_stress
# ══════════════════════════════════════════════════════════════

async def compute_zone_stress(zone_id: str, conn) -> dict:
    """
    Compute the stress ratio for a zone vs its historical baseline.

    stress_ratio = current_density / historical_avg_density
    (for the same hour of day, averaged over the last 28 days)

    This replaces a point-in-time density reading with a contextual
    signal — a density of 0.4 at 2pm on a Tuesday means something
    different from a density of 0.4 at 8pm on a Friday.

    Historical baseline comes from the zone_density_hourly continuous
    aggregate (pre-computed by TimescaleDB, no cold aggregation).

    Returns:
        zone_id, current_density, baseline_density, stress_ratio,
        stress_level, is_dead_zone, is_stressed
    """
    now_utc      = datetime.now(timezone.utc)
    current_hour = now_utc.hour

    # Current density
    current_row = await conn.fetchrow(
        """
        SELECT density_score, order_count
        FROM zone_density_snapshots
        WHERE zone_id = $1
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        zone_id,
    )
    current_density = float(current_row["density_score"] or 0.0) if current_row else 0.0

    # Historical baseline: same hour of day, last 28 days
    # Uses the zone_density_hourly continuous aggregate
    baseline_row = await conn.fetchrow(
        """
        SELECT AVG(avg_density) AS baseline_density
        FROM zone_density_hourly
        WHERE zone_id = $1
          AND EXTRACT(HOUR FROM bucket) = $2
          AND bucket > NOW() - INTERVAL '28 days'
        """,
        zone_id,
        current_hour,
    )
    baseline_density = (
        float(baseline_row["baseline_density"])
        if baseline_row and baseline_row["baseline_density"] is not None
        else None
    )

    # Compute stress ratio
    if baseline_density and baseline_density > 0.0:
        stress_ratio = current_density / baseline_density
    elif current_density > 0.0:
        # No historical data — use raw density as proxy
        # 0.5 is the neutral reference point
        stress_ratio = current_density / 0.5
    else:
        stress_ratio = 0.0

    stress_ratio = round(stress_ratio, 4)
    is_dead      = stress_ratio < DEAD_ZONE_STRESS_THRESHOLD
    is_stressed  = stress_ratio > STRESSED_ZONE_THRESHOLD

    return {
        "zone_id":          zone_id,
        "current_density":  round(current_density, 4),
        "baseline_density": round(baseline_density, 4) if baseline_density is not None else None,
        "stress_ratio":     stress_ratio,
        "stress_level":     _stress_level(stress_ratio),
        "is_dead_zone":     is_dead,
        "is_stressed":      is_stressed,
        "hour_of_day":      current_hour,
    }


# ══════════════════════════════════════════════════════════════
# FUNCTION 4 — compute_dead_zone_map
# ══════════════════════════════════════════════════════════════

async def compute_dead_zone_map(conn) -> list[dict]:
    """
    Classify all active zones as dead / low / normal / high demand.

    Reads the latest density snapshot for every active zone in a
    single LATERAL join query. Dead zone risk_level is continuous
    (0.0–1.0) for the frontend heatmap overlay.

    Dead zone risk logic:
      - stress_ratio == 0 or no snapshot → risk_level = 1.0 (worst case)
      - stress_ratio < DEAD_ZONE_STRESS_THRESHOLD → risk scales from 0–1
      - stress_ratio ≥ DEAD_ZONE_STRESS_THRESHOLD → risk_level = 0.0

    Returns:
        List of zone dicts sorted by risk_level descending (deadest first).
        Each: zone_id, name, city, density_score, stress_ratio,
              order_count, stress_level, is_dead_zone, risk_level
    """
    rows = await conn.fetch(
        """
        SELECT z.id, z.name, z.city,
               COALESCE(d.density_score, 0.0) AS density_score,
               COALESCE(d.stress_ratio,  0.0) AS stress_ratio,
               COALESCE(d.order_count,   0)   AS order_count
        FROM zones z
        LEFT JOIN LATERAL (
            SELECT density_score, stress_ratio, order_count
            FROM zone_density_snapshots
            WHERE zone_id = z.id
            ORDER BY timestamp DESC
            LIMIT 1
        ) d ON TRUE
        WHERE z.is_active = TRUE
        """
    )

    result = []
    for row in rows:
        s_ratio   = float(row["stress_ratio"])
        is_dead   = s_ratio < DEAD_ZONE_STRESS_THRESHOLD

        if s_ratio == 0.0:
            risk_level = 1.0
        elif is_dead:
            # Linear scale: stress_ratio=0 → risk=1.0, threshold → risk=0.0
            risk_level = max(0.0, 1.0 - (s_ratio / DEAD_ZONE_STRESS_THRESHOLD))
        else:
            risk_level = 0.0

        result.append({
            "zone_id":       str(row["id"]),
            "name":          row["name"],
            "city":          row["city"],
            "density_score": round(float(row["density_score"]), 4),
            "stress_ratio":  round(s_ratio, 4),
            "order_count":   int(row["order_count"]),
            "stress_level":  _stress_level(s_ratio),
            "is_dead_zone":  is_dead,
            "risk_level":    round(risk_level, 4),
        })

    result.sort(key=lambda z: -z["risk_level"])
    return result
