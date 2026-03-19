"""
ARIA — Event Stream: FastAPI App
==================================
Entrypoint. Exposes:

  Control API:
    GET  /health
    GET  /simulation/status
    POST /simulation/start
    POST /simulation/pause
    POST /simulation/resume
    POST /simulation/stop

  Rider management:
    POST /riders                          — create new rider (GPS → auto zone)
    POST /riders/{rider_id}/session/start — manually bring rider online
    POST /riders/{rider_id}/session/stop  — manually take rider offline

  Anomaly injection (demo):
    POST /anomaly/inject/{anomaly_type}   — restaurant_delay | dead_zone_pressure
                                            | rider_earnings_degradation
    POST /anomaly/reset

The simulation starts automatically on service startup.
"""

import uuid
import json
import math
import random
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
import structlog
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from clock       import SimClock
from simulation  import Simulator
from redis_client import init_redis, close_redis, get_redis
from config      import DATABASE_URL, TIME_SCALE, PORT, CYCLE_INTERVAL_MINS

log = structlog.get_logger()

# ── Global singletons ─────────────────────────────────────────
_db_pool:   asyncpg.Pool | None = None
_simulator: Simulator    | None = None
_sim_task:  asyncio.Task | None = None


# ══════════════════════════════════════════════════════════════
# LIFESPAN
# ══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db_pool, _simulator, _sim_task

    _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=4, max_size=16)
    redis    = await init_redis()
    clock    = SimClock(time_scale=TIME_SCALE)

    # Reset all restaurant queue counters — stale Redis values from a previous
    # run cause wrong congestion readings (e.g. -2 after active_orders dict cleared on restart).
    queue_keys = await redis.keys("aria:restaurant_queue:*")
    if queue_keys:
        await redis.delete(*queue_keys)
        log.info("restaurant queues reset on startup", count=len(queue_keys))

    # Close any sessions left open by a previous event-stream process.
    # Without this, rider_map in zone_engine accumulates stale counts that make
    # every zone appear to have riders (rider_count > 0) but no orders, producing
    # density_score=0 and stress_ratio=0 (dead) for all zones on restart.
    async with _db_pool.acquire() as _conn:
        result = await _conn.execute(
            "UPDATE rider_sessions SET shift_end = NOW() "
            "WHERE session_date = CURRENT_DATE AND shift_end IS NULL"
        )
        # asyncpg returns "UPDATE N" string
        closed = int(result.split()[-1]) if result else 0
        if closed:
            log.info("stale sessions closed on startup", count=closed)

    _simulator = Simulator(clock, _db_pool, redis)
    await _simulator.load_reference_data()

    # Start simulation automatically
    _sim_task = asyncio.create_task(_simulator.run())
    log.info("event stream started", time_scale=TIME_SCALE,
             cycle_interval_mins=CYCLE_INTERVAL_MINS)

    yield

    _simulator.stop()
    if _sim_task:
        _sim_task.cancel()
        try:
            await _sim_task
        except asyncio.CancelledError:
            pass
    await close_redis()
    await _db_pool.close()
    log.info("event stream stopped")


app = FastAPI(title="ARIA Event Stream", version="1.0.0", lifespan=lifespan)


# ══════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════
# SIMULATION CONTROL
# ══════════════════════════════════════════════════════════════

@app.get("/simulation/status")
async def simulation_status():
    if not _simulator:
        return {"running": False}
    clock = _simulator.clock
    return {
        "running":              _simulator.running,
        "paused":               clock.is_paused,
        "time_scale":           clock.time_scale,
        "cycle_interval_mins":  CYCLE_INTERVAL_MINS,
        "sim_time":             clock.now().isoformat(),
        "active_riders":        len(_simulator.active_riders),
        "active_orders":        len(_simulator.active_orders),
    }


@app.post("/simulation/pause")
async def simulation_pause():
    if not _simulator:
        raise HTTPException(503, "Simulator not initialised")
    _simulator.clock.pause()
    _simulator.paused = True
    return {"status": "paused", "sim_time": _simulator.clock.now().isoformat()}


