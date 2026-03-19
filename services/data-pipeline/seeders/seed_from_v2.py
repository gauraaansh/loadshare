#!/usr/bin/env python3
"""
ARIA — Seeder: seed_from_v2.py
================================
Reads the v2 generator JSON output and inserts into PostgreSQL.

The v2 generator produces:
  zones.json, riders.json, restaurants.json, zone_snapshots.json

It does NOT produce orders, rider_sessions, or restaurant_delay_events.
This seeder generates those procedurally so the algorithmic modules
have meaningful historical baselines on day one.

What this inserts:
  1. zones               (180, from JSON)
  2. riders              (500, from JSON)
  3. restaurants         (200, from JSON)
  4. zone_density_snapshots (~120k, from JSON)
  5. rider_sessions      (30 days × 500 riders × ~70% activity ≈ 10k rows)
  6. orders              (historical, minimal — FK anchor for delay events)
  7. restaurant_delay_events (14 days × peak hours ≈ 28k rows)
  8. Refresh continuous aggregates

Zone type (hub/commercial/residential/peripheral) and city_tier are
stored in the boundary_geojson JSONB column since the zones table has
no dedicated type column. This keeps the schema unchanged and makes
type info queryable via boundary_geojson->>'zone_type'.

Run:
    python seeders/seed_from_v2.py

Prerequisites:
    - PostgreSQL running: docker compose up -d postgres
    - Schema applied (auto on first postgres start)
    - v2 generator has been run: python generators/synthetic_generator_v2.py
"""

import json
import math
import os
import random
import uuid
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aria:aria_secret_change_me@localhost:5432/aria_db",
)

SYNTHETIC_DIR = Path(__file__).parent.parent / "synthetic"
BATCH_SIZE    = 1000
random.seed(0)   # reproducible historical data

# ── Article-grounded EPH constants (mirror v2 generator) ─────
EPH_SUPPLEMENTARY_TARGET = 95.0
EPH_DEDICATED_TARGET     = 110.0
EPH_CRISIS_MEAN          = 77.0     # Rs.70-85 crisis range midpoint
AVG_DELIVERY_MINS        = 26.0     # from dataset Metropolitan mean
IDLE_TOTAL_MINS_PER_DAY  = 120.0   # 2 hours daily (article)

SESSION_DAYS  = 30   # days of rider session history
DELAY_DAYS    = 14   # days of restaurant delay event history
PEAK_HOURS    = [12, 13, 19, 20]   # lunch + dinner peaks


# ── Helpers ───────────────────────────────────────────────────

def get_conn():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    except Exception as e:
        print(f"  Cannot connect to PostgreSQL: {e}")
        print(f"  Is postgres running?  docker compose ps")
        sys.exit(1)


