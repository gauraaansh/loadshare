"""
ARIA — MCP Server: Redis Client + Key Helpers
===============================================
Reads from the same Redis instance as the event-stream service.
Key names must stay in sync with event-stream/redis_client.py.
"""

import redis.asyncio as aioredis
from config import REDIS_URL

_redis: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    global _redis
    _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised — call init_redis() first")
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


# ── Key helpers (mirror event-stream — same Redis instance) ───

def key_active_riders() -> str:
    """SET of rider_ids currently online."""
    return "aria:active_riders"


def key_zone_density(zone_id: str) -> str:
    """HASH — latest density snapshot for a zone. TTL 900s."""
    return f"aria:zone_density:{zone_id}"


def key_rider_state(rider_id: str) -> str:
    """HASH — live rider state: status, current_order_id, zone. TTL 3600s."""
    return f"aria:rider_state:{rider_id}"


# ── Pub/Sub channels ──────────────────────────────────────────
# Published by event-stream; MCP ws_manager bridges these to WebSocket clients.
CHANNEL_ZONE_UPDATES    = "aria:pubsub:zone_updates"
CHANNEL_SESSION_UPDATES = "aria:pubsub:session_updates"
CHANNEL_ORDER_UPDATES   = "aria:pubsub:order_updates"
# Published by MCP scheduler after each cycle completes.
CHANNEL_CYCLE_COMPLETE  = "aria:pubsub:cycle_complete"
