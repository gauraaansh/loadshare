"""
ARIA — Event Stream: Order Factory
====================================
Pure functions for generating synthetic orders.
No DB calls, no Redis calls — all inputs passed in.

Functions:
  haversine_km       — great-circle distance
  pick_weather       — time-correlated weather condition
  pick_traffic       — zone+time correlated traffic density
  travel_mins        — speed-based travel time with noise
  compute_prep_time  — queue-aware restaurant prep time
  pick_delivery_zone — density-weighted delivery zone selection
  compute_fare       — INR fare from distance + flags
"""

import math
import random

from config import (
    AVG_SPEEDS, PEAK_HOURS,
    BASE_FARE_RS, PER_KM_RATE_RS, LD_BONUS_RS,
    PREP_TIME_PER_SLOT,
)


def _to_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── Distance ──────────────────────────────────────────────────

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ── Conditions ────────────────────────────────────────────────

def pick_weather(hour: int) -> str:
    """Weather skewed toward rain in late afternoon / evening."""
    if 15 <= hour <= 20:
        return random.choices(
            ["Clear", "Cloudy", "Rain", "Heavy_Rain"],
            weights=[0.35, 0.30, 0.25, 0.10],
        )[0]
    return random.choices(
        ["Clear", "Cloudy", "Rain", "Heavy_Rain"],
        weights=[0.60, 0.22, 0.14, 0.04],
    )[0]


def pick_traffic(hour: int, zone_type: str) -> str:
    """Traffic density by zone type and peak hour."""
    is_peak       = hour in PEAK_HOURS
    is_congested  = zone_type in ("hub", "commercial")

    if is_peak and is_congested:
        return random.choices(["medium", "high", "jam"], weights=[0.20, 0.50, 0.30])[0]
    if is_peak:
        return random.choices(["low", "medium", "high"], weights=[0.20, 0.55, 0.25])[0]
    return random.choices(["low", "medium", "high"], weights=[0.55, 0.35, 0.10])[0]


# ── Timing ────────────────────────────────────────────────────

def travel_mins(distance_km: float, zone_type: str, is_peak: bool) -> float:
    """
    Realistic travel time based on zone type and time of day.
    Gaussian noise (σ=10%) models real-world variance.
    Minimum 2 minutes regardless of distance.
    """
    period = "peak" if is_peak else "offpeak"
    speed  = AVG_SPEEDS.get((zone_type, period), 20.0)
    base   = (distance_km / speed) * 60.0
    return max(2.0, base * random.normalvariate(1.0, 0.10))


def compute_prep_time(base_prep_mins: float, queue_len: int) -> float:
    """
    Queue-aware prep time.
    capacity = base_prep / PREP_TIME_PER_SLOT
    If queue_len > capacity, prep time scales up with congestion.

    Example: base=20min → capacity=4 orders
      queue=2 → no congestion, base ± noise
      queue=6 → congestion_factor=1.5, actual ≈ 30min
    """
    capacity = max(2, round(base_prep_mins / PREP_TIME_PER_SLOT))
    congestion = 1.0 + max(0.0, (queue_len - capacity) / capacity)
    actual = base_prep_mins * congestion * random.normalvariate(1.0, 0.12)
    return max(3.0, round(actual, 1))


# ── Zone selection ────────────────────────────────────────────

def pick_delivery_zone(
    zones: dict[str, dict],
    pickup_zone_id: str,
    zone_density_cache: dict[str, dict],
) -> str:
    """
    Pick a delivery zone weighted by order activity and zone type.
    Restricts to the same city as the pickup zone to avoid cross-city
    assignments (which produce unrealistic hundreds-of-km distances).
    Excludes the pickup zone itself to avoid trivial same-zone deliveries.

    Weight = zone_type_factor × max(order_count_in_cache, 1)
    Commercial and hub zones attract more deliveries.
    Peripheral zones rarely receive deliveries.
    """
    pickup_city = zones.get(pickup_zone_id, {}).get("city")
    candidates = [
        (zid, z)
        for zid, z in zones.items()
        if zid != pickup_zone_id
        and z.get("is_active", True)
        and (pickup_city is None or z.get("city") == pickup_city)
    ]
    if not candidates:
        return pickup_zone_id

    TYPE_WEIGHTS = {"hub": 2.0, "commercial": 1.8, "residential": 1.0, "peripheral": 0.4}

    weights = []
    for zid, z in candidates:
        type_w   = TYPE_WEIGHTS.get(z.get("zone_type", "residential"), 1.0)
        raw_order_w = zone_density_cache.get(zid, {}).get("order_count", 1.0)
        order_w  = max(0.1, _to_float(raw_order_w, 1.0))
        weights.append(type_w * order_w)

    total = sum(weights)
    norm  = [w / total for w in weights]
    return random.choices(candidates, weights=norm)[0][0]


# ── Fare ──────────────────────────────────────────────────────

def compute_fare(distance_km: float, is_long_distance: bool) -> float:
    """
    INR fare for the rider.
    Tuned so ~3.3 orders/hr at 3km avg ≈ Rs.89/hr EPH (near Rs.90 target).
    """
    fare = BASE_FARE_RS + distance_km * PER_KM_RATE_RS
    if is_long_distance:
        fare += LD_BONUS_RS
    return round(fare + random.uniform(-1.5, 2.5), 2)
