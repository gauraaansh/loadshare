"""
ARIA — Synthetic Data Generator
=================================
Grounded entirely in:
  1. Loadshare article statistics (primary source)
  2. Real dataset distributions from train_final.csv (secondary)

Generates training data for:
  - Model 1: Rider Persona Classifier
  - Model 3: Dead Zone Risk Predictor
  - Model 4: Earnings Trajectory Forecaster

Also generates operational DB records:
  - riders, restaurants, zones, orders, sessions
  - zone_density_snapshots, restaurant_delay_events

ARTICLE GROUND TRUTH (never deviate from these):
  - 80% Supplementary Earners, 20% Dedicated Earners
  - Supplementary EPH target: Rs.90-100/hr
  - Dedicated EPH target: Rs.100+/hr
  - Actual EPH at crisis: Rs.70-85/hr
  - 2 hours daily idle (1hr between orders + 1hr at restaurants)
  - 5km psychological barrier for "long distance" in Bangalore
  - Sister zones: 2-3 dense zones within 6-7km radius
  - 90% of LD orders crossed zone boundaries
  - Dead zone = peripheral zones with low return order density
  - Retention dropped to 30% at crisis peak
  - JIT eliminated 5min idle between orders
  - Restaurant delay: certain restaurants consistently slow

Run:
    python synthetic_generator_v2.py

Outputs:
    synthetic/riders.json
    synthetic/zones.json
    synthetic/restaurants.json
    synthetic/model1_training.csv    (persona classifier)
    synthetic/model3_training.csv    (dead zone risk)
    synthetic/model4_training.csv    (earnings trajectory)
    synthetic/operational_orders.json
    synthetic/sessions.json
    synthetic/zone_snapshots.json
"""

import csv
import json
import math
import random
import uuid
import os
from datetime import datetime, timedelta, date
from collections import defaultdict
from pathlib import Path

random.seed(42)

OUTPUT_DIR = Path(__file__).parent.parent / "synthetic"
OUTPUT_DIR.mkdir(exist_ok=True)

DATA_FILE = Path(__file__).parent.parent / "kaggle_data" / "train_final.csv"

# ── CITY DATA (from dataset — 22 Indian cities) ───────────────
# City centers derived from dataset coordinates
CITIES = {
    "Bangalore":   {"lat": 12.97,  "lng": 77.59,  "tier": "Metropolitan", "code": "BAN"},
    "Mumbai":      {"lat": 19.07,  "lng": 72.87,  "tier": "Metropolitan", "code": "MUM"},
    "Hyderabad":   {"lat": 17.38,  "lng": 78.48,  "tier": "Metropolitan", "code": "HYD"},
    "Chennai":     {"lat": 13.08,  "lng": 80.27,  "tier": "Metropolitan", "code": "CHE"},
    "Jaipur":      {"lat": 26.91,  "lng": 75.78,  "tier": "Metropolitan", "code": "JAP"},
    "Pune":        {"lat": 18.52,  "lng": 73.85,  "tier": "Metropolitan", "code": "PUN"},
    "Kochi":       {"lat": 10.04,  "lng": 76.32,  "tier": "Metropolitan", "code": "KOC"},
    "Kolkata":     {"lat": 22.57,  "lng": 88.36,  "tier": "Urban",        "code": "KOL"},
    "Indore":      {"lat": 22.72,  "lng": 75.86,  "tier": "Urban",        "code": "IND"},
    "Mysore":      {"lat": 12.30,  "lng": 76.65,  "tier": "Urban",        "code": "MYS"},
    "Surat":       {"lat": 21.17,  "lng": 72.83,  "tier": "Urban",        "code": "SUR"},
    "Coimbatore":  {"lat": 11.02,  "lng": 76.98,  "tier": "Urban",        "code": "COI"},
}

# Zone types and their properties
# Derived from article: hub zones have high return density (low dead zone risk)
# Peripheral zones have low return density (high dead zone risk)
ZONE_TYPES = {
    "hub":         {"dead_zone_prob": 0.05, "density_base": 45, "n_zones": 3},
    "commercial":  {"dead_zone_prob": 0.20, "density_base": 28, "n_zones": 4},
    "residential": {"dead_zone_prob": 0.45, "density_base": 18, "n_zones": 5},
    "peripheral":  {"dead_zone_prob": 0.75, "density_base": 8,  "n_zones": 3},
}

