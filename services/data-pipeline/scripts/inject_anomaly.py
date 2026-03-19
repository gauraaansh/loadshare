#!/usr/bin/env python3
"""
ARIA — Demo Anomaly Injection Scripts
=======================================
Plants specific anomalies into the live database
so you can watch ARIA detect and diagnose them in real time.

Usage:
    python scripts/inject_anomaly.py restaurant_delay
    python scripts/inject_anomaly.py dead_zone_pressure
    python scripts/inject_anomaly.py rider_earnings_degradation
    python scripts/inject_anomaly.py all   (plants all three)
    python scripts/inject_anomaly.py reset (removes planted anomalies)

Perfect for demos — run one of these, then trigger a Supervisor
cycle and watch the agent graph light up.
"""

import os
import sys
import uuid
import random
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://aria:aria_secret@localhost:5432/aria_db")


def get_conn():
    return psycopg2.connect(DATABASE_URL)


# ══════════════════════════════════════════════════════════════
# ANOMALY 1: RESTAURANT DELAY SPIKE
# ── Makes a specific restaurant suddenly show 3x its normal
#    prep time. Restaurant Intelligence Agent should detect
#    this within one cycle and flag it.
# ══════════════════════════════════════════════════════════════
def inject_restaurant_delay(conn):
    print("\n🔴 Injecting: Restaurant Delay Spike")
    print("   Target: Picks a random active restaurant and spikes its delays")

    with conn.cursor() as cur:
        # Pick a random restaurant
        cur.execute("SELECT id, name, avg_prep_time_mins FROM restaurants WHERE is_active = TRUE LIMIT 1 OFFSET (random()*100)::int")
        row = cur.fetchone()
        if not row:
            print("   ❌ No restaurants found")
            return

        rest_id, rest_name, avg_prep = row
        spiked_prep = avg_prep * 3.2  # 3x normal
        now = datetime.now()

        print(f"   Restaurant: {rest_name}")
        print(f"   Normal prep: {avg_prep:.0f} min → Spiked: {spiked_prep:.0f} min")

        # Inject 8 recent delay events showing the spike
        events = []
        for i in range(8):
            ts = now - timedelta(minutes=i * 5)
            actual = spiked_prep + random.uniform(-3, 5)
            events.append((
                str(uuid.uuid4()), rest_id, str(uuid.uuid4()),
                ts, avg_prep, actual, actual - avg_prep,
                "Cloudy", ts.hour, ts.weekday()
            ))

        execute_values(conn.cursor(), """
            INSERT INTO restaurant_delay_events
                (id, restaurant_id, order_id, timestamp,
                 expected_prep_mins, actual_prep_mins, delay_mins,
                 weather_condition, hour_of_day, day_of_week)
            VALUES %s
        """, events)

        # Tag this restaurant so we can reset it later
        cur.execute("""
            UPDATE restaurants
            SET avg_prep_time_mins = %s,
                last_risk_score = 0.85
            WHERE id = %s
        """, (spiked_prep, rest_id))

    conn.commit()
    print("   ✅ Injected 8 delay events")
    print("   👁  Watch Restaurant Intelligence Agent in next cycle")


# ══════════════════════════════════════════════════════════════
# ANOMALY 2: DEAD ZONE PRESSURE
# ── Drops the order density in 3 random peripheral zones to
#    near-zero, simulating a dead zone forming.
#    Dead Run Prevention Agent should detect this and flag
#    any pending orders going to those zones.
# ══════════════════════════════════════════════════════════════
def inject_dead_zone_pressure(conn):
    print("\n🔴 Injecting: Dead Zone Pressure")
    print("   Target: 3 peripheral zones → near-zero density")

    now = datetime.now()

    with conn.cursor() as cur:
        # Pick 3 peripheral zones dynamically — works with any seeded city set
        cur.execute("""
            SELECT id, name
            FROM zones
            WHERE is_active = TRUE
              AND (
                  boundary_geojson->>'zone_type' = 'peripheral'
                  OR name ILIKE '%peripheral%'
              )
            ORDER BY RANDOM()
            LIMIT 3
        """)
        target_zones = cur.fetchall()

        if not target_zones:
            # Fall back to any 3 active zones if no peripheral zones found
            cur.execute("SELECT id, name FROM zones WHERE is_active = TRUE ORDER BY RANDOM() LIMIT 3")
            target_zones = cur.fetchall()

        if not target_zones:
            print("   ❌ No active zones found")
            return

        zone_names = [row[1] for row in target_zones]
        snapshots = []
        for zone_id, zone_name in target_zones:
            # Plant 4 consecutive near-zero snapshots (last hour)
            for i in range(4):
                ts = now - timedelta(minutes=i * 15)
                snapshots.append((
                    str(zone_id), ts,
                    random.randint(0, 2),        # near-zero order count
                    0,                            # no active riders
                    random.uniform(0.01, 0.04),  # near-zero density
                    random.uniform(0.05, 0.15),  # very low stress ratio
                ))

        execute_values(conn.cursor(), """
            INSERT INTO zone_density_snapshots
                (zone_id, timestamp, order_count, active_rider_count,
                 density_score, stress_ratio)
            VALUES %s
        """, snapshots)

    conn.commit()
    print(f"   Zones affected: {', '.join(zone_names)}")
    print(f"   ✅ Injected {len(snapshots)} near-zero density snapshots")
    print("   👁  Watch Dead Run Prevention Agent in next cycle")
    print("   👁  Zone Intelligence Agent should update sister zone recommendations")


