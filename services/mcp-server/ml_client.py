"""
ARIA — ML Server Client
========================
Async HTTP client for the internal FastAPI ML server (port 8002).

All endpoints require X-Internal-Key header — never exposed outside Docker network.

Functions:
  predict_duration(inputs)          → POST /internal/predict/duration
  predict_dead_zone(inputs)         → POST /internal/predict/dead-zone
  predict_earnings_trajectory(...)  → POST /internal/predict/earnings-trajectory
  predict_persona(inputs)           → POST /internal/predict/persona

Design:
  - One shared httpx.AsyncClient with connection pooling (limits=10/20).
  - 10s timeout per call — ML server should respond in <500ms.
  - On HTTP error: log and return None. Callers must handle None gracefully.
  - Agents use these results for context/display, not hard gates.
"""

import os
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

_ML_HOST         = os.getenv("ML_HOST",          "http://fastapi-ml:8002")
_ML_INTERNAL_KEY = os.getenv("ML_INTERNAL_KEY",  "aria-ml-internal-dev-key")

_HEADERS = {
    "X-Internal-Key": _ML_INTERNAL_KEY,
    "Content-Type":   "application/json",
}

# Shared client — reuse across all coroutines (thread-safe in asyncio).
_client = httpx.AsyncClient(
    base_url=_ML_HOST,
    headers=_HEADERS,
    timeout=10.0,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


async def _post(endpoint: str, payload: dict) -> dict | None:
    """Internal helper — POST to ML server, return parsed JSON or None."""
    try:
        resp = await _client.post(endpoint, json=payload)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        log.warning("ml_http_error", endpoint=endpoint, status=exc.response.status_code)
        return None
    except Exception as exc:
        log.warning("ml_client_error", endpoint=endpoint, error=str(exc))
        return None


async def predict_duration(inputs: dict[str, Any]) -> dict | None:
    """
    Predict total delivery duration in minutes.

    Inputs match DurationRequest schema (ml-server/schemas.py).
    Returns: { predicted_duration_mins, model_version, shap_top5 }
    """
    return await _post("/internal/predict/duration", inputs)


async def predict_dead_zone(inputs: dict[str, Any]) -> dict | None:
    """
    Predict dead zone risk for a pending order.

    Inputs match DeadZoneRequest schema.
    Returns: { dead_zone_risk_score, is_high_risk, risk_label,
               estimated_wait_mins, confidence, shap_top5 }
    """
    return await _post("/internal/predict/dead-zone", inputs)


async def predict_earnings_trajectory(inputs: dict[str, Any]) -> dict | None:
    """
    Predict earnings trajectory for a rider this session/day.

    Inputs match EarningsTrajectoryRequest schema.
    Returns: { predicted_eph, is_below_target, trajectory_label,
               shortfall_inr, confidence, shap_top5 }
    """
    return await _post("/internal/predict/earnings-trajectory", inputs)


async def predict_persona(inputs: dict[str, Any]) -> dict | None:
    """
    Classify a rider as supplementary (0) or dedicated (1).

    Inputs match PersonaRequest schema.
    Returns: { persona_label, persona_enc, confidence, shap_top5 }
    """
    return await _post("/internal/predict/persona", inputs)


async def close():
    """Call on application shutdown to drain connections."""
    await _client.aclose()