# Hour-of-day order multipliers
# Peak: lunch 12-14, dinner 18-21 (from article: supplementary riders work peaks)
HOURLY_MULT = {
    0:0.04, 1:0.02, 2:0.01, 3:0.01, 4:0.01, 5:0.02,
    6:0.05, 7:0.10, 8:0.15, 9:0.18, 10:0.20, 11:0.40,
    12:0.90, 13:0.85, 14:0.40, 15:0.30, 16:0.35, 17:0.55,
    18:0.75, 19:1.00, 20:0.95, 21:0.80, 22:0.50, 23:0.20,
}

# Delivery time stats from real dataset by tier
DELIVERY_STATS = {
    "Metropolitan": {"mean": 27.3, "std": 8.5,  "min": 10, "max": 54},
    "Urban":        {"mean": 23.0, "std": 7.0,  "min": 10, "max": 54},
    "Semi-Urban":   {"mean": 49.7, "std": 5.0,  "min": 35, "max": 60},
}

# ── ARTICLE-DERIVED CONSTANTS ─────────────────────────────────
EPH = {
    "supplementary_target": 95.0,    # Rs.90-100, use midpoint
    "dedicated_target":     110.0,   # Rs.100+
    "crisis_min":           70.0,    # actual at crisis
    "crisis_max":           85.0,    # actual at crisis
    "order_value_mean":     88.0,    # Rs per order
    "order_value_std":      22.0,
}

PERSONA = {
    "supplementary": 0.80,           # 80% from article
    "dedicated":     0.20,           # 20% from article
}

# LD order threshold from article: 5km psychological barrier
LD_DISTANCE_THRESHOLD_KM = 5.0
# 90% of LD orders crossed zone boundaries (article)
LD_ZONE_CROSSING_RATE = 0.90

# Idle time from article: 2 hours daily (1hr between orders + 1hr at restaurants)
IDLE_TIME = {
    "between_orders_mins": 60,       # 1 hour/day between orders
    "at_restaurants_mins": 60,       # 1 hour/day at restaurants
    "total_daily_mins":    120,      # 2 hours total
}

# ── HELPERS ───────────────────────────────────────────────────
def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def gen_id():
    return str(uuid.uuid4())

def haversine(lat1, lng1, lat2, lng2):
    R = 6371
    lat1, lng1, lat2, lng2 = [math.radians(x) for x in [lat1, lng1, lat2, lng2]]
    dlat = lat2 - lat1; dlng = lng2 - lng1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def rand_coord_near(lat, lng, radius_km=0.8):
    r = radius_km / 111.0
    return (round(lat + random.uniform(-r, r), 6),
            round(lng + random.uniform(-r, r), 6))

def gauss_clamp(mean, std, lo, hi):
    return clamp(random.gauss(mean, std), lo, hi)

def weighted_choice(options, weights):
    total = sum(weights)
    r = random.random() * total
    for opt, w in zip(options, weights):
        r -= w
        if r <= 0:
            return opt
    return options[-1]


