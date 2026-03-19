"""
ARIA — MCP Server: Autonomous Cycle Scheduler
===============================================
APScheduler fires every CYCLE_INTERVAL_MINS (default 15).
Each cycle runs all 5 agents sequentially, writes outputs to DB,
and broadcasts the final briefing to connected WebSocket clients.

Agent execution order matters:
  Zone → Restaurant → Dead Run → Earnings → Supervisor
  (Supervisor receives all four sub-agent result dicts)
"""

import time
import uuid
import asyncio
import urllib.request
import json
from datetime import datetime, timezone

import asyncpg
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval  import IntervalTrigger

from config import CYCLE_INTERVAL_MINS, EVENT_STREAM_HOST
from agents import ZoneAgent, RestaurantAgent, DeadRunAgent, EarningsAgent, SupervisorAgent

log = structlog.get_logger()

_scheduler: AsyncIOScheduler | None = None


def _fetch_sim_now() -> datetime:
    """
    Fetch current sim time from the event-stream /simulation/status endpoint.
    Falls back to real UTC time if event-stream is unreachable.
    This is a blocking call (urllib) but runs once per cycle before the async work begins.
    """
    try:
        with urllib.request.urlopen(f"{EVENT_STREAM_HOST}/simulation/status", timeout=2) as r:
            data = json.loads(r.read())
            sim_time_str = data.get("sim_time")
            if sim_time_str:
                return datetime.fromisoformat(sim_time_str.replace("Z", "+00:00"))
    except Exception:
        pass
    return datetime.now(timezone.utc)


async def run_cycle(pool: asyncpg.Pool, redis, ws_manager) -> dict:
    """
    Full 15-min cycle. Acquires one DB connection shared across all agents,
    runs them sequentially, returns the Supervisor's briefing dict.
    Individual agent failures are caught and logged — the cycle never crashes.
    """
    cycle_id    = str(uuid.uuid4())
    cycle_start = time.monotonic()

    # Fetch current sim time so agents use sim hour/day for baseline lookups,
    # not real UTC time (which is 3 AM while the sim may be at 13:xx).
    sim_now = await asyncio.to_thread(_fetch_sim_now)

    log.info("cycle started", cycle_id=cycle_id,
             sim_hour=sim_now.hour, sim_day=sim_now.weekday())

    # Notify frontend that a cycle is starting (drives agent pipeline animation)
    await ws_manager.broadcast({
        "type":          "cycle_start",
        "cycle_id":      cycle_id,
        "event_version": 1,
        "sent_at":       __import__("datetime").datetime.utcnow().isoformat() + "Z",
    })

    sub_results: dict = {}

    async with pool.acquire() as conn:

        # ── 1. Zone Intelligence ──────────────────────────────
        try:
            sub_results["zone"] = await ZoneAgent(conn, redis).run(cycle_id)
            log.info("zone agent done", cycle_id=cycle_id,
                     alerts=sub_results["zone"].get("alert_count", 0))
        except Exception as e:
            log.error("zone agent failed", cycle_id=cycle_id, error=str(e))
            sub_results["zone"] = {"status": "failed", "summary_text": str(e),
                                   "alert_count": 0, "severity": "normal"}

        # ── 2. Restaurant Intelligence ────────────────────────
        try:
            sub_results["restaurant"] = await RestaurantAgent(conn, redis).run(cycle_id, sim_now=sim_now)
            log.info("restaurant agent done", cycle_id=cycle_id,
                     alerts=sub_results["restaurant"].get("alert_count", 0))
        except Exception as e:
            log.error("restaurant agent failed", cycle_id=cycle_id, error=str(e))
            sub_results["restaurant"] = {"status": "failed", "summary_text": str(e),
                                         "alert_count": 0, "severity": "normal"}

        # ── 3. Dead Run Prevention ────────────────────────────
        try:
            sub_results["dead_run"] = await DeadRunAgent(conn, redis).run(cycle_id, sim_now=sim_now)
            log.info("dead_run agent done", cycle_id=cycle_id,
                     alerts=sub_results["dead_run"].get("alert_count", 0))
        except Exception as e:
            log.error("dead_run agent failed", cycle_id=cycle_id, error=str(e))
            sub_results["dead_run"] = {"status": "failed", "summary_text": str(e),
                                       "alert_count": 0, "severity": "normal"}

        # ── 4. Earnings Guardian ──────────────────────────────
        try:
            sub_results["earnings"] = await EarningsAgent(conn, redis).run(cycle_id)
            log.info("earnings agent done", cycle_id=cycle_id,
                     alerts=sub_results["earnings"].get("alert_count", 0))
        except Exception as e:
            log.error("earnings agent failed", cycle_id=cycle_id, error=str(e))
            sub_results["earnings"] = {"status": "failed", "summary_text": str(e),
                                       "alert_count": 0, "severity": "normal"}

        # ── 5. Supervisor (receives all sub-agent outputs) ────
        try:
            briefing = await SupervisorAgent(conn, redis).run(cycle_id, sub_results)
            log.info("supervisor done", cycle_id=cycle_id,
                     severity=briefing.get("severity_level"), alerts=briefing.get("alert_count"))
        except Exception as e:
            log.error("supervisor failed", cycle_id=cycle_id, error=str(e))
            briefing = {
                "cycle_id":      cycle_id,
                "severity_level": "normal",
                "alert_count":   0,
                "status":        "failed",
                "error":         str(e),
            }

    elapsed_ms           = int((time.monotonic() - cycle_start) * 1000)
    briefing["execution_ms"] = elapsed_ms

    # Broadcast completed briefing to all connected WebSocket clients
    await ws_manager.broadcast({
        "type":          "cycle_complete",
        "cycle_id":      cycle_id,
        "event_version": 1,
        "sent_at":       __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "elapsed_ms":    elapsed_ms,
    })

    log.info("cycle complete", cycle_id=cycle_id, elapsed_ms=elapsed_ms,
             severity=briefing.get("severity_level"), alerts=briefing.get("alert_count", 0))

    return briefing