# ══════════════════════════════════════════════════════════════
# ANOMALY 3: RIDER EARNINGS DEGRADATION
# ── Takes 3 specific riders and plants a pattern of bad
#    sessions over the last 5 days. Earnings Guardian Agent
#    should detect the multi-session pattern and escalate
#    a churn signal to the Supervisor.
# ══════════════════════════════════════════════════════════════
def inject_rider_earnings_degradation(conn):
    print("\n🔴 Injecting: Rider Earnings Degradation")
    print("   Target: 3 supplementary riders — 5 consecutive bad sessions")

    with conn.cursor() as cur:
        # Get 3 supplementary riders
        cur.execute("""
            SELECT id, name FROM riders
            WHERE persona_type = 'supplementary'
            AND is_active = TRUE
            ORDER BY RANDOM()
            LIMIT 3
        """)
        target_riders = cur.fetchall()

        if not target_riders:
            print("   ❌ No supplementary riders found")
            return

        degraded_rider_ids = []
        now = datetime.now()

        for rider_id, rider_name in target_riders:
            print(f"   Rider: {rider_name}")

            # Plant 5 bad sessions (last 5 days)
            for day_offset in range(1, 6):
                session_date = (now - timedelta(days=day_offset)).date()

                # Bad EPH: 55-70 (well below 90 target for supplementary)
                bad_eph = random.uniform(55, 70)
                health_score = (bad_eph / 90.0) * 100   # below threshold

                # Check if session exists
                cur.execute("""
                    SELECT id FROM rider_sessions
                    WHERE rider_id = %s AND session_date = %s
                """, (rider_id, session_date))
                existing = cur.fetchone()

                if existing:
                    cur.execute("""
                        UPDATE rider_sessions
                        SET eph = %s,
                            health_score = %s,
                            below_threshold = TRUE,
                            dead_runs_count = dead_runs_count + %s
                        WHERE rider_id = %s AND session_date = %s
                    """, (bad_eph, health_score,
                          random.randint(1, 3),   # extra dead runs
                          rider_id, session_date))
                else:
                    cur.execute("""
                        INSERT INTO rider_sessions
                            (id, rider_id, session_date, total_orders,
                             total_earnings, total_distance_km,
                             idle_time_mins, dead_runs_count,
                             long_distance_count, eph, health_score, below_threshold)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    """, (
                        str(uuid.uuid4()), rider_id, session_date,
                        random.randint(8, 14),          # low order count
                        bad_eph * random.uniform(4, 5), # earnings
                        random.uniform(15, 30),          # distance
                        random.uniform(60, 90),          # high idle time
                        random.randint(2, 4),            # many dead runs
                        random.randint(2, 4),
                        bad_eph, health_score
                    ))

            degraded_rider_ids.append(rider_id)

    conn.commit()
    print(f"\n   ✅ Planted degradation pattern for {len(target_riders)} riders")
    print("   👁  Watch Earnings Guardian Agent — should escalate churn signals")
    print("   👁  Supervisor briefing should show these as high-priority alerts")

    return degraded_rider_ids


# ══════════════════════════════════════════════════════════════
# RESET — removes planted anomalies
# ══════════════════════════════════════════════════════════════
def reset_anomalies(conn):
    print("\n🔄 Resetting planted anomalies...")
    with conn.cursor() as cur:
        # Reset restaurant risk scores
        cur.execute("UPDATE restaurants SET last_risk_score = 0.2 WHERE last_risk_score > 0.7")
        print(f"  Reset {cur.rowcount} restaurant risk scores")

        # Remove recent injected delay events (last hour)
        cur.execute("""
            DELETE FROM restaurant_delay_events
            WHERE timestamp > NOW() - INTERVAL '2 hours'
        """)
        print(f"  Removed {cur.rowcount} injected delay events")

        # Remove recent zone snapshots with near-zero density
        cur.execute("""
            DELETE FROM zone_density_snapshots
            WHERE timestamp > NOW() - INTERVAL '2 hours'
            AND density_score < 0.05
        """)
        print(f"  Removed {cur.rowcount} dead zone snapshots")

        # Reset bad sessions (restore to normal EPH)
        cur.execute("""
            UPDATE rider_sessions
            SET eph = 82.0,
                health_score = 91.1,
                below_threshold = FALSE,
                dead_runs_count = 0
            WHERE eph < 72 AND session_date >= NOW()::date - 6
        """)
        print(f"  Reset {cur.rowcount} degraded rider sessions")

        # Clear agent outputs from last cycle
        cur.execute("DELETE FROM rider_churn_signals WHERE created_at > NOW() - INTERVAL '2 hours'")
        cur.execute("DELETE FROM rider_alerts WHERE created_at > NOW() - INTERVAL '2 hours'")

    conn.commit()
    print("  ✅ Anomalies reset")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
COMMANDS = {
    "restaurant_delay":         inject_restaurant_delay,
    "dead_zone_pressure":       inject_dead_zone_pressure,
    "rider_earnings_degradation": inject_rider_earnings_degradation,
    "reset":                    reset_anomalies,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS and sys.argv[1] != "all":
        print("Usage: python inject_anomaly.py <command>")
        print("\nCommands:")
        for cmd in COMMANDS:
            print(f"  {cmd}")
        print("  all    — inject all three anomalies")
        print("  reset  — remove all planted anomalies")
        sys.exit(1)

    command = sys.argv[1]
    conn    = get_conn()

    print("\n" + "="*55)
    print("  ARIA — Anomaly Injector")
    print("="*55)
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if command == "all":
        inject_restaurant_delay(conn)
        inject_dead_zone_pressure(conn)
        inject_rider_earnings_degradation(conn)
    else:
        COMMANDS[command](conn)

    conn.close()

    print("\n" + "="*55)
    print("  Anomaly injection complete.")
    print("  Trigger a Supervisor cycle to see ARIA respond:")
    print("  curl -X POST http://localhost:8001/api/trigger-cycle \\")
    print("       -H 'X-API-Key: your_api_key'")
    print("="*55)
