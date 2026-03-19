"""
ARIA — ML Server: FastAPI Application
========================================
Internal prediction service. Wraps all 4 trained ML models.

Security model:
  - NO host port mapping in docker-compose — unreachable outside
    the Docker internal network (aria_network)
  - API key required on every request via X-Internal-Key header
  - Only the MCP server (fastapi-mcp) is a peer on this network
    and knows the internal key

Endpoints:
  POST /internal/predict/persona              → Model 1
  POST /internal/predict/duration             → Model 2
  POST /internal/predict/dead-zone            → Model 3
  POST /internal/predict/earnings-trajectory  → Model 4
  GET  /health                                → Model load status
  GET  /                                      → Basic info (no auth)
"""

import logging
import os
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse

from loader    import load_all_models, ModelRegistry
from schemas   import (
    PersonaRequest,   PersonaResponse,
    DurationRequest,  DurationResponse,
    DeadZoneRequest,  DeadZoneResponse,
    EarningsRequest,  EarningsResponse,
    HealthResponse,   ModelStatus,
)
from inference import (
    predict_persona,
    predict_duration,
    predict_dead_zone,
    predict_earnings,
)

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
INTERNAL_API_KEY = os.getenv("ML_INTERNAL_KEY", "aria-ml-internal-dev-key")
SERVICE_NAME     = "aria-ml-server"
_startup_time    = time.time()