def batches(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def pg_uuid_array(ids: list) -> str | None:
    """Format a Python list of UUID strings as a PostgreSQL array literal."""
    if not ids:
        return None
    return "{" + ",".join(ids) + "}"


def gauss_clamp(mean, std, lo, hi):
    return max(lo, min(hi, random.gauss(mean, std)))


def rand_coord_near(lat, lng, radius_km=0.3):
    offset = radius_km / 111.0
    return (
        round(lat + random.uniform(-offset, offset), 6),
        round(lng + random.uniform(-offset, offset), 6),
    )


def load_json(filename: str) -> list:
    path = SYNTHETIC_DIR / filename
    if not path.exists():
        print(f"  ERROR: {path} not found. Run synthetic_generator_v2.py first.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


# ── Phase 0: Clear existing data ──────────────────────────────

def clear_all(conn):
    """Truncate in reverse FK order. Zones are truncated too — v2 replaces them."""
    print("\n── Clearing existing data ──")
    tables = [
        # Agent outputs
        "observability_logs", "rider_interventions", "rider_churn_signals",
        "rider_alerts", "rider_health_snapshots", "order_risk_scores",
        "restaurant_risk_scores", "dead_zone_snapshots", "zone_stress_snapshots",
        "zone_recommendations", "agent_memory", "cycle_briefings",
        # Time-series
        "restaurant_delay_events", "zone_density_snapshots",
        # Operational
        "rider_sessions", "rider_location_updates", "orders",
        # Reference
        "restaurants", "riders", "zones",
    ]
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(f"TRUNCATE TABLE {t} CASCADE;")
    conn.commit()
    print(f"  Cleared {len(tables)} tables")


# ── Phase 1: Zones ────────────────────────────────────────────

def insert_zones(conn, zones: list) -> None:
    print(f"\n── Inserting {len(zones)} zones ──")
    sql = """
        INSERT INTO zones
            (id, name, city, centroid_lat, centroid_lng,
             sister_zone_ids, boundary_geojson, is_active)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """
    rows = []
    for z in zones:
        # Store zone_type and city_tier in boundary_geojson JSONB.
        # The zones table has no dedicated type column; this keeps the
        # schema unchanged while making type queryable:
        #   SELECT boundary_geojson->>'zone_type' FROM zones WHERE id = ...
        meta = json.dumps({
            "zone_type":  z["type"],
            "city_tier":  z["city_tier"],
            "density_base": z.get("density_base"),
            "dead_zone_prob": z.get("dead_zone_prob"),
        })
        rows.append((
            z["id"],
            z["name"],
            z["city"],
            z["lat"],
            z["lng"],
            pg_uuid_array(z.get("sister_zone_ids", [])),
            meta,
            True,
        ))
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=BATCH_SIZE)
    conn.commit()
    print(f"  Inserted {len(rows)} zones")


# ── Phase 2: Riders ───────────────────────────────────────────

def insert_riders(conn, riders: list) -> None:
    print(f"\n── Inserting {len(riders)} riders ──")
    sql = """
        INSERT INTO riders
            (id, name, home_zone_id, vehicle_type, rating,
             persona_type, persona_confidence, is_active, onboarded_at)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """
    now = datetime.utcnow()
    rows = [
        (
            r["id"], r["name"], r["home_zone_id"],
            r["vehicle_type"], r["rating"],
            r["persona_type"], r.get("persona_confidence"),
            r.get("is_active", True),
            now - timedelta(days=random.randint(30, 365)),
        )
        for r in riders
    ]
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=BATCH_SIZE)
    conn.commit()
    print(f"  Inserted {len(rows)} riders")


# ── Phase 3: Restaurants ──────────────────────────────────────

def insert_restaurants(conn, restaurants: list) -> None:
    print(f"\n── Inserting {len(restaurants)} restaurants ──")
    sql = """
        INSERT INTO restaurants
            (id, name, zone_id, lat, lng,
             avg_prep_time_mins, last_risk_score, is_active)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """
    rows = [
        (
            r["id"], r["name"], r["zone_id"],
            r["lat"], r["lng"],
            r["avg_prep_time_mins"],
            r.get("last_risk_score", 0.2),
            True,
        )
        for r in restaurants
    ]
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=BATCH_SIZE)
    conn.commit()
    print(f"  Inserted {len(rows)} restaurants")


# ── Phase 4: Zone density snapshots ──────────────────────────

def insert_zone_snapshots(conn, snapshots: list) -> None:
    print(f"\n── Inserting {len(snapshots):,} zone density snapshots ──")
    sql = """
        INSERT INTO zone_density_snapshots
            (zone_id, timestamp, order_count, active_rider_count,
             density_score, stress_ratio)
        VALUES %s
    """
    total = 0
    for b in batches(snapshots, BATCH_SIZE):
        rows = [
            (
                s["zone_id"], s["timestamp"],
                s["order_count"], s["active_rider_count"],
                s["density_score"], s["stress_ratio"],
            )
            for s in b
        ]
        with conn.cursor() as cur:
            execute_values(cur, sql, rows, page_size=BATCH_SIZE)
        conn.commit()
        total += len(b)
        print(f"  {total:>8,} / {len(snapshots):,}", end="\r")
    print(f"  Inserted {len(snapshots):,} zone snapshots          ")


# ── Phase 5: Historical rider sessions ───────────────────────

