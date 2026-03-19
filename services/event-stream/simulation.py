"""
ARIA — Event Stream: Simulator
================================
Core simulation engine. Four concurrent asyncio loops:

  _scheduler_loop      — riders come online / go offline (Poisson-inspired)
  _dispatcher_loop     — central dispatcher assigns orders to idle riders
  _order_pipeline_loop — advances order statuses based on sim time due-times
  _zone_snapshot_loop  — writes zone_density_snapshots every CYCLE_INTERVAL_MINS

Design notes:
  - All timing uses SimClock — never datetime.now() or asyncio.sleep() directly
  - due-times stored as sim datetimes; pipeline loop compares clock.now() >= due
  - Zone snapshot cadence = CYCLE_INTERVAL_MINS (same knob as supervisor cycle)
  - Restaurant queue model lives in Redis (ephemeral, real-time only)
  - On restart: lingering non-terminal orders are failed to avoid ghost state
"""

import uuid
import asyncio
import random
import json
from datetime import datetime, timedelta
from typing import Optional

import structlog

from config import (
    SIMULATION_PEAK_RIDERS, SIMULATION_OFFPEAK_RIDERS,
    PEAK_HOURS, SUPPLEMENTARY_SHIFT_HOURS, DEDICATED_SHIFT_HOURS,
    DEAD_ZONE_STRESS_THRESHOLD,
    PIPELINE_TICK_SECS, DISPATCHER_TICK_SECS, SCHEDULER_TICK_SECS,
    CYCLE_INTERVAL_MINS,
)
from order_factory import (
    haversine_km, pick_weather, pick_traffic,
    travel_mins, compute_prep_time, pick_delivery_zone, compute_fare,
)
from session_manager import open_session, close_session, update_session_on_delivery
from zone_engine import snapshot_all_zones
from redis_client import (
    key_active_riders, key_rider_state, key_restaurant_queue,
    CHANNEL_ORDER_UPDATES,
)

log = structlog.get_logger()


# ── Rider online probability (Poisson-inspired) ───────────────

def rider_online_prob(hour: int, persona: str) -> float:
    """
    Probability that a given offline rider comes online this scheduler tick.
    Checked every SCHEDULER_TICK_SECS real seconds.
    Supplementary riders spike at morning + evening rush.
    Dedicated riders maintain elevated probability all day.
    """
    if persona == "dedicated":
        if hour in PEAK_HOURS:
            return 0.20
        if 8 <= hour < 22:
            return 0.10
        return 0.01
    # supplementary
    if hour in {7, 8, 9}:
        return 0.18
    if hour in {12, 13}:
        return 0.10
    if hour in {18, 19, 20, 21, 22}:
        return 0.22
    return 0.02


# ══════════════════════════════════════════════════════════════

