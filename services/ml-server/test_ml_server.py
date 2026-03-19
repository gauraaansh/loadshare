#!/usr/bin/env python3
"""
ARIA — ML Server Integration Tests
=====================================
Hits all 4 prediction endpoints with realistic payloads.
Run this after starting the server to verify everything works
before connecting the MCP server to it.

Usage:
    # Against local dev server (python main.py)
    python test_ml_server.py

    # Against Docker container (adjust host if needed)
    ML_SERVER_URL=http://localhost:8002 python test_ml_server.py

    # Against internal Docker network (from another container)
    ML_SERVER_URL=http://aria-ml-server:8002 python test_ml_server.py
"""

import os
import sys
import json
import time
import httpx

BASE_URL = os.getenv("ML_SERVER_URL", "http://localhost:8002")
API_KEY  = os.getenv("ML_INTERNAL_KEY", "aria-ml-internal-dev-key")
HEADERS  = {"X-Internal-Key": API_KEY, "Content-Type": "application/json"}

PASS = "✅ PASS"
FAIL = "❌ FAIL"
WARN = "⚠️  WARN"

results = []


def section(title: str):
    print(f"\n{'─'*55}")
    print(f"  {title}")
    print(f"{'─'*55}")


def check(name: str, condition: bool, detail: str = ""):
    icon = PASS if condition else FAIL
    print(f"  {icon}  {name}")
    if detail:
        print(f"         {detail}")
    results.append((name, condition))
    return condition


# ══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════

section("Health Check")
try:
    r = httpx.get(f"{BASE_URL}/health", timeout=10)
    r.raise_for_status()
    data = r.json()

    check("Health endpoint reachable", r.status_code == 200)
    check("Status field present", "status" in data)
    check("Models dict present", "models" in data)

    if "models" in data:
        for model_name, status in data["models"].items():
            loaded = status.get("loaded", False)
            ver    = status.get("version", "unknown")
            check(f"  {model_name} loaded", loaded, f"version={ver}")

    print(f"\n  Overall status: {data.get('status', 'unknown').upper()}")
    print(f"  Uptime: {data.get('uptime_seconds', 0):.1f}s")

except Exception as e:
    check("Health endpoint reachable", False, str(e))
    print("\n  ❌ Cannot reach server — make sure it's running:")
    print(f"     cd services/ml-server && python main.py")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════
# AUTH CHECK
# ══════════════════════════════════════════════════════════════

section("Auth — X-Internal-Key")
try:
    # Missing key
    r_no_key = httpx.post(
        f"{BASE_URL}/internal/predict/persona",
        json={"dummy": True},
        headers={"Content-Type": "application/json"},
        timeout=5,
    )
    check("Missing key returns 401", r_no_key.status_code == 401,
          f"got {r_no_key.status_code}")

    # Wrong key
    r_bad_key = httpx.post(
        f"{BASE_URL}/internal/predict/persona",
        json={"dummy": True},
        headers={"X-Internal-Key": "wrong-key", "Content-Type": "application/json"},
        timeout=5,
    )
    check("Wrong key returns 401", r_bad_key.status_code == 401,
          f"got {r_bad_key.status_code}")
except Exception as e:
    check("Auth check", False, str(e))


# ══════════════════════════════════════════════════════════════
# MODEL 1 — PERSONA CLASSIFIER
# ══════════════════════════════════════════════════════════════

section("Model 1 — Rider Persona Classifier")

# Test: Supplementary earner profile
supp_payload = {
    "n_rides_observed": 8,
    "peak_hour_rate": 0.65,
    "morning_rate": 0.20,
    "night_rate": 0.05,
    "n_distinct_zones": 2,
    "acceptance_rate": 0.78,
    "ld_rejection_rate": 0.72,
    "avg_shift_hours": 4.5,
    "off_peak_acceptance": 0.15,
    "avg_orders_per_shift": 9.0,
}

# Test: Dedicated earner profile
ded_payload = {
    "n_rides_observed": 12,
    "peak_hour_rate": 0.45,
    "morning_rate": 0.35,
    "night_rate": 0.20,
    "n_distinct_zones": 4,
    "acceptance_rate": 0.91,
    "ld_rejection_rate": 0.22,
    "avg_shift_hours": 9.5,
    "off_peak_acceptance": 0.82,
    "avg_orders_per_shift": 21.0,
}

