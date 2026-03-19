"""
ARIA — Algorithmic Module: Restaurant + Order
==============================================
Pure Python. No ML. No side effects (read-only).

Functions:
  compute_restaurant_baseline  — historical delay baseline for a restaurant/time slot
  compute_delay_deviation      — actual vs baseline deviation with z-score
  get_active_pickups           — orders currently at pickup stage
  score_assignment             — assemble dead zone risk inputs for an order-rider pair

Design:
  The Restaurant Intelligence Agent does NOT use Model 2 to predict prep time.
  Instead:
    - Model 2 predicts TOTAL DELIVERY DURATION (baseline for a route/time)
    - compute_restaurant_baseline queries restaurant_delay_hourly continuous
      aggregate to get historical actual-vs-expected delay per restaurant
    - compute_delay_deviation detects consistent positive deviation = ripple signal
  Richer signal than either source alone — model provides route context,
  operational table provides restaurant-specific history.

  The score_assignment function assembles Model 3 inputs (dead zone risk).
  Note: zones table currently has no 'type' column. dest_zone_type_enc
  defaults to 1 (residential/mixed). Update when zone_type is added.

From Loadshare 2023 research:
  Certain restaurants consistently delayed prep, adding unpredictable idle time,
  lowering EPH. Binary blacklists miss contextual patterns (bad on Friday 8PM,
  fine Tuesday 2PM). This module enables contextual scoring.
"""

import math
import os
from datetime import datetime, timezone

import structlog

from .constants import ZONE_TYPE_ENC, ZONE_TYPE_ENC_DEFAULT, CITY_TIER_ENC, CITY_TIER_ENC_DEFAULT

log = structlog.get_logger()

# ── Constants ─────────────────────────────────────────────────
# Delay deviation > this z-score → significant (flag for agent review)
DELAY_Z_SCORE_THRESHOLD = float(os.getenv("DELAY_Z_SCORE_THRESHOLD", "1.5"))
# Minimum std for z-score denominator (avoids division near zero)
MIN_DELAY_STD_MINS      = 0.5
# Historical window for dead zone rate calculation
DEAD_ZONE_HISTORY_DAYS  = int(os.getenv("DEAD_ZONE_HISTORY_DAYS", "14"))
# stress_ratio below this → zone counted as dead in historical rate
DEAD_ZONE_STRESS_THRESHOLD = float(os.getenv("DEAD_ZONE_STRESS_THRESHOLD", "0.5"))


# ── Helpers ───────────────────────────────────────────────────

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ══════════════════════════════════════════════════════════════
# FUNCTION 1 — compute_restaurant_baseline
# ══════════════════════════════════════════════════════════════