# ══════════════════════════════════════════════════════════════
# ZONE GENERATION
# Per city: hub zones near center, peripheral zones at edges
# Sister zones: 2-3 dense zones within 6-7km (article)
# ══════════════════════════════════════════════════════════════
def generate_zones(cities=CITIES):
    zones = {}   # zone_id -> zone dict

    for city_name, city_info in cities.items():
        clat = city_info["lat"]
        clng = city_info["lng"]
        city_zones = []

        # Hub zones — near city center, within 2km
        for i in range(ZONE_TYPES["hub"]["n_zones"]):
            zlat, zlng = rand_coord_near(clat, clng, radius_km=2.0)
            z = {
                "id":          gen_id(),
                "city":        city_name,
                "city_tier":   city_info["tier"],
                "name":        f"{city_name} Hub {i+1}",
                "type":        "hub",
                "lat":         zlat,
                "lng":         zlng,
                "dead_zone_prob":  ZONE_TYPES["hub"]["dead_zone_prob"],
                "density_base":    ZONE_TYPES["hub"]["density_base"],
                "sister_zone_ids": [],   # filled after all zones created
            }
            city_zones.append(z)

        # Commercial zones — 2-5km from center
        for i in range(ZONE_TYPES["commercial"]["n_zones"]):
            zlat, zlng = rand_coord_near(clat, clng, radius_km=3.5)
            z = {
                "id":          gen_id(),
                "city":        city_name,
                "city_tier":   city_info["tier"],
                "name":        f"{city_name} Commercial {i+1}",
                "type":        "commercial",
                "lat":         zlat,
                "lng":         zlng,
                "dead_zone_prob":  ZONE_TYPES["commercial"]["dead_zone_prob"],
                "density_base":    ZONE_TYPES["commercial"]["density_base"],
                "sister_zone_ids": [],
            }
            city_zones.append(z)

        # Residential zones — 4-7km from center
        for i in range(ZONE_TYPES["residential"]["n_zones"]):
            zlat, zlng = rand_coord_near(clat, clng, radius_km=5.5)
            z = {
                "id":          gen_id(),
                "city":        city_name,
                "city_tier":   city_info["tier"],
                "name":        f"{city_name} Residential {i+1}",
                "type":        "residential",
                "lat":         zlat,
                "lng":         zlng,
                "dead_zone_prob":  ZONE_TYPES["residential"]["dead_zone_prob"],
                "density_base":    ZONE_TYPES["residential"]["density_base"],
                "sister_zone_ids": [],
            }
            city_zones.append(z)

        # Peripheral zones — 7-12km from center (high dead zone risk)
        for i in range(ZONE_TYPES["peripheral"]["n_zones"]):
            zlat, zlng = rand_coord_near(clat, clng, radius_km=9.0)
            z = {
                "id":          gen_id(),
                "city":        city_name,
                "city_tier":   city_info["tier"],
                "name":        f"{city_name} Peripheral {i+1}",
                "type":        "peripheral",
                "lat":         zlat,
                "lng":         zlng,
                "dead_zone_prob":  ZONE_TYPES["peripheral"]["dead_zone_prob"],
                "density_base":    ZONE_TYPES["peripheral"]["density_base"],
                "sister_zone_ids": [],
            }
            city_zones.append(z)

        # Assign sister zones: 2-3 nearest dense zones within 6-7km (article)
        for z in city_zones:
            candidates = []
            for other in city_zones:
                if other["id"] == z["id"]:
                    continue
                dist = haversine(z["lat"], z["lng"], other["lat"], other["lng"])
                if dist <= 7.0 and other["type"] in ("hub", "commercial"):
                    candidates.append((dist, other["id"]))
            candidates.sort()
            z["sister_zone_ids"] = [zid for _, zid in candidates[:3]]

        for z in city_zones:
            zones[z["id"]] = z

    total_zones = len(zones)
    print(f"  Generated {total_zones} zones across {len(cities)} cities")
    print(f"  ({ZONE_TYPES['hub']['n_zones']} hub + {ZONE_TYPES['commercial']['n_zones']} commercial + "
          f"{ZONE_TYPES['residential']['n_zones']} residential + {ZONE_TYPES['peripheral']['n_zones']} peripheral) × {len(cities)} cities")
    return zones


# ══════════════════════════════════════════════════════════════
# RIDER GENERATION
# Article: 80% supplementary, 20% dedicated
# Supplementary: peak hours only, home zone = hub/commercial
# Dedicated: full day, home zone = any
# ══════════════════════════════════════════════════════════════
def generate_riders(zones, n=500):
    riders = []
    zone_list = list(zones.values())
    hub_commercial = [z for z in zone_list if z["type"] in ("hub", "commercial")]
    all_zones = zone_list

    for i in range(n):
        persona = "supplementary" if random.random() < PERSONA["supplementary"] else "dedicated"

        # Home zone — supplementary prefers hub/commercial (familiar zones, article)
        if persona == "supplementary":
            home_zone = random.choice(hub_commercial) if random.random() < 0.70 else random.choice(all_zones)
        else:
            home_zone = random.choice(all_zones)

        # Age from dataset: mean=29.6, range 15-50
        age = int(gauss_clamp(29.6, 5.5, 18, 50))

        # Rating from dataset: mean=4.63, range 1-5
        rating = round(gauss_clamp(4.63, 0.35, 3.0, 5.0), 1)

        # Vehicle: from dataset 58% motorcycle, 33.5% scooter, 8.4% e-scooter
        vehicle = weighted_choice(
            ["motorcycle", "scooter", "electric_scooter"],
            [0.58, 0.335, 0.085]
        )

        # Persona confidence (simulates what our classifier would output)
        persona_confidence = round(gauss_clamp(0.82, 0.08, 0.60, 0.99), 2)

        riders.append({
            "id":                 gen_id(),
            "name":               f"Rider_{i+1:04d}",
            "city":               home_zone["city"],
            "home_zone_id":       home_zone["id"],
            "home_zone_type":     home_zone["type"],
            "persona_type":       persona,
            "persona_confidence": persona_confidence,
            "age":                age,
            "rating":             rating,
            "vehicle_type":       vehicle,
            "is_active":          random.random() < 0.85,
        })

    supp = sum(1 for r in riders if r["persona_type"] == "supplementary")
    print(f"  Generated {n} riders: {supp} supplementary ({supp/n:.0%}), {n-supp} dedicated ({(n-supp)/n:.0%})")
    return riders


