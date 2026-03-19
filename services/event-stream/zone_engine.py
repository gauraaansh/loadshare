"""
ARIA — Event Stream: Zone Engine
==================================
Computes and writes zone_density_snapshots every 15 sim minutes.

snapshot_all_zones:
  1. Counts active orders (status IN pending/assigned/rider_inbound/picked_up/en_route_delivery)
     per pickup zone + active riders per home zone
  2. Computes density_score and stress_ratio vs historical baseline
     (zone_density_hourly continuous aggregate, same hour last 28 days)
  3. Computes order_delta vs previous snapshot (surge detection signal)
  4. Writes zone_density_snapshots (hypertable)
  5. Updates Redis cache aria:zone_density:{zone_id} TTL 900s
  6. Publishes to aria:pubsub:zone_updates
"""

import json
import structlog

from redis_client import key_zone_density, CHANNEL_ZONE_UPDATES

log = structlog.get_logger()

STALE_SNAPSHOT_MINS = 20


async def snapshot_all_zones(
    zones: dict[str, dict],
    clock,
    db_pool,
    redis_client,
) -> dict[str, dict]:
    """
    Compute and write density snapshots for all active zones.
    Returns the updated cache dict: zone_id -> snapshot data.
    """
    sim_now  = clock.now()
    sim_hour = sim_now.hour
    sim_date = sim_now.date()

    async with db_pool.acquire() as conn:
        # ── Active order counts per pickup zone ──────────────
        order_counts = await conn.fetch(
            """
            SELECT pickup_zone_id::text AS zone_id, COUNT(*) AS cnt
            FROM orders
            WHERE status IN ('pending','assigned','rider_inbound','picked_up','en_route_delivery')
            GROUP BY pickup_zone_id
            """
        )
        order_map: dict[str, int] = {r["zone_id"]: int(r["cnt"]) for r in order_counts}

        # ── Active rider counts per home zone ─────────────────
        rider_counts = await conn.fetch(
            """
            SELECT r.home_zone_id::text AS zone_id, COUNT(*) AS cnt
            FROM rider_sessions rs
            JOIN riders r ON r.id = rs.rider_id
            WHERE rs.session_date = $1 AND rs.shift_end IS NULL
            GROUP BY r.home_zone_id
            """,
            sim_date,
        )
        rider_map: dict[str, int] = {r["zone_id"]: int(r["cnt"]) for r in rider_counts}

        # ── Historical baseline (same hour, last 28 days) ─────
        baselines = await conn.fetch(
            """
            SELECT zone_id::text, AVG(avg_density) AS baseline
            FROM zone_density_hourly
            WHERE EXTRACT(HOUR FROM bucket) = $1
              AND bucket > NOW() - INTERVAL '28 days'
            GROUP BY zone_id
            """,
            sim_hour,
        )
        baseline_map: dict[str, float] = {
            r["zone_id"]: float(r["baseline"]) for r in baselines if r["baseline"]
        }

        # ── Previous order counts per zone (for order_delta) ──
        prev_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (zone_id)
                zone_id::text, order_count
            FROM zone_density_snapshots
            ORDER BY zone_id, timestamp DESC
            """
        )
        prev_map: dict[str, int] = {r["zone_id"]: int(r["order_count"]) for r in prev_rows}

        # ── Build and insert snapshots ─────────────────────────
        insert_rows = []
        cache_updates: dict[str, dict] = {}

        for zone_id, zone in zones.items():
            order_count  = order_map.get(zone_id, 0)
            rider_count  = rider_map.get(zone_id, 0)
            order_delta  = order_count - prev_map.get(zone_id, order_count)

            # density_score: demand/supply ratio, normalised 0-1
            # max(rider_count, 1) prevents div-by-zero
            density_score = min(1.0, order_count / max(rider_count, 1) / 10.0)

            # stress_ratio: current vs historical baseline
            # When a zone has zero orders AND zero riders there is no signal —
            # treat as neutral (1.0) rather than dead (0.0).  "Dead" should mean
            # there IS demand but no supply, not that the zone is simply empty.
            baseline = baseline_map.get(zone_id)
            if order_count == 0 and rider_count == 0:
                stress_ratio = 1.0   # no data — neutral
            elif baseline and baseline > 0.0:
                stress_ratio = round(density_score / baseline, 4)
            elif density_score > 0.0:
                stress_ratio = round(density_score / 0.5, 4)   # 0.5 = neutral reference
            else:
                stress_ratio = 0.0

            insert_rows.append((
                zone_id, sim_now,
                order_count, rider_count,
                round(density_score, 4),
                round(stress_ratio, 4),
                order_delta,
            ))

            cache_updates[zone_id] = {
                "density_score":      str(round(density_score, 4)),
                "stress_ratio":       str(round(stress_ratio, 4)),
                "order_count":        str(order_count),
                "active_rider_count": str(rider_count),
                "order_delta":        str(order_delta),
                "timestamp":          sim_now.isoformat(),
            }

        if insert_rows:
            await conn.executemany(
                """
                INSERT INTO zone_density_snapshots
                    (zone_id, timestamp, order_count, active_rider_count,
                     density_score, stress_ratio, order_delta)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                insert_rows,
            )

    # ── Redis cache update ─────────────────────────────────────
    pipe = redis_client.pipeline()
    for zone_id, data in cache_updates.items():
        pipe.hset(key_zone_density(zone_id), mapping=data)
        pipe.expire(key_zone_density(zone_id), 900)
    await pipe.execute()

    # ── Publish ───────────────────────────────────────────────
    await redis_client.publish(
        CHANNEL_ZONE_UPDATES,
        json.dumps({
            "event":       "zone_snapshot",
            "sim_time":    sim_now.isoformat(),
            "zone_count":  len(insert_rows),
        }),
    )

    log.info("zone snapshots written", count=len(insert_rows), sim_hour=sim_hour)
    return cache_updates