def generate_sessions(riders: list) -> list:
    """
    Generate SESSION_DAYS of completed rider sessions.
    Parametrised from Loadshare article statistics:
      - 40% of sessions are below EPH target (struggling)
      - Supplementary: 3-6 hr shifts, 60% of days active
      - Dedicated: 7-11 hr shifts, 85% of days active
      - 2 hours idle per day (article)
    """
    today   = date.today()
    sessions = []

    for rider in riders:
        persona    = rider["persona_type"]
        eph_target = EPH_SUPPLEMENTARY_TARGET if persona == "supplementary" else EPH_DEDICATED_TARGET

        for day_offset in range(1, SESSION_DAYS + 1):
            session_date = today - timedelta(days=day_offset)

            # Activity probability
            active_prob = 0.60 if persona == "supplementary" else 0.85
            if random.random() > active_prob:
                continue

            # Shift length
            if persona == "supplementary":
                shift_hours = gauss_clamp(4.0, 0.8, 2.5, 6.0)
                # Supplementary riders work peak hours — start lunch or dinner
                start_hour = random.choice([10.5, 11.0, 18.0, 18.5]) + random.uniform(-0.3, 0.3)
            else:
                shift_hours = gauss_clamp(8.5, 1.0, 6.0, 11.0)
                start_hour  = 8.0 + random.uniform(-1.0, 1.0)

            # Determine session quality
            is_struggling = random.random() < 0.40
            if is_struggling:
                eph = gauss_clamp(EPH_CRISIS_MEAN, 7.0, 58.0, 87.0)
            else:
                eph = gauss_clamp(eph_target * 1.05, eph_target * 0.12,
                                  eph_target * 0.90, 300.0)

            below_threshold = eph < eph_target

            # Session stats
            idle_time_mins   = gauss_clamp(
                IDLE_TOTAL_MINS_PER_DAY * (shift_hours / 8.0),
                10.0, 0.0, shift_hours * 60 * 0.5,
            )
            working_mins     = shift_hours * 60 - idle_time_mins
            total_orders     = max(1, int(working_mins / AVG_DELIVERY_MINS))
            total_earnings   = round(eph * shift_hours, 2)
            total_distance   = round(total_orders * gauss_clamp(4.5, 1.5, 2.0, 9.0), 2)
            dead_runs_count  = random.randint(2, 4) if is_struggling else random.randint(0, 1)
            ld_count         = random.randint(0, 2) if persona == "dedicated" else random.randint(0, 1)
            health_score     = round(min(100.0, max(0.0, (eph / eph_target) * 100)), 1)

            # Timestamps
            h     = int(start_hour)
            m     = int((start_hour - h) * 60)
            start = datetime(session_date.year, session_date.month, session_date.day, h, m)
            end   = start + timedelta(hours=shift_hours)

            sessions.append({
                "id":                  str(uuid.uuid4()),
                "rider_id":            rider["id"],
                "session_date":        session_date,
                "shift_start":         start,
                "shift_end":           end,
                "total_orders":        total_orders,
                "total_earnings":      total_earnings,
                "total_distance_km":   total_distance,
                "idle_time_mins":      round(idle_time_mins, 1),
                "dead_runs_count":     dead_runs_count,
                "long_distance_count": ld_count,
                "eph":                 round(eph, 2),
                "health_score":        health_score,
                "below_threshold":     below_threshold,
            })

    return sessions


def insert_sessions(conn, sessions: list) -> None:
    print(f"\n── Inserting {len(sessions):,} rider sessions ──")
    sql = """
        INSERT INTO rider_sessions
            (id, rider_id, session_date, shift_start, shift_end,
             total_orders, total_earnings, total_distance_km,
             idle_time_mins, dead_runs_count, long_distance_count,
             eph, health_score, below_threshold)
        VALUES %s
        ON CONFLICT (rider_id, session_date) DO NOTHING
    """
    total = 0
    for b in batches(sessions, BATCH_SIZE):
        rows = [
            (
                s["id"], s["rider_id"], s["session_date"],
                s["shift_start"], s["shift_end"],
                s["total_orders"], s["total_earnings"], s["total_distance_km"],
                s["idle_time_mins"], s["dead_runs_count"], s["long_distance_count"],
                s["eph"], s["health_score"], s["below_threshold"],
            )
            for s in b
        ]
        with conn.cursor() as cur:
            execute_values(cur, sql, rows, page_size=BATCH_SIZE)
        conn.commit()
        total += len(b)
    print(f"  Inserted {total:,} sessions")