# ══════════════════════════════════════════════════════════════
# RESTAURANT GENERATION
# Article: certain restaurants consistently delay → slow_factor
# Restaurant Intelligence Network identifies these
# ══════════════════════════════════════════════════════════════
def generate_restaurants(zones, n=200):
    restaurants = []
    zone_list = list(zones.values())

    # Weight zones by density (more restaurants in busier zones)
    zone_weights = {"hub": 4.0, "commercial": 2.5, "residential": 1.5, "peripheral": 0.5}

    for i in range(n):
        zone = weighted_choice(
            zone_list,
            [zone_weights[z["type"]] for z in zone_list]
        )
        lat, lng = rand_coord_near(zone["lat"], zone["lng"], 0.5)

        # 15% of restaurants are "perennially slow" (article: certain restaurant partners)
        is_slow = random.random() < 0.15
        slow_factor = gauss_clamp(1.8, 0.3, 1.4, 2.5) if is_slow else 1.0

        # Base prep time: mean ~10min from pickup_delay analysis
        avg_prep = gauss_clamp(10.0 * slow_factor, 3.0, 3.0, 40.0)

        restaurants.append({
            "id":               gen_id(),
            "name":             f"Restaurant_{i+1:03d}",
            "city":             zone["city"],
            "zone_id":          zone["id"],
            "zone_type":        zone["type"],
            "lat":              lat,
            "lng":              lng,
            "avg_prep_time_mins": round(avg_prep, 1),
            "is_slow":          is_slow,
            "slow_factor":      round(slow_factor, 2),
            "last_risk_score":  round(random.uniform(0.6, 0.9) if is_slow else random.uniform(0.1, 0.4), 2),
        })

    slow = sum(1 for r in restaurants if r["is_slow"])
    print(f"  Generated {n} restaurants: {slow} slow ({slow/n:.0%})")
    return restaurants


# ══════════════════════════════════════════════════════════════
# MODEL 1 TRAINING DATA — Rider Persona Classifier
# Features from FIRST 5-10 rides of a rider
# Article behavioral signals:
#   Supplementary: peak hours, low zone drift, high LD rejection
#   Dedicated:     all-day, high zone drift, lower LD rejection
# ══════════════════════════════════════════════════════════════
def generate_model1_data(n_samples=10000):
    """
    Each row = behavioral summary of a rider's first 5-10 rides.
    Target: persona_type (0=supplementary, 1=dedicated)
    """
    rows = []

    for _ in range(n_samples):
        persona = "supplementary" if random.random() < PERSONA["supplementary"] else "dedicated"
        label   = 0 if persona == "supplementary" else 1

        n_rides = random.randint(5, 10)   # first 5-10 rides

        if persona == "supplementary":
            # Article: peak meal times, familiar zones, high LD rejection
            # Hours cluster around 11-14 and 18-21
            hours = [weighted_choice(
                list(range(24)),
                [0.01,0.01,0.01,0.01,0.01,0.01, 0.02,0.03,0.04,0.04,0.05,0.10,
                 0.15,0.12,0.05,0.03,0.03,0.05, 0.08,0.10,0.09,0.07,0.04,0.02]
            ) for _ in range(n_rides)]

            # Zone drift: stays in 1-2 zones (low)
            n_distinct_zones = random.choice([1, 1, 2, 2, 2])

            # Acceptance rate: moderate (rejects LD orders)
            acceptance_rate = round(gauss_clamp(0.72, 0.10, 0.45, 0.95), 2)

            # LD rejection rate: high (80%+ reject long distance, article)
            ld_rejection_rate = round(gauss_clamp(0.78, 0.12, 0.50, 0.99), 2)

            # Avg shift length: 3-5 hours
            avg_shift_hours = round(gauss_clamp(4.0, 0.8, 2.5, 6.0), 1)

            # Off-peak acceptance: low (don't work off-peak)
            off_peak_acceptance = round(gauss_clamp(0.12, 0.08, 0.0, 0.35), 2)

            # Orders per shift: fewer (part time)
            avg_orders_per_shift = round(gauss_clamp(8.0, 2.5, 3.0, 16.0), 1)

        else:
            # Dedicated: full day spread, wider zones, more tolerant of LD
            hours = [random.randint(6, 22) for _ in range(n_rides)]

            # Zone drift: covers 3-5 zones
            n_distinct_zones = random.choice([3, 3, 4, 4, 5])

            # Acceptance rate: high (needs orders, can't be picky)
            acceptance_rate = round(gauss_clamp(0.88, 0.07, 0.65, 0.99), 2)

            # LD rejection rate: lower (willing to do LD for higher pay)
            ld_rejection_rate = round(gauss_clamp(0.30, 0.15, 0.05, 0.65), 2)

            # Avg shift: 7-10 hours
            avg_shift_hours = round(gauss_clamp(8.5, 1.0, 6.0, 11.0), 1)

            # Off-peak acceptance: high (works all day)
            off_peak_acceptance = round(gauss_clamp(0.75, 0.12, 0.45, 0.99), 2)

            # Orders per shift: more
            avg_orders_per_shift = round(gauss_clamp(18.0, 3.5, 10.0, 35.0), 1)

        # Derived features
        peak_hour_rate = round(sum(1 for h in hours if h in range(11,15) or h in range(18,22)) / n_rides, 2)
        morning_rate   = round(sum(1 for h in hours if h in range(7,11)) / n_rides, 2)
        night_rate     = round(sum(1 for h in hours if h in range(22,24) or h in range(0,6)) / n_rides, 2)

        rows.append({
            # Features
            "n_rides_observed":      n_rides,
            "peak_hour_rate":        peak_hour_rate,
            "morning_rate":          morning_rate,
            "night_rate":            night_rate,
            "n_distinct_zones":      n_distinct_zones,
            "acceptance_rate":       acceptance_rate,
            "ld_rejection_rate":     ld_rejection_rate,
            "avg_shift_hours":       avg_shift_hours,
            "off_peak_acceptance":   off_peak_acceptance,
            "avg_orders_per_shift":  avg_orders_per_shift,
            # Target
            "persona_label":         label,    # 0=supplementary, 1=dedicated
        })

    supp = sum(1 for r in rows if r["persona_label"] == 0)
    print(f"  Model 1: {n_samples} samples — {supp} supplementary ({supp/n_samples:.0%}), {n_samples-supp} dedicated")
    return rows


