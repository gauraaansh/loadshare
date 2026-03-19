"""
ARIA — Event Stream: Redis Client + Key Helpers
================================================
Single connection pool. All Redis key names defined here —
no magic strings scattered across files.

Pub/Sub channels:
  CHANNEL_ZONE_UPDATES    — published after every zone snapshot batch
  CHANNEL_SESSION_UPDATES — published on session open/close
  CHANNEL_ORDER_UPDATES   — published on order status transitions
"""

import redis.asyncio as aioredis
from config import REDIS_URL

# ── Connection pool (initialised once in lifespan) ────────────
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


# ── Key helpers ───────────────────────────────────────────────

def key_active_riders() -> str:
    """SET of rider_ids currently online."""
    return "aria:active_riders"


def key_zone_density(zone_id: str) -> str:
    """HASH — latest density snapshot for a zone. TTL 900s."""
    return f"aria:zone_density:{zone_id}"


def key_rider_state(rider_id: str) -> str:
    """HASH — live rider state: status, current_order_id, zone. TTL 3600s."""
    return f"aria:rider_state:{rider_id}"


def key_restaurant_queue(restaurant_id: str) -> str:
    """INT — active order count at a restaurant (queue model). TTL 3600s."""
    return f"aria:restaurant_queue:{restaurant_id}"


# ── Pub/Sub channels ─────────────────────────────────────────

CHANNEL_ZONE_UPDATES    = "aria:pubsub:zone_updates"
CHANNEL_SESSION_UPDATES = "aria:pubsub:session_updates"
CHANNEL_ORDER_UPDATES   = "aria:pubsub:order_updates"