# ── Phase 6 + 7: Historical orders + delay events ────────────

def generate_orders_and_delay_events(
    restaurants: list, zones: list
) -> tuple[list, list]:
    """
    Generate DELAY_DAYS of restaurant delay events (for baseline computation).
    Each delay event requires a FK reference to an order, so we generate
    minimal historical orders alongside them.

    Slow restaurants (is_slow=True, ~15%) produce delays consistently
    above their baseline, generating the Restaurant Ripple signal.
    Normal restaurants have random variation around their expected prep time.
    """
    today      = date.today()
    orders     = []
    delay_evts = []

    # Index zones by city for fast delivery zone lookup
    city_zones: dict[str, list] = defaultdict(list)
    for z in zones:
        city_zones[z["city"]].append(z)

    for restaurant in restaurants:
        city        = restaurant["city"]
        zone_pool   = city_zones.get(city, [])
        if not zone_pool:
            continue

        avg_prep    = restaurant["avg_prep_time_mins"]
        is_slow     = restaurant.get("is_slow", False)
        slow_factor = restaurant.get("slow_factor", 1.0)

        for day_offset in range(1, DELAY_DAYS + 1):
            event_date = today - timedelta(days=day_offset)

            for hour in PEAK_HOURS:
                n_events = random.randint(2, 4)

                for _ in range(n_events):
                    order_id = str(uuid.uuid4())
                    minute   = random.randint(0, 59)
                    ts       = datetime(
                        event_date.year, event_date.month, event_date.day,
                        hour, minute,
                    )

                    # Delivery zone — pick any zone in same city
                    delivery_zone = random.choice(zone_pool)
                    dlat, dlng    = rand_coord_near(
                        delivery_zone["lat"], delivery_zone["lng"]
                    )

                    # Minimal historical order (FK anchor only)
                    orders.append((
                        order_id,
                        restaurant["id"],
                        restaurant["zone_id"],  # pickup_zone_id
                        delivery_zone["id"],     # delivery_zone_id
                        restaurant["lat"],
                        restaurant["lng"],
                        dlat, dlng,
                        "delivered",
                        ts,
                        ts + timedelta(minutes=random.randint(25, 50)),  # delivered_at
                    ))

                    # Delay event: slow restaurants stay consistently above baseline
                    if is_slow:
                        actual = gauss_clamp(
                            avg_prep * slow_factor, 3.0,
                            avg_prep * 0.8, avg_prep * slow_factor * 1.5,
                        )
                    else:
                        actual = gauss_clamp(avg_prep, 2.5, 2.0, avg_prep * 2.2)

                    delay_evts.append((
                        str(uuid.uuid4()),
                        restaurant["id"],
                        order_id,
                        ts,
                        round(avg_prep, 1),
                        round(actual, 1),
                        round(actual - avg_prep, 2),
                        random.choice(["Clear", "Cloudy", "Fog", "Windy"]),
                        hour,
                        event_date.weekday(),
                    ))

    return orders, delay_evts


def insert_orders(conn, orders: list) -> None:
    print(f"\n── Inserting {len(orders):,} historical orders ──")
    sql = """
        INSERT INTO orders
            (id, restaurant_id, pickup_zone_id, delivery_zone_id,
             pickup_lat, pickup_lng, delivery_lat, delivery_lng,
             status, created_at, delivered_at)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """
    total = 0
    for b in batches(orders, BATCH_SIZE):
        with conn.cursor() as cur:
            execute_values(cur, sql, b, page_size=BATCH_SIZE)
        conn.commit()
        total += len(b)
        print(f"  {total:>8,} / {len(orders):,}", end="\r")
    print(f"  Inserted {len(orders):,} historical orders          ")