_cycle_args: tuple = ()   # (pool, redis, ws_manager) stored for reschedule

MIN_REAL_INTERVAL_SECS = 30   # never fire faster than every 30 real seconds


def start_scheduler(pool: asyncpg.Pool, redis, ws_manager) -> AsyncIOScheduler:
    global _scheduler, _cycle_args
    _cycle_args = (pool, redis, ws_manager)
    _scheduler  = AsyncIOScheduler()
    _scheduler.add_job(
        run_cycle,
        trigger          = IntervalTrigger(minutes=CYCLE_INTERVAL_MINS),
        args             = list(_cycle_args),
        id               = "aria_cycle",
        replace_existing = True,
        max_instances    = 1,    # never allow overlapping cycles
    )
    _scheduler.start()
    log.info("scheduler started", interval_mins=CYCLE_INTERVAL_MINS)
    return _scheduler


def reschedule_cycle(time_scale: float) -> float:
    """
    Recompute the real-time interval so one sim-cycle (CYCLE_INTERVAL_MINS sim-minutes)
    fires at the right wall-clock cadence.

      real_secs = (CYCLE_INTERVAL_MINS * 60) / time_scale   (min: MIN_REAL_INTERVAL_SECS)

    Returns the effective real interval in seconds.
    """
    global _scheduler, _cycle_args
    if not _scheduler or not _scheduler.running:
        return 0.0

    real_secs = max(
        MIN_REAL_INTERVAL_SECS,
        (CYCLE_INTERVAL_MINS * 60.0) / time_scale,
    )
    _scheduler.reschedule_job(
        "aria_cycle",
        trigger = IntervalTrigger(seconds=real_secs),
    )
    log.info("cycle rescheduled", time_scale=time_scale, real_interval_secs=real_secs)
    return real_secs


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler stopped")
