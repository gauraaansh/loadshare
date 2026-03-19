"""
ARIA — Event Stream: Session Manager
======================================
Handles rider session lifecycle: open, update on delivery, close.

open_session            — inserts rider_sessions row (shift_start = sim now)
update_session_on_delivery — increments earnings/orders/distance/idle/dead_runs
close_session           — sets shift_end, computes final EPH, updates below_threshold
                          removes rider from Redis, publishes session_updates event
"""

import json
import uuid
import structlog

from redis_client import key_active_riders, key_rider_state, CHANNEL_SESSION_UPDATES
from config import DEAD_ZONE_STRESS_THRESHOLD

log = structlog.get_logger()

EPH_TARGET_SUPPLEMENTARY = 90.0   # matches .env default


async def open_session(rider_id: str, clock, db_pool) -> str:
    """
    Open a new rider session.
    Uses ON CONFLICT so a re-triggered online event is idempotent.
    Returns the session_id (UUID string).
    """
    session_id = str(uuid.uuid4())
    sim_now    = clock.now()

    async with db_pool.acquire() as conn:
        # If a session is currently open for today, reopen it (e.g. transient reconnect).
        # Only match sessions with shift_end IS NULL — do NOT reopen closed sessions,
        # as that would carry over stale total_orders/total_earnings from a previous shift.
        row = await conn.fetchrow(
            "SELECT id FROM rider_sessions WHERE rider_id=$1 AND session_date=$2 AND shift_end IS NULL",
            rider_id, sim_now.date(),
        )
        if row:
            session_id = str(row["id"])
            await conn.execute(
                "UPDATE rider_sessions SET shift_start=$1, shift_end=NULL, updated_at=NOW() WHERE id=$2",
                sim_now, session_id,
            )
        else:
            await conn.execute(
                """
                INSERT INTO rider_sessions
                    (id, rider_id, session_date, shift_start)
                VALUES ($1, $2, $3, $4)
                """,
                session_id, rider_id, sim_now.date(), sim_now,
            )

    log.debug("session opened", rider_id=rider_id, session_id=session_id)
    return session_id


async def update_session_on_delivery(
    session_id:      str,
    fare_rs:         float,
    distance_km:     float,
    is_long_distance: bool,
    idle_time_mins:  float,
    is_dead_run:     bool,
    db_pool,
) -> None:
    """Increment session counters after each delivered order."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE rider_sessions SET
                total_orders        = total_orders + 1,
                total_earnings      = total_earnings + $1,
                total_distance_km   = total_distance_km + $2,
                long_distance_count = long_distance_count + $3,
                dead_runs_count     = dead_runs_count + $4,
                idle_time_mins      = idle_time_mins + $5,
                updated_at          = NOW()
            WHERE id = $6
            """,
            fare_rs,
            distance_km,
            1 if is_long_distance else 0,
            1 if is_dead_run else 0,
            max(0.0, idle_time_mins),
            session_id,
        )


async def close_session(
    rider_id:   str,
    session_id: str,
    clock,
    db_pool,
    redis_client,
) -> None:
    """
    Close the rider's active session.
    Computes final EPH and below_threshold flag.
    Cleans up Redis state and publishes session close event.
    """
    sim_now = clock.now()

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT shift_start, total_earnings FROM rider_sessions WHERE id=$1",
            session_id,
        )
        if not row:
            log.warning("session not found on close", session_id=session_id)
            await _cleanup_redis(rider_id, redis_client)
            return

        shift_start   = row["shift_start"]
        hours_elapsed = max((sim_now - shift_start).total_seconds() / 3600, 1 / 60)
        earnings      = float(row["total_earnings"])
        eph           = round(earnings / hours_elapsed, 2)
        below         = eph < EPH_TARGET_SUPPLEMENTARY

        await conn.execute(
            """
            UPDATE rider_sessions SET
                shift_end        = $1,
                eph              = $2,
                below_threshold  = $3,
                updated_at       = NOW()
            WHERE id = $4
            """,
            sim_now, eph, below, session_id,
        )

    log.info("session closed", rider_id=rider_id, eph=eph, below_threshold=below)

    await _cleanup_redis(rider_id, redis_client)
    await redis_client.publish(
        CHANNEL_SESSION_UPDATES,
        json.dumps({"event": "session_closed", "rider_id": rider_id, "eph": eph, "below_threshold": below}),
    )


async def _cleanup_redis(rider_id: str, redis_client) -> None:
    await redis_client.srem(key_active_riders(), rider_id)
    await redis_client.delete(key_rider_state(rider_id))