# ══════════════════════════════════════════════════════════════
# LIFESPAN — model loading on startup
# ══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Load all 4 model artifacts once at startup.
    Stored in app.state.registry for the entire process lifetime.
    """
    log.info("aria-ml-server starting — loading model artifacts...")
    t0 = time.time()

    registry = load_all_models()
    app.state.registry = registry

    elapsed = time.time() - t0
    if registry.is_healthy():
        log.info(f"All 4 models loaded in {elapsed:.1f}s — server ready")
    else:
        failed = list(registry.load_errors.keys())
        log.warning(f"Server started with degraded models: {failed}")

    yield

    log.info("aria-ml-server shutting down")


# ══════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════

app = FastAPI(
    title       = "ARIA ML Server",
    description = (
        "Internal FastAPI service serving ARIA's 4 trained XGBoost models. "
        "Not exposed externally — accessible only within the Docker network "
        "by the MCP server. Requires X-Internal-Key header on all /internal/* routes."
    ),
    version     = "1.0.0",
    lifespan    = lifespan,
    docs_url    = "/docs",
    redoc_url   = None,
)


# ══════════════════════════════════════════════════════════════
# AUTH DEPENDENCY
# ══════════════════════════════════════════════════════════════

def require_internal_key(request: Request):
    key = request.headers.get("X-Internal-Key", "")
    if key != INTERNAL_API_KEY:
        log.warning("Unauthorized request — bad or missing X-Internal-Key",
                    path=str(request.url.path),
                    client=request.client.host if request.client else "unknown")
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Key")


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def _get_registry(request: Request) -> ModelRegistry:
    return request.app.state.registry


def _model_or_503(registry: ModelRegistry, model_name: str):
    artifact = getattr(registry, model_name, None)
    if artifact is None:
        err = registry.load_errors.get(model_name, "unknown load error")
        raise HTTPException(
            status_code=503,
            detail=f"Model {model_name} not available: {err}"
        )
    return artifact


# ══════════════════════════════════════════════════════════════
# ROUTES — unauthenticated
# ══════════════════════════════════════════════════════════════

@app.get("/", include_in_schema=False)
def root():
    return {
        "service":  SERVICE_NAME,
        "status":   "running",
        "note":     "Internal service — all prediction endpoints require X-Internal-Key",
        "endpoints": [
            "POST /internal/predict/persona",
            "POST /internal/predict/duration",
            "POST /internal/predict/dead-zone",
            "POST /internal/predict/earnings-trajectory",
            "GET  /health",
        ]
    }


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health(request: Request):
    """
    Returns load status for all 4 models.
    Called by Docker healthcheck and by the MCP server before
    forwarding requests.
    """
    registry = _get_registry(request)
    statuses = registry.model_status()

    model_status_objs = {
        name: ModelStatus(
            loaded       = info["loaded"],
            version      = info.get("version"),
            artifact_dir = info.get("artifact_dir"),
        )
        for name, info in statuses.items()
    }

    overall = "healthy" if registry.is_healthy() else "degraded"
    uptime  = round(time.time() - _startup_time, 1)

    return HealthResponse(
        status         = overall,
        models         = model_status_objs,
        uptime_seconds = uptime,
    )


# ══════════════════════════════════════════════════════════════
# ROUTES — internal prediction endpoints (all require key)
# ══════════════════════════════════════════════════════════════

@app.post(
    "/internal/predict/persona",
    response_model = PersonaResponse,
    tags           = ["predictions"],
    summary        = "Classify rider as supplementary or dedicated earner",
    dependencies   = [Depends(require_internal_key)],
)
def predict_persona_endpoint(req: PersonaRequest, request: Request):
    """
    Model 1 — Rider Persona Classifier.
    Called by Earnings Guardian and Dead Run Prevention agents.
    Supplementary riders (80%) are more risk-averse.
    Dedicated riders (20%) tolerate longer-distance orders.
    """
    t0        = time.time()
    registry  = _get_registry(request)
    artifacts = _model_or_503(registry, "model1_persona")

    try:
        result = predict_persona(artifacts, req)
        log.info("persona prediction",
                 persona=result["persona"],
                 confidence=result["confidence"],
                 latency_ms=round((time.time() - t0) * 1000, 1))
        return result
    except Exception as e:
        log.error("persona prediction failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


@app.post(
    "/internal/predict/duration",
    response_model = DurationResponse,
    tags           = ["predictions"],
    summary        = "Predict delivery duration in minutes",
    dependencies   = [Depends(require_internal_key)],
)
def predict_duration_endpoint(req: DurationRequest, request: Request):
    """
    Model 2 — Delivery Duration Scorer.
    Baseline for restaurant ripple detection.
    deviation = actual_time - predicted_minutes
    Consistent positive deviation per restaurant = ripple signal.
    RMSE ~4min, R2 ~0.82 on 41,953 real food delivery records.
    """
    t0        = time.time()
    registry  = _get_registry(request)
    artifacts = _model_or_503(registry, "model2_duration")

    try:
        result = predict_duration(artifacts, req)
        log.info("duration prediction",
                 predicted_mins=result["predicted_minutes"],
                 latency_ms=round((time.time() - t0) * 1000, 1))
        return result
    except Exception as e:
        log.error("duration prediction failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


@app.post(
    "/internal/predict/dead-zone",
    response_model = DeadZoneResponse,
    tags           = ["predictions"],
    summary        = "Predict dead zone stranding risk for an order assignment",
    dependencies   = [Depends(require_internal_key)],
)
def predict_dead_zone_endpoint(req: DeadZoneRequest, request: Request):
    """
    Model 3 — Dead Zone Risk Predictor.
    Called before each order assignment by Dead Run Prevention Agent.
    Two-stage: classifier probability + regressor stranding time if high risk.
    Probability is calibrated (isotonic) — true frequency estimate.
    Addresses: The Dead Zone Dilemma from Loadshare 2023 research.
    """
    t0        = time.time()
    registry  = _get_registry(request)
    artifacts = _model_or_503(registry, "model3_deadzone")

    try:
        result = predict_dead_zone(artifacts, req)
        log.info("dead zone prediction",
                 probability=result["dead_zone_probability"],
                 high_risk=result["is_high_risk"],
                 stranding_mins=result.get("expected_stranding_mins"),
                 latency_ms=round((time.time() - t0) * 1000, 1))
        return result
    except Exception as e:
        log.error("dead zone prediction failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


@app.post(
    "/internal/predict/earnings-trajectory",
    response_model = EarningsResponse,
    tags           = ["predictions"],
    summary        = "Forecast end-of-shift EPH and flag earnings risk",
    dependencies   = [Depends(require_internal_key)],
)
def predict_earnings_endpoint(req: EarningsRequest, request: Request):
    """
    Model 4 — Earnings Trajectory Forecaster.
    Called mid-session by Earnings Guardian Agent.
    projected_final_eph (regressor) + below_threshold flag (classifier).
    Alert levels: none → watch → intervene.
    Addresses: The Hourly Reality — platform EPH Rs.70-85 vs rider expectation Rs.90-100.
    """
    t0        = time.time()
    registry  = _get_registry(request)
    artifacts = _model_or_503(registry, "model4_earnings")

    try:
        result = predict_earnings(artifacts, req)
        log.info("earnings prediction",
                 projected_eph=result["projected_final_eph"],
                 alert=result["alert_level"],
                 trend=result["eph_trend"],
                 latency_ms=round((time.time() - t0) * 1000, 1))
        return result
    except Exception as e:
        log.error("earnings prediction failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


# ══════════════════════════════════════════════════════════════
# EXCEPTION HANDLERS
# ══════════════════════════════════════════════════════════════

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error("unhandled exception",
              path=str(request.url.path),
              error=str(exc),
              error_type=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True, log_level="info")