@app.post("/simulation/resume")
async def simulation_resume():
    if not _simulator:
        raise HTTPException(503, "Simulator not initialised")
    _simulator.clock.resume()
    _simulator.paused = False
    return {"status": "resumed", "sim_time": _simulator.clock.now().isoformat()}


@app.post("/simulation/stop")
async def simulation_stop():
    global _sim_task
    if not _simulator:
        raise HTTPException(503, "Simulator not initialised")
    _simulator.stop()
    if _sim_task:
        _sim_task.cancel()
    return {"status": "stopped"}


@app.post("/simulation/set-timescale")
async def set_timescale(value: float = Query(..., gt=0, le=500)):
    if not _simulator:
        raise HTTPException(503, "Simulator not initialised")
    _simulator.clock.set_time_scale(value)
    return {"time_scale": value, "sim_time": _simulator.clock.now().isoformat()}


@app.post("/simulation/start")
async def simulation_start():
    global _sim_task
    if not _simulator:
        raise HTTPException(503, "Simulator not initialised")
    if _simulator.running:
        return {"status": "already_running"}
    _sim_task = asyncio.create_task(_simulator.run())
    return {"status": "started"}


# ══════════════════════════════════════════════════════════════
# RIDER MANAGEMENT
# ══════════════════════════════════════════════════════════════

class NewRiderRequest(BaseModel):
    name:         str
    lat:          float
    lng:          float
    vehicle_type: str = "bike"
    phone:        Optional[str] = None