# ══════════════════════════════════════════════════════════════
# MODEL 3 TRAINING DATA — Dead Zone Risk Predictor
# Article: 90% of LD orders crossed zone boundaries
# Dead run = stranded in peripheral zone, no return orders
# Features: destination zone type, time, day, density, distance from home
# ══════════════════════════════════════════════════════════════
def generate_model3_data(zones, n_samples=15000):
    """
    Each row = one order assignment evaluated for dead zone risk.
    Target: is_dead_zone (0/1) + expected_stranding_mins
    """
    rows = []
    zone_list = list(zones.values())

    for _ in range(n_samples):
        # Pick destination zone
        dest_zone = random.choice(zone_list)
        dest_type = dest_zone["type"]

        # Time features
        hour       = weighted_choice(list(range(24)), [HOURLY_MULT[h] for h in range(24)])
        day_of_week= random.randint(0, 6)
        is_weekend = 1 if day_of_week >= 5 else 0

        # Distance from rider's home zone (key feature)
        # Supplementary riders mostly in hub/commercial, so home distance to peripheral is large
        is_ld_order = random.random() < 0.10   # 10% of all orders are LD
        if is_ld_order:
            # LD orders: 90% cross zone boundaries (article)
            dist_from_home_km = gauss_clamp(8.0, 2.5, 5.0, 20.0)
        else:
            dist_from_home_km = gauss_clamp(2.5, 1.0, 0.5, 5.0)

        # Current zone density at this hour (fraction of peak)
        hourly_mult   = HOURLY_MULT[hour]
        base_density  = ZONE_TYPES[dest_type]["density_base"]
        current_density = clamp(
            base_density * hourly_mult * gauss_clamp(1.0, 0.2, 0.5, 1.5),
            0, base_density * 1.5
        )
        density_ratio = round(current_density / (base_density * 1.5), 3)  # 0-1

        # Historical dead zone rate for this zone type at this time
        base_dead_prob = ZONE_TYPES[dest_type]["dead_zone_prob"]
        # Off-peak hours amplify dead zone risk
        time_factor = 1.5 if hour < 10 or hour > 22 else 1.0
        adjusted_dead_prob = clamp(base_dead_prob * time_factor, 0, 0.99)

        # Dead zone outcome — grounded in article
        # 90% of LD orders to peripheral create dead zone risk (article stat)
        if is_ld_order and dest_type == "peripheral":
            dead_zone_prob = clamp(adjusted_dead_prob * 1.4, 0, 0.99)
        else:
            dead_zone_prob = adjusted_dead_prob * (dist_from_home_km / 10.0)
            dead_zone_prob = clamp(dead_zone_prob, 0, 0.99)

        # Add noise for realism
        dead_zone_prob = clamp(dead_zone_prob + random.gauss(0, 0.05), 0, 0.99)
        is_dead_zone   = 1 if random.random() < dead_zone_prob else 0

        # Expected stranding time (minutes) if dead zone
        # Article: dead run back to home zone eats into earnings
        if is_dead_zone:
            stranding_mins = gauss_clamp(dist_from_home_km * 5.0, 10.0, 10.0, 90.0)
        else:
            stranding_mins = 0.0

        # Encode zone type
        zone_type_enc = {"hub": 0, "commercial": 1, "residential": 2, "peripheral": 3}[dest_type]
        city_tier_enc = {"Metropolitan": 0, "Urban": 1}[dest_zone["city_tier"]]

        rows.append({
            # Features
            "dest_zone_type_enc":      zone_type_enc,
            "city_tier_enc":           city_tier_enc,
            "hour_of_day":             hour,
            "day_of_week":             day_of_week,
            "is_weekend":              is_weekend,
            "is_ld_order":             int(is_ld_order),
            "dist_from_home_zone_km":  round(dist_from_home_km, 2),
            "current_density_ratio":   round(density_ratio, 3),
            "historical_dead_rate":    round(adjusted_dead_prob, 3),
            # Targets
            "is_dead_zone":            is_dead_zone,
            "expected_stranding_mins": round(stranding_mins, 1),
        })

    dead = sum(1 for r in rows if r["is_dead_zone"] == 1)
    print(f"  Model 3: {n_samples} samples — {dead} dead zone ({dead/n_samples:.1%} positive rate)")
    return rows


