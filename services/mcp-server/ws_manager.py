"""
ARIA — MCP Server: WebSocket Manager
=======================================
Manages all connected frontend WebSocket clients.

Two sources push data to clients:
  1. Scheduler — broadcasts full cycle briefing after each 15-min cycle.
  2. Redis bridge — subscribes to event-stream pub/sub channels and
     forwards zone/session/order updates in real-time.

Usage:
    ws_manager = WSManager()          # one singleton, created at module level
    await ws_manager.connect(ws)      # in WebSocket endpoint
    ws_manager.disconnect(ws)         # on disconnect
    await ws_manager.broadcast(data)  # from scheduler / anywhere
    asyncio.create_task(ws_manager.start_redis_bridge(redis))   # in lifespan
"""

import json
import asyncio

from fastapi import WebSocket
import structlog

from redis_client import CHANNEL_ZONE_UPDATES, CHANNEL_SESSION_UPDATES, CHANNEL_ORDER_UPDATES

log = structlog.get_logger()


class WSManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        log.info("ws client connected", total=len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)
        log.info("ws client disconnected", total=len(self._connections))

    async def broadcast(self, payload: dict) -> None:
        """Send JSON to all connected clients. Silently remove dead connections."""
        if not self._connections:
            return
        msg  = json.dumps(payload, default=str)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.remove(ws)

    async def start_redis_bridge(self, redis) -> None:
        """
        Background task: subscribe to event-stream pub/sub channels and
        forward every message to all connected WebSocket clients.
        Runs until cancelled (lifespan shutdown).
        """
        pubsub = redis.pubsub()
        await pubsub.subscribe(
            CHANNEL_ZONE_UPDATES,
            CHANNEL_SESSION_UPDATES,
            CHANNEL_ORDER_UPDATES,
        )
        log.info("ws redis bridge started")
        try:
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                    await self.broadcast({"channel": msg["channel"], "data": data})
                except Exception as e:
                    log.debug("ws bridge parse error", error=str(e))
        except asyncio.CancelledError:
            await pubsub.unsubscribe()
            log.info("ws redis bridge stopped")


# Singleton used by main.py and scheduler.py
ws_manager = WSManager()