async def compute_restaurant_baseline(
    restaurant_id: str,
    hour_of_day:   int,
    day_of_week:   int,
    conn,
) -> dict:
    """
    Get the historical delay baseline for a restaurant at a specific time slot.

    Primary source: restaurant_delay_hourly continuous aggregate
    (pre-computed by TimescaleDB, filters by hour_of_day + day_of_week
    over the last 28 days). Fast — no cold aggregation.

    Fallback: if no time-specific data exists, query the raw
    restaurant_delay_events table for any recent event from this restaurant.

    Returns:
        restaurant_id, avg_delay_mins, std_delay_mins, sample_count,
        hour_of_day, day_of_week, is_time_specific, has_baseline
    """
    # Primary: time-specific baseline from continuous aggregate
    row = await conn.fetchrow(
        """
        SELECT AVG(avg_delay)    AS baseline_avg,
               AVG(std_delay)    AS baseline_std,
               SUM(sample_count) AS total_samples
        FROM restaurant_delay_hourly
        WHERE restaurant_id = $1
          AND hour_of_day    = $2
          AND day_of_week    = $3
          AND bucket         > NOW() - INTERVAL '28 days'
        """,
        restaurant_id,
        hour_of_day,
        day_of_week,
    )

    has_time_data = (
        row is not None
        and row["total_samples"] is not None
        and int(row["total_samples"]) > 0
    )

    if has_time_data:
        return {
            "restaurant_id":    restaurant_id,
            "avg_delay_mins":   round(float(row["baseline_avg"]), 2),
            "std_delay_mins":   round(float(row["baseline_std"] or MIN_DELAY_STD_MINS), 2),
            "sample_count":     int(row["total_samples"]),
            "hour_of_day":      hour_of_day,
            "day_of_week":      day_of_week,
            "is_time_specific": True,
            "has_baseline":     True,
        }

    # Fallback: any event from this restaurant in the last 14 days
    fallback = await conn.fetchrow(
        """
        SELECT AVG(delay_mins)    AS fallback_avg,
               STDDEV(delay_mins) AS fallback_std,
               COUNT(*)           AS sample_count
        FROM restaurant_delay_events
        WHERE restaurant_id = $1
          AND timestamp > NOW() - INTERVAL '14 days'
        """,
        restaurant_id,
    )

    if (
        fallback is not None
        and fallback["sample_count"] is not None
        and int(fallback["sample_count"]) > 0
    ):
        return {
            "restaurant_id":    restaurant_id,
            "avg_delay_mins":   round(float(fallback["fallback_avg"]), 2),
            "std_delay_mins":   round(float(fallback["fallback_std"] or MIN_DELAY_STD_MINS), 2),
            "sample_count":     int(fallback["sample_count"]),
            "hour_of_day":      hour_of_day,
            "day_of_week":      day_of_week,
            "is_time_specific": False,
            "has_baseline":     True,
        }

    # No data at all
    log.debug("no delay baseline found", restaurant_id=restaurant_id)
    return {
        "restaurant_id":    restaurant_id,
        "avg_delay_mins":   0.0,
        "std_delay_mins":   0.0,
        "sample_count":     0,
        "hour_of_day":      hour_of_day,
        "day_of_week":      day_of_week,
        "is_time_specific": False,
        "has_baseline":     False,
    }


# ══════════════════════════════════════════════════════════════
# FUNCTION 2 — compute_delay_deviation
# ══════════════════════════════════════════════════════════════

async def compute_delay_deviation(
    restaurant_id:    str,
    actual_prep_mins: float,
    hour_of_day:      int,
    day_of_week:      int,
    conn,
) -> dict:
    """
    Compare actual prep time to the historical baseline. Compute z-score.

    This is the Restaurant Ripple detection signal:
    consistent positive deviation per restaurant = ripple pattern.

    z_score > DELAY_Z_SCORE_THRESHOLD (1.5 sigma) → flagged as significant.
    The Restaurant Intelligence Agent uses this to:
      - Score active pickup locations by real-time delay risk
      - Generate proactive rider alerts before they arrive

    Returns:
        restaurant_id, actual_prep_mins, baseline_avg_mins, baseline_std_mins,
        deviation_mins, z_score, is_significant, is_delayed,
        sample_count, has_baseline
    """
    baseline = await compute_restaurant_baseline(
        restaurant_id, hour_of_day, day_of_week, conn
    )

    if not baseline["has_baseline"]:
        return {
            "restaurant_id":     restaurant_id,
            "actual_prep_mins":  actual_prep_mins,
            "baseline_avg_mins": None,
            "baseline_std_mins": None,
            "deviation_mins":    None,
            "z_score":           None,
            "is_significant":    False,
            "is_delayed":        False,
            "sample_count":      0,
            "has_baseline":      False,
        }

    avg        = baseline["avg_delay_mins"]
    std        = max(baseline["std_delay_mins"], MIN_DELAY_STD_MINS)
    deviation  = actual_prep_mins - avg
    z_score    = deviation / std

    is_significant = abs(z_score) > DELAY_Z_SCORE_THRESHOLD
    is_delayed     = deviation > 0 and is_significant

    return {
        "restaurant_id":     restaurant_id,
        "actual_prep_mins":  actual_prep_mins,
        "baseline_avg_mins": avg,
        "baseline_std_mins": round(std, 2),
        "deviation_mins":    round(deviation, 2),
        "z_score":           round(z_score, 3),
        "is_significant":    is_significant,
        "is_delayed":        is_delayed,
        "sample_count":      baseline["sample_count"],
        "has_baseline":      True,
    }