# ══════════════════════════════════════════════════════════════
# MODEL 4 TRAINING DATA — Earnings Trajectory Forecaster
# Article: Rs.70-85 actual vs Rs.90-100 target
# 2 hours idle daily: 1hr between orders + 1hr at restaurants
# Lag EPH features track degradation curve
# ══════════════════════════════════════════════════════════════
def generate_model4_data(n_samples=20000):
    """
    Each row = mid-shift snapshot of a rider.
    Observation point: 90-180 mins into shift.
    Target: projected_final_eph (what EPH will be at shift end)
    Also target: will_finish_below_threshold (churn signal)
    """
    rows = []

    for _ in range(n_samples):
        persona = "supplementary" if random.random() < PERSONA["supplementary"] else "dedicated"

        # Shift parameters
        if persona == "supplementary":
            total_shift_mins  = gauss_clamp(4.0 * 60, 30, 180, 360)    # 3-6 hrs
            eph_target        = EPH["supplementary_target"]
        else:
            total_shift_mins  = gauss_clamp(8.5 * 60, 45, 360, 600)    # 6-10 hrs
            eph_target        = EPH["dedicated_target"]

        # Observation point (how far into shift we are when we make the prediction)
        obs_point_mins = gauss_clamp(total_shift_mins * 0.5, 20, 60, total_shift_mins * 0.8)
        time_remaining_mins = total_shift_mins - obs_point_mins

        # Determine if this is a "healthy" or "struggling" session
        # Article: Rs.70-85 actual at crisis, Rs.90-100 target
        is_struggling = random.random() < 0.40   # 40% of sessions are below-target

        # Idle time accumulated so far (article: 2hrs/day total = proportional to time in shift)
        idle_proportion = obs_point_mins / total_shift_mins
        if is_struggling:
            # Struggling riders accumulate more idle time (stuck at slow restaurants, dead runs)
            idle_time_so_far = gauss_clamp(
                IDLE_TIME["total_daily_mins"] * idle_proportion * 1.4, 10, 0, obs_point_mins * 0.7
            )
        else:
            idle_time_so_far = gauss_clamp(
                IDLE_TIME["total_daily_mins"] * idle_proportion * 0.7, 8, 0, obs_point_mins * 0.4
            )

        # Orders completed so far
        # Working time = obs_point_mins - idle_time_so_far
        working_mins  = max(obs_point_mins - idle_time_so_far, 10)
        # From dataset: avg delivery ~26min Metropolitan, ~23min Urban
        avg_delivery_mins = gauss_clamp(26.0, 5.0, 15.0, 45.0)
        orders_completed  = max(1, int(working_mins / avg_delivery_mins))

        # Earnings so far
        order_value_mean = EPH["order_value_mean"]
        earnings_so_far  = sum(gauss_clamp(order_value_mean, EPH["order_value_std"], 40, 180)
                               for _ in range(orders_completed))

        # Current EPH
        hours_worked = obs_point_mins / 60.0
        current_eph  = earnings_so_far / hours_worked if hours_worked > 0 else 0

        # Dead runs so far
        dead_runs = random.randint(2, 5) if is_struggling else random.randint(0, 1)

        # Zone density (affects future order availability)
        hour_of_day = random.randint(6, 23)
        zone_density = round(HOURLY_MULT[hour_of_day] * gauss_clamp(1.0, 0.2, 0.4, 1.5), 3)

        # ── PROJECT FINAL EPH ─────────────────────────────────
        # Ground truth from article:
        #   Crisis actual EPH:  Rs.70-85/hr  (struggling)
        #   Healthy actual EPH: Rs.90-130/hr (above target)
        # Set final EPH directly from these ranges — do NOT extrapolate
        # a slope forward (causes unrealistic collapse to Rs.45)

        if is_struggling:
            # Struggling: finish Rs.60-87, mean ~Rs.77 (article: Rs.70-85 crisis)
            projected_final_eph = round(
                clamp(gauss_clamp(77.0, 7.0, 60.0, 87.0) + random.gauss(0, 2.0), 58.0, 89.0), 2
            )
        else:
            # Healthy: finish at or above persona target
            projected_final_eph = round(
                clamp(gauss_clamp(eph_target * 1.05, eph_target * 0.12,
                                  eph_target * 0.92, eph_target * 1.50)
                      + random.gauss(0, 2.0), eph_target * 0.90, 300.0), 2
            )

        # Will finish below threshold?
        below_threshold = 1 if projected_final_eph < eph_target * 0.88 else 0

        # ── LAG EPH VALUES ────────────────────────────────────
        # Build lags backward from projected_final_eph — consistent curve.
        # Struggling: current EPH slightly above final (still declining toward floor)
        # Healthy: current EPH close to final (stable)

        if is_struggling:
            current_eph = round(clamp(projected_final_eph * gauss_clamp(1.10, 0.04, 1.04, 1.20), 60, 200), 2)
            eph_lag1    = round(clamp(current_eph * gauss_clamp(1.04, 0.02, 1.01, 1.08), 60, 200), 2)
            eph_lag2    = round(clamp(eph_lag1    * gauss_clamp(1.03, 0.02, 1.01, 1.06), 60, 200), 2)
            eph_lag3    = round(clamp(eph_lag2    * gauss_clamp(1.03, 0.01, 1.01, 1.05), 60, 200), 2)
        else:
            current_eph = round(clamp(projected_final_eph * gauss_clamp(0.97, 0.03, 0.90, 1.02), 80, 300), 2)
            eph_lag1    = round(clamp(current_eph * gauss_clamp(0.98, 0.02, 0.93, 1.02), 80, 300), 2)
            eph_lag2    = round(clamp(eph_lag1    * gauss_clamp(0.99, 0.01, 0.95, 1.02), 80, 300), 2)
            eph_lag3    = round(clamp(eph_lag2    * gauss_clamp(0.99, 0.01, 0.96, 1.02), 80, 300), 2)

        # Persona encoding
        persona_enc = 0 if persona == "supplementary" else 1

        rows.append({
            # Features
            "persona_enc":           persona_enc,
            "hour_of_day":           hour_of_day,
            "orders_completed":      orders_completed,
            "earnings_so_far":       round(earnings_so_far, 2),
            "current_eph":           round(current_eph, 2),
            "idle_time_mins":        round(idle_time_so_far, 1),
            "dead_runs_count":       dead_runs,
            "zone_density":          zone_density,
            "obs_point_mins":        round(obs_point_mins, 1),
            "time_remaining_mins":   round(time_remaining_mins, 1),
            "total_shift_mins":      round(total_shift_mins, 1),
            "eph_lag1_30min":        round(eph_lag1, 2),
            "eph_lag2_60min":        round(eph_lag2, 2),
            "eph_lag3_90min":        round(eph_lag3, 2),
            "eph_target":            eph_target,
            # Targets
            "projected_final_eph":   projected_final_eph,
            "below_threshold":       below_threshold,
        })

    below = sum(1 for r in rows if r["below_threshold"] == 1)
    print(f"  Model 4: {n_samples} samples — {below} below threshold ({below/n_samples:.1%})")
    return rows