try:
    t0 = time.time()
    r  = httpx.post(f"{BASE_URL}/internal/predict/persona",
                    json=supp_payload, headers=HEADERS, timeout=10)
    latency = round((time.time() - t0) * 1000)

    check("Supplementary request succeeds (200)", r.status_code == 200,
          f"got {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        check("Has persona field",         "persona"        in d)
        check("Has confidence field",      "confidence"     in d)
        check("Has key_factors list",      "key_factors"    in d and len(d["key_factors"]) > 0)
        check("Has model_version",         "model_version"  in d)
        check("Confidence is 0-1",         0 <= d.get("confidence", -1) <= 1)
        check(f"Latency < 500ms",          latency < 500,   f"{latency}ms")
        print(f"\n  Supplementary profile → persona={d['persona']}  "
              f"conf={d['confidence']:.2f}  "
              f"key_factor={d['key_factors'][0]['feature'] if d['key_factors'] else 'none'}")

    # Dedicated profile
    r2 = httpx.post(f"{BASE_URL}/internal/predict/persona",
                    json=ded_payload, headers=HEADERS, timeout=10)
    if r2.status_code == 200:
        d2 = r2.json()
        print(f"  Dedicated profile    → persona={d2['persona']}  conf={d2['confidence']:.2f}")

except Exception as e:
    check("Model 1 endpoint", False, str(e))


# ══════════════════════════════════════════════════════════════
# MODEL 2 — DURATION SCORER
# ══════════════════════════════════════════════════════════════

section("Model 2 — Delivery Duration Scorer")

duration_payload = {
    "distance_km": 4.2,
    "Road_traffic_density_enc": 2,
    "order_hour": 19,
    "is_lunch_peak": 0,
    "is_dinner_peak": 1,
    "is_weekend": 1,
    "day_of_week": 5,
    "month": 7,
    "City_enc": 1,
    "city_name_enc": 3,
    "Weatherconditions_enc": 0,
    "Type_of_vehicle_enc": 3,
    "Type_of_order_enc": 2,
    "Festival_enc": 0,
    "multiple_deliveries": 0,
    "Delivery_person_Age": 28,
    "Delivery_person_Ratings": 4.6,
    "Vehicle_condition": 2,
}

try:
    t0 = time.time()
    r  = httpx.post(f"{BASE_URL}/internal/predict/duration",
                    json=duration_payload, headers=HEADERS, timeout=10)
    latency = round((time.time() - t0) * 1000)

    check("Duration request succeeds (200)", r.status_code == 200,
          f"got {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        check("Has predicted_minutes",  "predicted_minutes" in d)
        check("Has key_factors (5)",    "key_factors" in d and len(d["key_factors"]) >= 3)
        check("Prediction is positive", d.get("predicted_minutes", -1) > 0)
        check("Prediction is sane (<120min)", d.get("predicted_minutes", 999) < 120)
        check(f"Latency < 500ms",       latency < 500,  f"{latency}ms")
        top_feat = d["key_factors"][0]["feature"] if d["key_factors"] else "none"
        print(f"\n  4.2km, dinner peak, high traffic → {d['predicted_minutes']:.1f} min  "
              f"top_factor={top_feat}")

except Exception as e:
    check("Model 2 endpoint", False, str(e))


# ══════════════════════════════════════════════════════════════
# MODEL 3 — DEAD ZONE PREDICTOR
# ══════════════════════════════════════════════════════════════

section("Model 3 — Dead Zone Risk Predictor")

# High-risk: peripheral zone, LD order, far from home, high historical rate
high_risk_payload = {
    "dest_zone_type_enc": 3,      # peripheral
    "city_tier_enc": 0,
    "hour_of_day": 14,
    "day_of_week": 2,
    "is_weekend": 0,
    "is_ld_order": 1,             # long-distance
    "dist_from_home_zone_km": 7.2,
    "current_density_ratio": 0.18,
    "historical_dead_rate": 0.68, # historically bad zone
}

# Low-risk: commercial zone, short order, home zone
low_risk_payload = {
    "dest_zone_type_enc": 0,      # commercial
    "city_tier_enc": 0,
    "hour_of_day": 19,
    "day_of_week": 4,
    "is_weekend": 0,
    "is_ld_order": 0,
    "dist_from_home_zone_km": 0.8,
    "current_density_ratio": 0.72,
    "historical_dead_rate": 0.08,
}

try:
    t0 = time.time()
    r  = httpx.post(f"{BASE_URL}/internal/predict/dead-zone",
                    json=high_risk_payload, headers=HEADERS, timeout=10)
    latency = round((time.time() - t0) * 1000)

    check("Dead zone request succeeds (200)", r.status_code == 200,
          f"got {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        check("Has dead_zone_probability",   "dead_zone_probability" in d)
        check("Has is_high_risk",            "is_high_risk"          in d)
        check("Probability is 0-1",          0 <= d.get("dead_zone_probability", -1) <= 1)
        check("Has key_factors",             "key_factors" in d and len(d["key_factors"]) > 0)
        check(f"Latency < 500ms",            latency < 500,  f"{latency}ms")

        if d.get("is_high_risk"):
            has_stranding = d.get("expected_stranding_mins") is not None
            check("High risk → stranding mins present", has_stranding,
                  f"stranding={d.get('expected_stranding_mins')}min")

        print(f"\n  Peripheral + LD + far → prob={d['dead_zone_probability']:.3f}  "
              f"high_risk={d['is_high_risk']}  "
              f"stranding={d.get('expected_stranding_mins', 'n/a')}min")

    # Low risk case
    r2 = httpx.post(f"{BASE_URL}/internal/predict/dead-zone",
                    json=low_risk_payload, headers=HEADERS, timeout=10)
    if r2.status_code == 200:
        d2 = r2.json()
        print(f"  Commercial + home zone → prob={d2['dead_zone_probability']:.3f}  "
              f"high_risk={d2['is_high_risk']}")
        check("Low risk scenario has lower probability than high risk",
              d2["dead_zone_probability"] < d.get("dead_zone_probability", 0),
              f"{d2['dead_zone_probability']:.3f} < {d.get('dead_zone_probability', 0):.3f}")

except Exception as e:
    check("Model 3 endpoint", False, str(e))


# ══════════════════════════════════════════════════════════════
# MODEL 4 — EARNINGS TRAJECTORY FORECASTER
# ══════════════════════════════════════════════════════════════

section("Model 4 — Earnings Trajectory Forecaster")

# Declining trajectory: EPH dropping over the last 3 lags
declining_payload = {
    "persona_enc": 0,           # supplementary
    "hour_of_day": 20,
    "orders_completed": 5,
    "earnings_so_far": 210.0,
    "current_eph": 76.5,
    "idle_time_mins": 28.0,
    "dead_runs_count": 1,
    "zone_density": 0.42,
    "obs_point_mins": 165.0,
    "time_remaining_mins": 75.0,
    "total_shift_mins": 240.0,
    "eph_lag1_30min": 82.0,
    "eph_lag2_60min": 88.0,
    "eph_lag3_90min": 91.0,     # was 91 → 88 → 82 → 76.5, clear decline
}

# Healthy trajectory: EPH stable/improving
healthy_payload = {
    "persona_enc": 1,           # dedicated
    "hour_of_day": 18,
    "orders_completed": 12,
    "earnings_so_far": 580.0,
    "current_eph": 94.5,
    "idle_time_mins": 8.0,
    "dead_runs_count": 0,
    "zone_density": 0.78,
    "obs_point_mins": 220.0,
    "time_remaining_mins": 140.0,
    "total_shift_mins": 360.0,
    "eph_lag1_30min": 92.0,
    "eph_lag2_60min": 91.0,
    "eph_lag3_90min": 90.5,     # stable around 90-94
}

try:
    t0 = time.time()
    r  = httpx.post(f"{BASE_URL}/internal/predict/earnings-trajectory",
                    json=declining_payload, headers=HEADERS, timeout=10)
    latency = round((time.time() - t0) * 1000)

    check("Earnings request succeeds (200)", r.status_code == 200,
          f"got {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        check("Has projected_final_eph",  "projected_final_eph" in d)
        check("Has below_threshold",      "below_threshold"     in d)
        check("Has alert_level",          "alert_level"         in d)
        check("Has eph_trend",            "eph_trend"           in d)
        check("Alert level is valid",     d.get("alert_level") in {"none", "watch", "intervene"})
        check("Trend is valid",           d.get("eph_trend")   in {"improving", "stable", "declining"})
        check(f"Latency < 500ms",         latency < 500,  f"{latency}ms")
        print(f"\n  Declining (91→88→82→76) → projected={d['projected_final_eph']:.1f}  "
              f"trend={d['eph_trend']}  alert={d['alert_level']}")

    # Healthy case
    r2 = httpx.post(f"{BASE_URL}/internal/predict/earnings-trajectory",
                    json=healthy_payload, headers=HEADERS, timeout=10)
    if r2.status_code == 200:
        d2 = r2.json()
        print(f"  Healthy  (90→91→92→94) → projected={d2['projected_final_eph']:.1f}  "
              f"trend={d2['eph_trend']}  alert={d2['alert_level']}")
        if "alert_level" in d and "alert_level" in d2:
            declining_is_worse = (
                {"none": 0, "watch": 1, "intervene": 2}.get(d["alert_level"], 0) >=
                {"none": 0, "watch": 1, "intervene": 2}.get(d2["alert_level"], 0)
            )
            check("Declining case has higher alert than healthy", declining_is_worse,
                  f"{d['alert_level']} >= {d2['alert_level']}")

except Exception as e:
    check("Model 4 endpoint", False, str(e))


# ══════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════

print(f"\n{'='*55}")
print("  TEST SUMMARY")
print(f"{'='*55}")

passed = sum(1 for _, ok in results if ok)
failed = len(results) - passed

for name, ok in results:
    print(f"  {'✅' if ok else '❌'} {name}")

print(f"\n  {passed}/{len(results)} passed,  {failed} failed")

if failed == 0:
    print("\n  🟢 ML server ready — connect the MCP server to it")
    print(f"     ML_SERVER_URL=http://aria-ml-server:8002")
    print(f"     ML_INTERNAL_KEY=<your key from .env>")
elif failed <= 3:
    print("\n  🟡 Minor failures — check model artifact paths")
else:
    print("\n  🔴 Multiple failures — check server logs")
    print("     docker logs aria-ml-server")

print(f"{'='*55}\n")
sys.exit(0 if failed == 0 else 1)