# ══════════════════════════════════════════════════════════════
# FUNCTION 3 — get_active_pickups
# ══════════════════════════════════════════════════════════════

async def get_active_pickups(conn) -> list[dict]:
    """
    Return all orders currently at the pickup stage.

    Covers statuses: 'assigned' (rider dispatched, not yet at restaurant)
    and 'rider_inbound' (rider confirmed at restaurant, waiting for prep).

    time_waiting_mins is computed from rider_inbound_at if available,
    otherwise from assigned_at. This is the key signal for restaurant
    ripple detection — riders waiting longer than expected_prep_mins
    accumulate idle time and lose EPH.

    Returns:
        List of pickup dicts ordered by assignment time (oldest first).
        Each: order_id, restaurant_id, restaurant_name, zone IDs,
              rider_id, expected_prep_mins, actual_prep_mins (if known),
              weather_condition, traffic_density, status, time_waiting_mins
    """
    rows = await conn.fetch(
        """
        SELECT o.id              AS order_id,
               o.restaurant_id,
               r.name            AS restaurant_name,
               r.zone_id         AS restaurant_zone_id,
               o.pickup_zone_id,
               o.rider_id,
               o.expected_prep_mins,
               o.actual_prep_mins,
               o.weather_condition,
               o.traffic_density,
               o.status,
               o.assigned_at,
               o.rider_inbound_at,
               NOW()             AS now_ts
        FROM orders o
        JOIN restaurants r ON r.id = o.restaurant_id
        WHERE o.status IN ('assigned', 'rider_inbound')
          AND o.assigned_at IS NOT NULL
        ORDER BY o.assigned_at ASC
        """
    )

    pickups = []
    for row in rows:
        # Use rider_inbound_at if the rider has confirmed arrival,
        # otherwise use assigned_at as the wait anchor
        anchor    = row["rider_inbound_at"] or row["assigned_at"]
        wait_mins = (row["now_ts"] - anchor).total_seconds() / 60 if anchor else 0.0

        pickups.append({
            "order_id":           str(row["order_id"]),
            "restaurant_id":      str(row["restaurant_id"]),
            "restaurant_name":    row["restaurant_name"],
            "restaurant_zone_id": str(row["restaurant_zone_id"]),
            "pickup_zone_id":     str(row["pickup_zone_id"]),
            "rider_id":           str(row["rider_id"]) if row["rider_id"] else None,
            "expected_prep_mins": float(row["expected_prep_mins"] or 0.0),
            "actual_prep_mins":   (
                float(row["actual_prep_mins"])
                if row["actual_prep_mins"] is not None
                else None
            ),
            "weather_condition":  row["weather_condition"],
            "traffic_density":    row["traffic_density"],
            "status":             row["status"],
            "time_waiting_mins":  round(max(wait_mins, 0.0), 1),
        })

    return pickups


# ══════════════════════════════════════════════════════════════
# FUNCTION 4 — score_assignment
# ══════════════════════════════════════════════════════════════