# ══════════════════════════════════════════════════════════════
# ZONE DENSITY SNAPSHOTS (operational DB)
# Every 15 minutes, last 7 days
# ══════════════════════════════════════════════════════════════
def generate_zone_snapshots(zones, days=7):
    snapshots = []
    now = datetime.now().replace(second=0, microsecond=0)
    base = now - timedelta(days=days)

    for day_offset in range(days):
        day_dt = base + timedelta(days=day_offset)
        for hour in range(24):
            for quarter in range(0, 60, 15):
                ts = day_dt.replace(hour=hour, minute=quarter)
                mult = HOURLY_MULT[hour]

                for zone_id, zone in zones.items():
                    base_density = zone["density_base"]
                    count = max(0, int(random.gauss(base_density * mult, base_density * 0.2)))
                    max_possible = base_density * 1.5
                    density_score = round(clamp(count / max_possible, 0, 1), 3)
                    typical = base_density * mult
                    stress_ratio = round(count / typical if typical > 0 else 1.0, 2)
                    active_riders = max(0, int(count * 0.35 + random.randint(-1, 2)))

                    snapshots.append({
                        "zone_id":            zone_id,
                        "timestamp":          ts.isoformat(),
                        "order_count":        count,
                        "active_rider_count": active_riders,
                        "density_score":      density_score,
                        "stress_ratio":       stress_ratio,
                    })

    print(f"  Zone snapshots: {len(snapshots):,} ({days} days × {len(zones)} zones × 96 per day)")
    return snapshots


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
def main():
    print("\n" + "=" * 60)
    print("  ARIA Synthetic Data Generator v2")
    print("  Grounded in Loadshare article + real dataset distributions")
    print("=" * 60)

    n_riders      = int(os.getenv("SEED_RIDER_COUNT",      "500"))
    n_restaurants = int(os.getenv("SEED_RESTAURANT_COUNT", "200"))

    # 1. Zones
    print("\n── Generating zones ──")
    zones = generate_zones(CITIES)
    with open(OUTPUT_DIR / "zones.json", "w") as f:
        json.dump(list(zones.values()), f, indent=2)

    # 2. Riders
    print("\n── Generating riders ──")
    riders = generate_riders(zones, n=n_riders)
    with open(OUTPUT_DIR / "riders.json", "w") as f:
        json.dump(riders, f, indent=2)

    # 3. Restaurants
    print("\n── Generating restaurants ──")
    restaurants = generate_restaurants(zones, n=n_restaurants)
    with open(OUTPUT_DIR / "restaurants.json", "w") as f:
        json.dump(restaurants, f, indent=2)

    # 4. Model 1 training data
    print("\n── Generating Model 1 training data (Persona Classifier) ──")
    m1_data = generate_model1_data(n_samples=10000)
    with open(OUTPUT_DIR / "model1_training.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(m1_data[0].keys()))
        writer.writeheader()
        writer.writerows(m1_data)

    # 5. Model 3 training data
    print("\n── Generating Model 3 training data (Dead Zone Risk) ──")
    m3_data = generate_model3_data(zones, n_samples=15000)
    with open(OUTPUT_DIR / "model3_training.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(m3_data[0].keys()))
        writer.writeheader()
        writer.writerows(m3_data)

    # 6. Model 4 training data
    print("\n── Generating Model 4 training data (Earnings Trajectory) ──")
    m4_data = generate_model4_data(n_samples=20000)
    with open(OUTPUT_DIR / "model4_training.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(m4_data[0].keys()))
        writer.writeheader()
        writer.writerows(m4_data)

    # 7. Zone density snapshots
    print("\n── Generating zone density snapshots ──")
    snapshots = generate_zone_snapshots(zones, days=7)
    with open(OUTPUT_DIR / "zone_snapshots.json", "w") as f:
        json.dump(snapshots, f)

    # Summary
    print("\n" + "=" * 60)
    print("  GENERATION COMPLETE")
    print("=" * 60)
    print(f"  Zones:           {len(zones):>8,}")
    print(f"  Riders:          {len(riders):>8,}")
    print(f"  Restaurants:     {len(restaurants):>8,}")
    print(f"  Model 1 samples: {len(m1_data):>8,}")
    print(f"  Model 3 samples: {len(m3_data):>8,}")
    print(f"  Model 4 samples: {len(m4_data):>8,}")
    print(f"  Zone snapshots:  {len(snapshots):>8,}")
    print(f"\n  Output dir: {OUTPUT_DIR}")
    print("\n  Next: python train_models.py")


if __name__ == "__main__":
    main()