def insert_delay_events(conn, events: list) -> None:
    print(f"\n── Inserting {len(events):,} restaurant delay events ──")
    sql = """
        INSERT INTO restaurant_delay_events
            (id, restaurant_id, order_id, timestamp,
             expected_prep_mins, actual_prep_mins, delay_mins,
             weather_condition, hour_of_day, day_of_week)
        VALUES %s
        ON CONFLICT DO NOTHING
    """
    total = 0
    for b in batches(events, BATCH_SIZE):
        with conn.cursor() as cur:
            execute_values(cur, sql, b, page_size=BATCH_SIZE)
        conn.commit()
        total += len(b)
        print(f"  {total:>8,} / {len(events):,}", end="\r")
    print(f"  Inserted {len(events):,} delay events          ")


# ── Phase 8: Refresh continuous aggregates ───────────────────

def refresh_continuous_aggregates(conn) -> None:
    """
    Force-refresh both TimescaleDB continuous aggregates so the
    algorithmic modules can query them immediately after seeding.
    Without this, zone_density_hourly and restaurant_delay_hourly
    are empty until TimescaleDB's scheduled refresh runs.
    """
    print("\n── Refreshing continuous aggregates ──")
    # Timescale refresh procedure must run outside a transaction block.
    prev_autocommit = conn.autocommit
    try:
        if not prev_autocommit:
            conn.commit()
            conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "CALL refresh_continuous_aggregate('zone_density_hourly', NULL, NULL);"
            )
            print("  zone_density_hourly refreshed")
            cur.execute(
                "CALL refresh_continuous_aggregate('restaurant_delay_hourly', NULL, NULL);"
            )
            print("  restaurant_delay_hourly refreshed")
    finally:
        conn.autocommit = prev_autocommit


# ── Verify ───────────────────────────────────────────────────

def verify(conn) -> None:
    print("\n── Row counts ──")
    tables = [
        "zones", "riders", "restaurants",
        "zone_density_snapshots", "rider_sessions",
        "orders", "restaurant_delay_events",
    ]
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            n = cur.fetchone()[0]
            print(f"  {t:<35} {n:>10,}")


# ── Main ─────────────────────────────────────────────────────

def main():
    import time
    print("\n" + "=" * 60)
    print("  ARIA — seed_from_v2.py")
    print("=" * 60)
    t0 = time.time()

    # Load JSON files
    print("\n── Loading synthetic data ──")
    zones       = load_json("zones.json")
    riders      = load_json("riders.json")
    restaurants = load_json("restaurants.json")
    snapshots   = load_json("zone_snapshots.json")
    print(f"  zones:       {len(zones):,}")
    print(f"  riders:      {len(riders):,}")
    print(f"  restaurants: {len(restaurants):,}")
    print(f"  snapshots:   {len(snapshots):,}")

    # Connect
    print("\n── Connecting to PostgreSQL ──")
    conn = get_conn()
    print(f"  Connected: {DATABASE_URL.split('@')[-1]}")

    # Run phases
    clear_all(conn)
    insert_zones(conn, zones)
    insert_riders(conn, riders)
    insert_restaurants(conn, restaurants)
    insert_zone_snapshots(conn, snapshots)

    print("\n── Generating historical sessions ──")
    sessions = generate_sessions(riders)
    print(f"  Generated {len(sessions):,} sessions")
    insert_sessions(conn, sessions)

    print("\n── Generating historical orders + delay events ──")
    orders, delay_evts = generate_orders_and_delay_events(restaurants, zones)
    print(f"  Generated {len(orders):,} orders, {len(delay_evts):,} delay events")
    insert_orders(conn, orders)
    insert_delay_events(conn, delay_evts)

    refresh_continuous_aggregates(conn)
    verify(conn)

    conn.close()
    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"  Seeding complete in {elapsed:.1f}s")
    print(f"{'=' * 60}")
    print("\nNext:")
    print("  Run event stream:  docker compose up event-stream")
    print("  Verify in psql:    docker exec aria-postgres psql -U aria -d aria_db")


if __name__ == "__main__":
    main()