@app.post("/riders", status_code=201)
async def create_rider(req: NewRiderRequest):
    """
    Create a new rider. Finds nearest zone by haversine, inserts into DB,
    adds to simulator's rider pool, and immediately brings online.
    """
    if not _simulator or not _db_pool:
        raise HTTPException(503, "Simulator not initialised")

    # Find nearest zone
    nearest_zone_id = _find_nearest_zone(req.lat, req.lng, _simulator.zones)
    if not nearest_zone_id:
        raise HTTPException(400, "No active zones found near provided coordinates")

    rider_id = str(uuid.uuid4())
    async with _db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO riders (id, name, phone, home_zone_id, vehicle_type, is_active)
            VALUES ($1,$2,$3,$4,$5,TRUE)
            """,
            rider_id, req.name, req.phone, nearest_zone_id, req.vehicle_type,
        )

    # Add to simulator reference pool and bring online immediately
    rider = {
        "id":           rider_id,
        "name":         req.name,
        "home_zone_id": nearest_zone_id,
        "persona_type": None,   # unclassified — will be set after first session closes
        "vehicle_type": req.vehicle_type,
        "is_active":    True,
    }
    _simulator.all_riders.append(rider)
    await _simulator._bring_online(rider, _simulator.clock.now())

    zone = _simulator.zones.get(nearest_zone_id, {})
    return {
        "rider_id":      rider_id,
        "home_zone_id":  nearest_zone_id,
        "home_zone_name": zone.get("name", ""),
        "city":          zone.get("city", ""),
        "status":        "online",
    }


@app.post("/riders/{rider_id}/session/start")
async def rider_session_start(rider_id: str):
    if not _simulator:
        raise HTTPException(503, "Simulator not initialised")
    if rider_id in _simulator.active_riders:
        return {"status": "already_online"}

    rider = next((r for r in _simulator.all_riders if r["id"] == rider_id), None)
    if not rider:
        raise HTTPException(404, f"Rider {rider_id} not found")

    await _simulator._bring_online(rider, _simulator.clock.now())
    return {"status": "online", "rider_id": rider_id}


@app.post("/riders/{rider_id}/session/stop")
async def rider_session_stop(rider_id: str):
    if not _simulator:
        raise HTTPException(503, "Simulator not initialised")
    state = _simulator.active_riders.get(rider_id)
    if not state:
        return {"status": "already_offline"}
    await _simulator._bring_offline(rider_id, state)
    return {"status": "offline", "rider_id": rider_id}


def _find_nearest_zone(lat: float, lng: float, zones: dict) -> Optional[str]:
    best_id, best_dist = None, float("inf")
    for zone_id, z in zones.items():
        d = _haversine(lat, lng, z["centroid_lat"], z["centroid_lng"])
        if d < best_dist:
            best_dist = d
            best_id   = zone_id
    return best_id


def _haversine(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ══════════════════════════════════════════════════════════════
# ANOMALY INJECTION
# ══════════════════════════════════════════════════════════════

@app.post("/anomaly/inject/{anomaly_type}")
async def inject_anomaly(anomaly_type: str):
    """
    Inject a demo anomaly into the live DB.
    anomaly_type: restaurant_delay | dead_zone_pressure | rider_earnings_degradation
    """
    if not _db_pool:
        raise HTTPException(503, "DB not initialised")
    if anomaly_type not in ("restaurant_delay", "dead_zone_pressure", "rider_earnings_degradation"):
        raise HTTPException(400, f"Unknown anomaly type: {anomaly_type}. "
                            "Use: restaurant_delay | dead_zone_pressure | rider_earnings_degradation")

    if anomaly_type == "restaurant_delay":
        result = await _inject_restaurant_delay()
    elif anomaly_type == "dead_zone_pressure":
        result = await _inject_dead_zone_pressure()
    else:
        result = await _inject_rider_earnings_degradation()

    return {"anomaly_type": anomaly_type, **result}


@app.post("/anomaly/reset")
async def reset_anomalies():
    async with _db_pool.acquire() as conn:
        r1 = await conn.execute(
            "UPDATE restaurants SET last_risk_score=0.2 WHERE last_risk_score > 0.7"
        )
        r2 = await conn.execute(
            "DELETE FROM restaurant_delay_events WHERE timestamp > NOW() - INTERVAL '2 hours'"
        )
        r3 = await conn.execute(
            "DELETE FROM zone_density_snapshots "
            "WHERE timestamp > NOW() - INTERVAL '2 hours' AND density_score < 0.05"
        )
        r4 = await conn.execute(
            "UPDATE rider_sessions SET eph=82.0, health_score=91.1, "
            "below_threshold=FALSE, dead_runs_count=0 "
            "WHERE eph < 72 AND session_date >= NOW()::date - 6"
        )
        await conn.execute(
            "DELETE FROM rider_churn_signals WHERE created_at > NOW() - INTERVAL '2 hours'"
        )
        await conn.execute(
            "DELETE FROM rider_alerts WHERE created_at > NOW() - INTERVAL '2 hours'"
        )
    return {"status": "reset", "restaurants_reset": r1, "delay_events_removed": r2,
            "zone_snapshots_removed": r3, "sessions_reset": r4}


# ── Anomaly helpers ───────────────────────────────────────────

async def _inject_restaurant_delay() -> dict:
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                r.id,
                r.name,
                r.avg_prep_time_mins,
                ARRAY_AGG(o.id ORDER BY o.created_at DESC) AS order_ids
            FROM restaurants r
            JOIN orders o ON o.restaurant_id = r.id
            WHERE r.is_active = TRUE
            GROUP BY r.id, r.name, r.avg_prep_time_mins
            ORDER BY RANDOM()
            LIMIT 1
            """
        )
        if not row:
            raise HTTPException(404, "No active restaurants with orders found")

        rest_id   = str(row["id"])
        rest_name = row["name"]
        order_ids = [str(x) for x in (row["order_ids"] or [])]
        if not order_ids:
            raise HTTPException(500, "Selected restaurant has no orders for anomaly injection")

        avg_prep  = float(row["avg_prep_time_mins"] or 20.0)
        spiked    = avg_prep * 3.2
        now       = datetime.utcnow()

        # 8 recent delay events showing the spike
        events = []
        for i in range(8):
            ts     = now - timedelta(minutes=i * 5)
            actual = spiked + random.uniform(-3, 5)
            order_id = order_ids[i % len(order_ids)]
            events.append((
                str(uuid.uuid4()), rest_id, order_id, ts,
                avg_prep, round(actual, 1), round(actual - avg_prep, 1),
                None, ts.hour, ts.weekday(),
            ))

        await conn.executemany(
            """
            INSERT INTO restaurant_delay_events
                (id, restaurant_id, order_id, timestamp,
                 expected_prep_mins, actual_prep_mins, delay_mins,
                 weather_condition, hour_of_day, day_of_week)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            events,
        )
        await conn.execute(
            "UPDATE restaurants SET avg_prep_time_mins=$1, last_risk_score=0.85 WHERE id=$2",
            spiked, rest_id,
        )

    return {
        "restaurant_id":   rest_id,
        "restaurant_name": rest_name,
        "normal_prep_mins": round(avg_prep, 1),
        "spiked_prep_mins": round(spiked, 1),
        "events_injected":  8,
        "message": "Watch Restaurant Intelligence Agent in next cycle",
    }


async def _inject_dead_zone_pressure() -> dict:
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, name FROM zones
            WHERE is_active=TRUE
              AND (boundary_geojson->>'zone_type' = 'peripheral'
                   OR name ILIKE '%peripheral%')
            ORDER BY RANDOM() LIMIT 3
            """
        )
        if not rows:
            rows = await conn.fetch(
                "SELECT id::text, name FROM zones WHERE is_active=TRUE ORDER BY RANDOM() LIMIT 3"
            )
        if not rows:
            raise HTTPException(404, "No active zones found")

        now       = datetime.utcnow()
        snapshots = []
        for row in rows:
            for i in range(4):
                ts = now - timedelta(minutes=i * 15)
                snapshots.append((
                    row["id"], ts,
                    random.randint(0, 2), 0,
                    random.uniform(0.01, 0.04),
                    random.uniform(0.05, 0.15),
                    0,
                ))

        await conn.executemany(
            """
            INSERT INTO zone_density_snapshots
                (zone_id, timestamp, order_count, active_rider_count,
                 density_score, stress_ratio, order_delta)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            snapshots,
        )

    zone_names = [r["name"] for r in rows]
    return {
        "zones_affected":      zone_names,
        "snapshots_injected":  len(snapshots),
        "message": "Watch Dead Run Prevention + Zone Intelligence agents in next cycle",
    }


async def _inject_rider_earnings_degradation() -> dict:
    async with _db_pool.acquire() as conn:
        riders = await conn.fetch(
            "SELECT id, name FROM riders WHERE persona_type='supplementary' "
            "AND is_active=TRUE ORDER BY RANDOM() LIMIT 3"
        )
        if not riders:
            raise HTTPException(404, "No supplementary riders found")

        now = datetime.utcnow()
        affected = []
        for rider in riders:
            rider_id = str(rider["id"])
            for day_offset in range(1, 6):
                session_date = (now - timedelta(days=day_offset)).date()
                bad_eph      = random.uniform(55, 70)
                health_score = (bad_eph / 90.0) * 100

                existing = await conn.fetchrow(
                    "SELECT id FROM rider_sessions WHERE rider_id=$1 AND session_date=$2",
                    rider_id, session_date,
                )
                if existing:
                    await conn.execute(
                        "UPDATE rider_sessions SET eph=$1, health_score=$2, "
                        "below_threshold=TRUE, dead_runs_count=dead_runs_count+$3 "
                        "WHERE rider_id=$4 AND session_date=$5",
                        bad_eph, health_score, random.randint(1, 3), rider_id, session_date,
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO rider_sessions
                            (id, rider_id, session_date, total_orders, total_earnings,
                             total_distance_km, idle_time_mins, dead_runs_count,
                             long_distance_count, eph, health_score, below_threshold)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE)
                        """,
                        str(uuid.uuid4()), rider_id, session_date,
                        random.randint(8, 14),
                        bad_eph * random.uniform(4, 5),
                        random.uniform(15, 30),
                        random.uniform(60, 90),
                        random.randint(2, 4),
                        random.randint(1, 3),
                        bad_eph, health_score,
                    )
            affected.append({"rider_id": rider_id, "name": rider["name"]})

    return {
        "riders_affected":   affected,
        "sessions_per_rider": 5,
        "message": "Watch Earnings Guardian — should escalate churn signals in next cycle",
    }