class Simulator:

    def __init__(self, clock, db_pool, redis_client):
        self.clock        = clock
        self.db_pool      = db_pool
        self.redis        = redis_client
        self.running      = False
        self.paused       = False

        # ── Reference data (loaded once at startup) ──────────
        self.zones:              dict[str, dict]       = {}   # zone_id → zone
        self.all_riders:         list[dict]            = []   # all seeded riders
        self.restaurants_by_zone: dict[str, list[dict]] = {}  # zone_id → [restaurants]

        # ── Live simulation state ─────────────────────────────
        # active_riders: rider_id → {session_id, shift_ends_at, persona, home_zone_id}
        self.active_riders:       dict[str, dict]            = {}
        # active_orders: order_id → full order state dict
        self.active_orders:       dict[str, dict]            = {}
        # rider_current_order: rider_id → order_id or None (None = idle)
        self.rider_current_order: dict[str, Optional[str]]   = {}
        # rider_last_delivery: rider_id → sim datetime of last delivered order
        self.rider_last_delivery: dict[str, Optional[datetime]] = {}
        # zone_density_cache: zone_id → latest snapshot data (refreshed by zone_engine)
        self.zone_density_cache:  dict[str, dict]            = {}

    # ══════════════════════════════════════════════════════════
    # STARTUP
    # ══════════════════════════════════════════════════════════

    async def load_reference_data(self) -> None:
        """Load zones, riders, restaurants into memory. Fail lingering orders."""
        async with self.db_pool.acquire() as conn:
            # Zones
            zone_rows = await conn.fetch(
                """
                SELECT id::text, name, city, centroid_lat, centroid_lng,
                       is_active, sister_zone_ids,
                       boundary_geojson->>'zone_type' AS zone_type
                FROM zones WHERE is_active = TRUE
                """
            )
            for z in zone_rows:
                sisters = [str(s) for s in (z["sister_zone_ids"] or [])]
                self.zones[z["id"]] = {
                    "id":           z["id"],
                    "name":         z["name"],
                    "city":         z["city"],
                    "centroid_lat": float(z["centroid_lat"]),
                    "centroid_lng": float(z["centroid_lng"]),
                    "zone_type":    z["zone_type"] or "residential",
                    "sister_zone_ids": sisters,
                    "is_active":    z["is_active"],
                }

            # Riders
            rider_rows = await conn.fetch(
                """
                SELECT id::text, name, home_zone_id::text,
                       persona_type, vehicle_type, is_active
                FROM riders WHERE is_active = TRUE
                """
            )
            self.all_riders = [dict(r) for r in rider_rows]

            # Restaurants
            rest_rows = await conn.fetch(
                """
                SELECT id::text, name, zone_id::text,
                       lat, lng, avg_prep_time_mins
                FROM restaurants WHERE is_active = TRUE
                """
            )
            for r in rest_rows:
                self.restaurants_by_zone.setdefault(r["zone_id"], []).append(dict(r))

            # Fail any orders left open from a previous run
            failed = await conn.execute(
                """
                UPDATE orders
                SET status = 'failed', failed_at = NOW(),
                    failure_reason = 'simulation_restart'
                WHERE status NOT IN ('delivered', 'failed')
                """
            )

            # Close any rider sessions left open from a previous run.
            # Ghost sessions accumulate when the sim is restarted — their
            # shift_start timestamps may be in the future relative to the new
            # sim clock (if time_scale changed between runs), causing the
            # Earnings Guardian to compute hours_elapsed=0 → EPH=∞.
            # Set session_date='1970-01-01' so open_session() never finds and
            # reopens these stale rows (which carry over old earnings data).
            closed_sessions = await conn.execute(
                "UPDATE rider_sessions SET shift_end = NOW(), session_date = '1970-01-01', "
                "updated_at = NOW() WHERE shift_end IS NULL"
            )

        log.info(
            "reference data loaded",
            zones=len(self.zones),
            riders=len(self.all_riders),
            restaurants=sum(len(v) for v in self.restaurants_by_zone.values()),
            stale_orders_failed=failed,
            stale_sessions_closed=closed_sessions,
        )

    # ══════════════════════════════════════════════════════════
    # MAIN LOOP
    # ══════════════════════════════════════════════════════════

    async def run(self) -> None:
        self.running = True
        log.info("simulation started", time_scale=self.clock.time_scale,
                 cycle_interval_mins=CYCLE_INTERVAL_MINS)
        await asyncio.gather(
            self._scheduler_loop(),
            self._dispatcher_loop(),
            self._order_pipeline_loop(),
            self._zone_snapshot_loop(),
        )

    def stop(self) -> None:
        self.running = False

    # ══════════════════════════════════════════════════════════
    # LOOP 1 — Scheduler: riders online / offline
    # ══════════════════════════════════════════════════════════

    async def _scheduler_loop(self) -> None:
        while self.running:
            if not self.paused:
                now    = self.clock.now()
                hour   = now.hour
                target = SIMULATION_PEAK_RIDERS if hour in PEAK_HOURS else SIMULATION_OFFPEAK_RIDERS

                # Bring riders offline whose shift has ended
                for rider_id, state in list(self.active_riders.items()):
                    if now >= state["shift_ends_at"]:
                        await self._bring_offline(rider_id, state)

                # Bring new riders online up to target
                current = len(self.active_riders)
                if current < target:
                    offline = [r for r in self.all_riders if r["id"] not in self.active_riders]
                    random.shuffle(offline)
                    for rider in offline:
                        if len(self.active_riders) >= target:
                            break
                        persona = rider["persona_type"] or "supplementary"
                        if random.random() < rider_online_prob(hour, persona):
                            await self._bring_online(rider, now)

            await asyncio.sleep(SCHEDULER_TICK_SECS)

    async def _bring_online(self, rider: dict, now: datetime) -> None:
        persona     = rider["persona_type"] or "supplementary"
        shift_hours = (
            random.uniform(*DEDICATED_SHIFT_HOURS)
            if persona == "dedicated"
            else random.uniform(*SUPPLEMENTARY_SHIFT_HOURS)
        )
        session_id = await open_session(rider["id"], self.clock, self.db_pool)

        self.active_riders[rider["id"]] = {
            "rider_id":      rider["id"],
            "session_id":    session_id,
            "persona":       persona,
            "home_zone_id":  rider["home_zone_id"],
            "shift_ends_at": now + timedelta(hours=shift_hours),
        }
        self.rider_current_order[rider["id"]] = None
        self.rider_last_delivery[rider["id"]] = None

        await self.redis.sadd(key_active_riders(), rider["id"])
        await self.redis.hset(key_rider_state(rider["id"]), mapping={
            "session_id":   session_id,
            "home_zone_id": rider["home_zone_id"],
            "status":       "idle",
        })
        log.debug("rider online", rider_id=rider["id"],
                  shift_hours=round(shift_hours, 1), persona=persona)

    async def _bring_offline(self, rider_id: str, state: dict) -> None:
        # Cancel any active order gracefully
        order_id = self.rider_current_order.get(rider_id)
        if order_id and order_id in self.active_orders:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE orders SET status='failed', failed_at=NOW(), "
                    "failure_reason='rider_shift_ended' WHERE id=$1",
                    order_id,
                )
            rkey = self.active_orders[order_id].get("restaurant_queue_key")
            if rkey:
                await self.redis.decr(rkey)
            del self.active_orders[order_id]

        await close_session(rider_id, state["session_id"], self.clock, self.db_pool, self.redis)
        self.active_riders.pop(rider_id, None)
        self.rider_current_order.pop(rider_id, None)
        self.rider_last_delivery.pop(rider_id, None)
        log.debug("rider offline", rider_id=rider_id)

    # ══════════════════════════════════════════════════════════
    # LOOP 2 — Dispatcher: assign orders to idle riders
    # ══════════════════════════════════════════════════════════

    async def _dispatcher_loop(self) -> None:
        while self.running:
            if not self.paused:
                idle = [
                    (rid, state)
                    for rid, state in self.active_riders.items()
                    if self.rider_current_order.get(rid) is None
                ]
                for rider_id, state in idle:
                    try:
                        await self._assign_order(rider_id, state)
                    except Exception as e:
                        log.error("dispatch error", rider_id=rider_id, error=str(e))
            await asyncio.sleep(DISPATCHER_TICK_SECS)

    async def _assign_order(self, rider_id: str, state: dict) -> None:
        home_zone_id = state["home_zone_id"]
        zone         = self.zones.get(home_zone_id)
        if not zone:
            return

        # Pickup zone: 80% home, 20% sister
        sisters = zone.get("sister_zone_ids", [])
        pickup_zone_id = (
            random.choice(sisters)
            if sisters and random.random() < 0.2
            else home_zone_id
        )
        pickup_zone = self.zones.get(pickup_zone_id, zone)

        # Restaurant in pickup zone
        restaurants = self.restaurants_by_zone.get(pickup_zone_id, [])
        if not restaurants:
            return
        restaurant = random.choice(restaurants)

        # Queue-aware prep time
        queue_key = key_restaurant_queue(restaurant["id"])
        queue_len = int(await self.redis.get(queue_key) or 0)
        base_prep = restaurant.get("avg_prep_time_mins") or 20.0
        actual_prep = compute_prep_time(base_prep, queue_len)
        await self.redis.incr(queue_key)
        await self.redis.expire(queue_key, 3600)

        # Delivery zone (density-weighted, excludes pickup zone)
        delivery_zone_id = pick_delivery_zone(
            self.zones, pickup_zone_id, self.zone_density_cache
        )
        delivery_zone = self.zones.get(delivery_zone_id, pickup_zone)

        # Distance + flags
        dist_km = haversine_km(
            pickup_zone["centroid_lat"], pickup_zone["centroid_lng"],
            delivery_zone["centroid_lat"], delivery_zone["centroid_lng"],
        )
        is_long_distance   = dist_km > 5.0
        zone_type          = pickup_zone.get("zone_type", "residential")
        is_peak            = self.clock.is_peak()

        # Travel times (sim minutes)
        travel_to_rest = travel_mins(dist_km * 0.4, zone_type, is_peak)
        travel_to_del  = travel_mins(dist_km,       zone_type, is_peak)
        fare           = compute_fare(dist_km, is_long_distance)
        weather        = pick_weather(self.clock.hour())
        traffic        = pick_traffic(self.clock.hour(), zone_type)

        # Due times in sim time
        now_sim           = self.clock.now()
        rider_inbound_due = now_sim + timedelta(minutes=travel_to_rest)
        picked_up_due     = rider_inbound_due + timedelta(minutes=actual_prep)
        delivered_due     = picked_up_due     + timedelta(minutes=travel_to_del)

        # Delivery coords: centroid + jitter
        delivery_lat = delivery_zone["centroid_lat"] + random.uniform(-0.004, 0.004)
        delivery_lng = delivery_zone["centroid_lng"] + random.uniform(-0.004, 0.004)

        # Dead run flag: peripheral delivery zone with very low stress
        dest_cache       = self.zone_density_cache.get(delivery_zone_id, {})
        dest_stress      = float(dest_cache.get("stress_ratio", 0.5) or 0.5)
        delivery_z_type  = delivery_zone.get("zone_type", "residential")
        is_dead_run      = (
            dest_stress < DEAD_ZONE_STRESS_THRESHOLD
            and delivery_z_type == "peripheral"
        )

        order_id = str(uuid.uuid4())

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders (
                    id, rider_id, restaurant_id,
                    pickup_zone_id, delivery_zone_id,
                    pickup_lat, pickup_lng, delivery_lat, delivery_lng,
                    distance_km, is_long_distance, status,
                    weather_condition, traffic_density,
                    expected_prep_mins, actual_prep_mins,
                    expected_delivery_mins, assigned_at
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                    'assigned',$12,$13,$14,$15,$16,$17
                )
                """,
                order_id, rider_id, restaurant["id"],
                pickup_zone_id, delivery_zone_id,
                pickup_zone["centroid_lat"], pickup_zone["centroid_lng"],
                delivery_lat, delivery_lng,
                round(dist_km, 2), is_long_distance,
                weather, traffic,
                round(base_prep, 1), round(actual_prep, 1),
                round(travel_to_del, 1), now_sim,
            )

        self.active_orders[order_id] = {
            "order_id":             order_id,
            "rider_id":             rider_id,
            "session_id":           state["session_id"],
            "restaurant_id":        restaurant["id"],
            "pickup_zone_id":       pickup_zone_id,
            "delivery_zone_id":     delivery_zone_id,
            "delivery_zone_type":   delivery_z_type,
            "distance_km":          round(dist_km, 2),
            "is_long_distance":     is_long_distance,
            "is_dead_run":          is_dead_run,
            "fare_rs":              fare,
            "status":               "assigned",
            "assigned_at_sim":      now_sim,
            "rider_inbound_due":    rider_inbound_due,
            "picked_up_due":        picked_up_due,
            "delivered_due":        delivered_due,
            "restaurant_queue_key": queue_key,
        }
        self.rider_current_order[rider_id] = order_id

        await self.redis.hset(key_rider_state(rider_id), mapping={
            "current_order_id": order_id,
            "status":           "assigned",
            "pickup_zone_id":   pickup_zone_id,
            "delivery_zone_id": delivery_zone_id,
        })
        log.debug("order assigned", order_id=order_id, rider_id=rider_id,
                  dist_km=round(dist_km, 2), is_ld=is_long_distance)

    # ══════════════════════════════════════════════════════════
    # LOOP 3 — Order pipeline: advance statuses on due-time
    # ══════════════════════════════════════════════════════════

    async def _order_pipeline_loop(self) -> None:
        while self.running:
            if not self.paused:
                now       = self.clock.now()
                completed = []

                for order_id, order in list(self.active_orders.items()):
                    try:
                        status = order["status"]
                        if status == "assigned" and now >= order["rider_inbound_due"]:
                            await self._to_rider_inbound(order_id, order, now)
                        elif status == "rider_inbound" and now >= order["picked_up_due"]:
                            await self._to_picked_up(order_id, order, now)
                        elif status == "picked_up":
                            await self._to_en_route(order_id, order, now)
                        elif status == "en_route_delivery" and now >= order["delivered_due"]:
                            await self._to_delivered(order_id, order, now)
                            completed.append(order_id)
                    except Exception as e:
                        log.error("pipeline error", order_id=order_id, error=str(e))

                for order_id in completed:
                    self.active_orders.pop(order_id, None)

            await asyncio.sleep(PIPELINE_TICK_SECS)

    async def _to_rider_inbound(self, order_id: str, order: dict, now: datetime) -> None:
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status='rider_inbound', rider_inbound_at=$1 WHERE id=$2",
                now, order_id,
            )
        order["status"] = "rider_inbound"
        await self.redis.hset(key_rider_state(order["rider_id"]), "status", "rider_inbound")

    async def _to_picked_up(self, order_id: str, order: dict, now: datetime) -> None:
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT rider_inbound_at, expected_prep_mins FROM orders WHERE id=$1", order_id
            )
        rider_inbound_at = row["rider_inbound_at"] if row else None
        expected_prep    = float(row["expected_prep_mins"] or 20.0) if row else 20.0
        actual_prep      = (
            (now - rider_inbound_at).total_seconds() / 60
            if rider_inbound_at else expected_prep
        )
        delay_mins = actual_prep - expected_prep

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status='picked_up', picked_up_at=$1, actual_prep_mins=$2 WHERE id=$3",
                now, round(actual_prep, 1), order_id,
            )
            await conn.execute(
                """
                INSERT INTO restaurant_delay_events
                    (id, restaurant_id, order_id, timestamp,
                     expected_prep_mins, actual_prep_mins, delay_mins,
                     weather_condition, hour_of_day, day_of_week)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                str(uuid.uuid4()), order["restaurant_id"], order_id, now,
                round(expected_prep, 1), round(actual_prep, 1), round(delay_mins, 1),
                None, now.hour, now.weekday(),
            )

        # Release restaurant queue slot
        rkey = order.get("restaurant_queue_key")
        if rkey:
            await self.redis.decr(rkey)
            await self.redis.expire(rkey, 3600)

        order["status"]           = "picked_up"
        order["actual_prep_mins"] = actual_prep
        await self.redis.hset(key_rider_state(order["rider_id"]), "status", "picked_up")

    async def _to_en_route(self, order_id: str, order: dict, now: datetime) -> None:
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status='en_route_delivery' WHERE id=$1", order_id
            )
        order["status"] = "en_route_delivery"
        await self.redis.hset(key_rider_state(order["rider_id"]), "status", "en_route_delivery")

    async def _to_delivered(self, order_id: str, order: dict, now: datetime) -> None:
        rider_id = order["rider_id"]

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT assigned_at FROM orders WHERE id=$1", order_id)
        assigned_at          = row["assigned_at"] if row else None
        actual_delivery_mins = (
            (now - assigned_at).total_seconds() / 60 if assigned_at else 30.0
        )

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status='delivered', delivered_at=$1, "
                "actual_delivery_mins=$2 WHERE id=$3",
                now, round(actual_delivery_mins, 1), order_id,
            )

        # Idle time = gap between last delivery and this order's assignment
        last = self.rider_last_delivery.get(rider_id)
        idle_mins = max(
            0.0,
            (order["assigned_at_sim"] - last).total_seconds() / 60
            if last else 0.0,
        )

        await update_session_on_delivery(
            session_id=order["session_id"],
            fare_rs=order["fare_rs"],
            distance_km=order["distance_km"],
            is_long_distance=order["is_long_distance"],
            idle_time_mins=round(idle_mins, 1),
            is_dead_run=order["is_dead_run"],
            db_pool=self.db_pool,
        )

        self.rider_last_delivery[rider_id]  = now
        self.rider_current_order[rider_id]  = None
        order["status"] = "delivered"

        await self.redis.hset(key_rider_state(rider_id), mapping={
            "status": "idle", "current_order_id": "",
        })
        await self.redis.publish(
            CHANNEL_ORDER_UPDATES,
            json.dumps({"event": "delivered", "order_id": order_id,
                        "rider_id": rider_id, "fare_rs": order["fare_rs"]}),
        )
        log.debug("order delivered", order_id=order_id, rider_id=rider_id,
                  fare_rs=order["fare_rs"], dead_run=order["is_dead_run"])

    # ══════════════════════════════════════════════════════════
    # LOOP 4 — Zone snapshots (fires every CYCLE_INTERVAL_MINS)
    # ══════════════════════════════════════════════════════════

    async def _zone_snapshot_loop(self) -> None:
        # Brief warm-up so some orders exist before first snapshot
        await self.clock.sleep(60)
        while self.running:
            if not self.paused:
                try:
                    updates = await snapshot_all_zones(
                        self.zones, self.clock, self.db_pool, self.redis
                    )
                    self.zone_density_cache.update(updates)
                except Exception as e:
                    log.error("zone snapshot failed", error=str(e))
            # Sleep exactly one cycle interval (sim time)
            await self.clock.sleep(CYCLE_INTERVAL_MINS * 60)