async def score_assignment(order_id: str, rider_id: str, conn) -> dict:
    """
    Assemble dead zone risk inputs for a specific order-rider assignment.

    This function gathers all the raw fields needed to call Model 3
    (Dead Zone Risk Predictor) at /internal/predict/dead-zone.
    The agent passes the ml_inputs dict directly to the ML server.

    What this computes:
      - dist_from_home_zone_km: haversine from rider's home zone centroid
        to order's delivery zone centroid
      - current_density_ratio: latest density score at destination zone
      - historical_dead_rate: fraction of the last 14 days' snapshots where
        the destination zone had stress_ratio < DEAD_ZONE_STRESS_THRESHOLD

    Schema note:
      The zones table currently has no 'type' column (hub/commercial/
      residential/peripheral). dest_zone_type_enc defaults to 1
      (residential/mixed) and city_tier_enc defaults to 0 (Metropolitan,
      since all seeded zones are Bangalore). When a zone_type column is
      added to the zones schema, update this query to use it.

    Raises:
        ValueError if order or rider is not found.

    Returns:
        order_id, rider_id, delivery_zone_id, rider_persona,
        ml_inputs (dict matching DeadZoneRequest schema)
    """
    # ── Order details ──────────────────────────────────────────
    order = await conn.fetchrow(
        """
        SELECT o.delivery_zone_id,
               o.distance_km,
               o.is_long_distance,
               o.weather_condition,
               o.traffic_density,
               z.centroid_lat AS dest_lat,
               z.centroid_lng AS dest_lng,
               z.boundary_geojson->>'zone_type' AS dest_zone_type,
               z.boundary_geojson->>'city_tier' AS dest_city_tier
        FROM orders o
        JOIN zones z ON z.id = o.delivery_zone_id
        WHERE o.id = $1
        """,
        order_id,
    )

    if order is None:
        raise ValueError(f"Order {order_id} not found")

    # ── Rider home zone ────────────────────────────────────────
    rider = await conn.fetchrow(
        """
        SELECT r.home_zone_id,
               r.persona_type,
               z.centroid_lat AS home_lat,
               z.centroid_lng AS home_lng
        FROM riders r
        JOIN zones z ON z.id = r.home_zone_id
        WHERE r.id = $1
        """,
        rider_id,
    )

    if rider is None:
        raise ValueError(f"Rider {rider_id} not found")

    # ── Derived features ───────────────────────────────────────
    dist_from_home_km = _haversine_km(
        float(rider["home_lat"]), float(rider["home_lng"]),
        float(order["dest_lat"]), float(order["dest_lng"]),
    )

    # Current density at destination zone
    dest_density = await conn.fetchrow(
        """
        SELECT COALESCE(density_score, 0.0) AS density_score
        FROM zone_density_snapshots
        WHERE zone_id = $1
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        order["delivery_zone_id"],
    )
    current_density_ratio = float(dest_density["density_score"]) if dest_density else 0.0

    # Historical dead zone rate at destination
    hist = await conn.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE stress_ratio < $2) AS dead_count,
               COUNT(*)                                   AS total_count
        FROM zone_density_snapshots
        WHERE zone_id = $1
          AND timestamp > NOW() - ($3 || ' days')::INTERVAL
        """,
        order["delivery_zone_id"],
        DEAD_ZONE_STRESS_THRESHOLD,
        str(DEAD_ZONE_HISTORY_DAYS),
    )

    if hist and hist["total_count"] and int(hist["total_count"]) > 0:
        historical_dead_rate = float(hist["dead_count"]) / float(hist["total_count"])
    else:
        historical_dead_rate = 0.3   # conservative default when no history

    now = datetime.now(timezone.utc)

    return {
        "order_id":          order_id,
        "rider_id":          rider_id,
        "delivery_zone_id":  str(order["delivery_zone_id"]),
        "rider_persona":     rider["persona_type"],
        # Ready to POST to /internal/predict/dead-zone
        "ml_inputs": {
            "dest_zone_type_enc":     ZONE_TYPE_ENC.get(order["dest_zone_type"] or "", ZONE_TYPE_ENC_DEFAULT),
            "city_tier_enc":          CITY_TIER_ENC.get(order["dest_city_tier"] or "", CITY_TIER_ENC_DEFAULT),
            "hour_of_day":            now.hour,
            "day_of_week":            now.weekday(),
            "is_weekend":             1 if now.weekday() >= 5 else 0,
            "is_ld_order":            1 if order["is_long_distance"] else 0,
            "dist_from_home_zone_km": round(dist_from_home_km, 2),
            "current_density_ratio":  round(current_density_ratio, 4),
            "historical_dead_rate":   round(historical_dead_rate, 4),
        },
    }
