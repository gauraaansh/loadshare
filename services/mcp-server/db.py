"""
ARIA — MCP Server: Database Pool
===================================
asyncpg connection pool. Initialised once in lifespan, shared across
all request handlers and scheduler tasks via get_pool().
"""

import asyncpg
from config import DATABASE_URL

_pool: asyncpg.Pool | None = None


async def init_db() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=4, max_size=16)
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_db() first")
    return _pool


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
