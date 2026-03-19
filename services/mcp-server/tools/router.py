"""
ARIA — MCP Server: Public Tool Endpoints
==========================================
14 FastAPI routes that fastapi-mcp exposes as MCP tools.
Claude Desktop and the frontend call these by name.

Auth: every request must carry X-API-Key matching MCP_API_KEY.
DB:   each handler acquires a connection from the shared pool.

Tools:
  1.  get_cycle_briefing        — latest N supervisor cycle briefings
  2.  get_zone_intelligence     — zone stress snapshots for a cycle
  3.  get_zone_recommendations  — per-rider zone repositioning recommendations
  4.  get_restaurant_risks      — restaurant delay risk scores above threshold
  5.  get_dead_run_risks        — flagged pending-order risk scores
  6.  get_dead_zone_snapshots   — zone-level dead-zone risk aggregates (Dead Run agent)
  7.  get_rider_health          — rider health snapshots (at_risk / critical)
  8.  get_rider_alerts          — unresolved rider-facing alerts
  9.  get_churn_signals         — unescalated rider churn signals
  10. get_system_status         — DB, Redis, ML server, event-stream health
  11. get_operator_alerts       — unresolved ops/dispatcher-level alerts
  12. get_bootstrap_status      — seeder data readiness check
  13. get_rider_interventions   — cross-agent action items for churn-risk riders
  14. get_zone_map              — zone geometry + live stress levels for the map panel
"""

import httpx
import math
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security.api_key import APIKeyHeader

from db          import get_pool
from redis_client import get_redis, key_active_riders
from config      import (
    MCP_API_KEY,
    ML_HOST, ML_INTERNAL_KEY,
    EVENT_STREAM_HOST,
    RESTAURANT_RISK_THRESHOLD,
    DEAD_ZONE_RISK_THRESHOLD,
    HEALTH_SCORE_THRESHOLD,
)

log    = structlog.get_logger()
router = APIRouter(prefix="/tools", tags=["tools"])

# ── Auth ──────────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str = Depends(_api_key_header)):
    if key != MCP_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return key


# ══════════════════════════════════════════════════════════════
# 1. CYCLE BRIEFING
# ══════════════════════════════════════════════════════════════

@router.get("/cycle-briefing", dependencies=[Depends(require_api_key)])
async def get_cycle_briefing(
    n: int = Query(default=1, ge=1, le=10,
                   description="Number of recent cycle briefings to return"),
):
    """
    Return the N most recent supervisor cycle briefings.
    Each briefing contains zone, restaurant, dead-run, and earnings summaries
    plus the supervisor's cross-agent insights and recommended actions.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT cycle_id, briefing_json, alert_count, severity_level,
                   execution_ms, timestamp
            FROM cycle_briefings
            ORDER BY timestamp DESC
            LIMIT $1
            """,
            n,
        )
    if not rows:
        return {"briefings": [], "message": "No cycle briefings yet — cycle has not run"}
    return {
        "briefings": [
            {
                "cycle_id":      str(r["cycle_id"]),
                "briefing":      r["briefing_json"],
                "alert_count":   r["alert_count"],
                "severity_level": r["severity_level"],
                "execution_ms":  r["execution_ms"],
                "timestamp":     r["timestamp"].isoformat(),
            }
            for r in rows
        ]
    }


# ══════════════════════════════════════════════════════════════
# 2. ZONE INTELLIGENCE
# ══════════════════════════════════════════════════════════════

@router.get("/zone-intelligence", dependencies=[Depends(require_api_key)])
async def get_zone_intelligence(
    cycle_id: str | None = Query(default=None,
                                 description="Filter by cycle UUID. Omit for latest cycle."),
    dead_only: bool = Query(default=False,
                            description="Return only dead zones (stress_ratio < 0.5)"),
):
    """
    Return zone stress snapshots written by the Zone Intelligence Agent.
    Each row shows density_score, stress_ratio, and whether a zone is dead.
    Optionally filtered to dead zones only, or a specific cycle.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        if cycle_id:
            rows = await conn.fetch(
                """
                SELECT zss.zone_id, z.name, z.city,
                       zss.stress_ratio, zss.density_score,
                       zss.is_dead_zone, zss.cycle_id, zss.timestamp
                FROM zone_stress_snapshots zss
                JOIN zones z ON z.id = zss.zone_id
                WHERE zss.cycle_id = $1::uuid
                  AND ($2 = FALSE OR zss.is_dead_zone = TRUE)
                ORDER BY zss.stress_ratio ASC
                """,
                cycle_id, dead_only,
            )
        else:
            # Latest cycle
            rows = await conn.fetch(
                """
                WITH latest AS (
                    SELECT cycle_id FROM zone_stress_snapshots
                    ORDER BY timestamp DESC LIMIT 1
                )
                SELECT zss.zone_id, z.name, z.city,
                       zss.stress_ratio, zss.density_score,
                       zss.is_dead_zone, zss.cycle_id, zss.timestamp
                FROM zone_stress_snapshots zss
                JOIN zones z ON z.id = zss.zone_id
                JOIN latest ON zss.cycle_id = latest.cycle_id
                WHERE ($1 = FALSE OR zss.is_dead_zone = TRUE)
                ORDER BY zss.stress_ratio ASC
                """,
                dead_only,
            )
    return {
        "count": len(rows),
        "zones": [
            {
                "zone_id":      str(r["zone_id"]),
                "name":         r["name"],
                "city":         r["city"],
                "stress_ratio": round(float(r["stress_ratio"]), 3),
                "density_score": round(float(r["density_score"]), 3),
                "is_dead_zone": r["is_dead_zone"],
                "cycle_id":     str(r["cycle_id"]),
                "timestamp":    r["timestamp"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 3. ZONE RECOMMENDATIONS
# ══════════════════════════════════════════════════════════════

@router.get("/zone-recommendations", dependencies=[Depends(require_api_key)])
async def get_zone_recommendations(
    cycle_id: str | None = Query(
        default=None,
        description="Filter by cycle UUID. Omit for latest cycle.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Return per-rider zone repositioning recommendations from the Zone Intelligence Agent.

    Generated each cycle for riders whose home zone is dead or low-density and
    viable sister zones exist nearby. Each row contains:
      - recommended_zone_ids: ordered UUID array (best sister zones, up to 2)
      - rationale: template string with zone name, density, distance, expected orders/hr gain

    Urgency context is embedded in the rationale text but not stored as a separate column.
    To filter by urgency, read the rationale text which includes the stress level and delta.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        if cycle_id:
            rows = await conn.fetch(
                """
                SELECT zr.id, zr.rider_id, r.name AS rider_name,
                       zr.cycle_id, zr.recommended_zone_ids,
                       zr.rationale, zr.timestamp
                FROM zone_recommendations zr
                JOIN riders r ON r.id = zr.rider_id
                WHERE zr.cycle_id = $1::uuid
                ORDER BY zr.timestamp DESC
                LIMIT $2
                """,
                cycle_id, limit,
            )
        else:
            rows = await conn.fetch(
                """
                WITH latest AS (
                    SELECT cycle_id FROM zone_recommendations
                    ORDER BY timestamp DESC LIMIT 1
                )
                SELECT zr.id, zr.rider_id, r.name AS rider_name,
                       zr.cycle_id, zr.recommended_zone_ids,
                       zr.rationale, zr.timestamp
                FROM zone_recommendations zr
                JOIN riders r ON r.id = zr.rider_id
                JOIN latest ON zr.cycle_id = latest.cycle_id
                ORDER BY zr.timestamp DESC
                LIMIT $1
                """,
                limit,
            )
    return {
        "count": len(rows),
        "recommendations": [
            {
                "recommendation_id":   str(r["id"]),
                "rider_id":            str(r["rider_id"]),
                "rider_name":          r["rider_name"],
                "recommended_zone_ids": [str(z) for z in (r["recommended_zone_ids"] or [])],
                "rationale":           r["rationale"],
                "cycle_id":            str(r["cycle_id"]),
                "timestamp":           r["timestamp"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 4. RESTAURANT RISKS
# ══════════════════════════════════════════════════════════════

@router.get("/restaurant-risks", dependencies=[Depends(require_api_key)])
async def get_restaurant_risks(
    threshold: float = Query(default=RESTAURANT_RISK_THRESHOLD, ge=0.0, le=1.0,
                             description="Minimum delay_risk_score to return"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    Return restaurant delay risk scores above the given threshold,
    ordered by risk descending. Includes ML-predicted expected delay and
    the LLM-generated explanation from the Restaurant Intelligence Agent.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            -- Only return restaurants flagged in the LATEST cycle.
            -- Restaurants that recover simply won't appear in the latest cycle's records,
            -- so they drop off automatically — no stale high-risk entries linger.
            WITH latest_cycle AS (
                SELECT cycle_id
                FROM restaurant_risk_scores
                ORDER BY timestamp DESC
                LIMIT 1
            )
            SELECT
                rrs.restaurant_id, rrs.cycle_id, rrs.delay_risk_score,
                rrs.expected_delay_mins, rrs.confidence, rrs.key_factors_json,
                rrs.explanation, rrs.timestamp,
                r.name AS restaurant_name, z.city
            FROM restaurant_risk_scores rrs
            JOIN restaurants r ON r.id = rrs.restaurant_id
            JOIN zones       z ON z.id = r.zone_id
            WHERE rrs.cycle_id = (SELECT cycle_id FROM latest_cycle)
              AND rrs.delay_risk_score >= $1
            ORDER BY rrs.delay_risk_score DESC
            LIMIT $2
            """,
            threshold, limit,
        )
    return {
        "threshold": threshold,
        "count":     len(rows),
        "restaurants": [
            {
                "restaurant_id":     str(r["restaurant_id"]),
                "name":              r["restaurant_name"],
                "city":              r["city"],
                "delay_risk_score":  round(float(r["delay_risk_score"]), 3),
                "expected_delay_mins": float(r["expected_delay_mins"]) if r["expected_delay_mins"] else None,
                "confidence":        round(float(r["confidence"]), 3) if r["confidence"] else None,
                "key_factors":       r["key_factors_json"],
                "explanation":       r["explanation"],
                "cycle_id":          str(r["cycle_id"]),
                "timestamp":         r["timestamp"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 4. DEAD RUN RISKS
# ══════════════════════════════════════════════════════════════

@router.get("/dead-run-risks", dependencies=[Depends(require_api_key)])
async def get_dead_run_risks(
    flagged_only: bool = Query(default=True,
                               description="Return only orders flagged as high dead-zone risk"),
    threshold: float = Query(default=DEAD_ZONE_RISK_THRESHOLD, ge=0.0, le=1.0),
    limit: int = Query(default=30, ge=1, le=100),
):
    """
    Return pending order dead-zone risk scores from the Dead Run Prevention Agent.
    Flagged orders (is_flagged=TRUE) should not be dispatched to peripheral zones
    without intervention.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ors.order_id, ors.cycle_id, ors.dead_zone_risk,
                   ors.expected_cost_mins, ors.is_flagged, ors.rationale,
                   ors.timestamp,
                   o.status AS order_status,
                   z.name   AS delivery_zone_name,
                   z.city
            FROM order_risk_scores ors
            JOIN orders o ON o.id = ors.order_id
            JOIN zones  z ON z.id = o.delivery_zone_id
            WHERE ors.dead_zone_risk >= $1
              AND ($2 = FALSE OR ors.is_flagged = TRUE)
            ORDER BY ors.dead_zone_risk DESC, ors.timestamp DESC
            LIMIT $3
            """,
            threshold, flagged_only, limit,
        )
    return {
        "flagged_only": flagged_only,
        "threshold":    threshold,
        "count":        len(rows),
        "orders": [
            {
                "order_id":          str(r["order_id"]),
                "dead_zone_risk":    round(float(r["dead_zone_risk"]), 3),
                "expected_cost_mins": float(r["expected_cost_mins"]) if r["expected_cost_mins"] else None,
                "is_flagged":        r["is_flagged"],
                "rationale":         r["rationale"],
                "order_status":      r["order_status"],
                "delivery_zone":     r["delivery_zone_name"],
                "city":              r["city"],
                "cycle_id":          str(r["cycle_id"]),
                "timestamp":         r["timestamp"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 6. DEAD ZONE SNAPSHOTS
# ══════════════════════════════════════════════════════════════

@router.get("/dead-zone-snapshots", dependencies=[Depends(require_api_key)])
async def get_dead_zone_snapshots(
    cycle_id: str | None = Query(
        default=None,
        description="Filter by cycle UUID. Omit for latest cycle.",
    ),
    min_risk: float = Query(
        default=0.0, ge=0.0, le=1.0,
        description="Minimum risk_level to return. 0 = all zones.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Return zone-level dead-zone risk aggregates written by the Dead Run Prevention Agent.

    Distinct from zone_stress_snapshots (Zone Intelligence Agent) — these are
    scored from pending order risk, not zone density:
      risk_level            — avg dead_zone_probability across all orders dispatched
                              to this zone this cycle (evidence-gated: only written
                              when max_risk >= 0.75 OR flagged_count >= 2)
      expected_stranding_mins — worst-case stranding time (max across all orders)

    Use this to identify which delivery destination zones are high dead-zone risk
    right now, distinct from which zones have low supply density.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        if cycle_id:
            rows = await conn.fetch(
                """
                SELECT dzs.id, dzs.zone_id, z.name AS zone_name, z.city,
                       dzs.cycle_id, dzs.risk_level,
                       dzs.expected_stranding_mins, dzs.timestamp
                FROM dead_zone_snapshots dzs
                JOIN zones z ON z.id = dzs.zone_id
                WHERE dzs.cycle_id = $1::uuid
                  AND dzs.risk_level >= $2
                ORDER BY dzs.risk_level DESC
                LIMIT $3
                """,
                cycle_id, min_risk, limit,
            )
        else:
            rows = await conn.fetch(
                """
                WITH latest AS (
                    SELECT cycle_id FROM dead_zone_snapshots
                    ORDER BY timestamp DESC LIMIT 1
                )
                SELECT dzs.id, dzs.zone_id, z.name AS zone_name, z.city,
                       dzs.cycle_id, dzs.risk_level,
                       dzs.expected_stranding_mins, dzs.timestamp
                FROM dead_zone_snapshots dzs
                JOIN zones z ON z.id = dzs.zone_id
                JOIN latest ON dzs.cycle_id = latest.cycle_id
                WHERE dzs.risk_level >= $1
                ORDER BY dzs.risk_level DESC
                LIMIT $2
                """,
                min_risk, limit,
            )
    return {
        "count": len(rows),
        "dead_zones": [
            {
                "snapshot_id":             str(r["id"]),
                "zone_id":                 str(r["zone_id"]),
                "zone_name":               r["zone_name"],
                "city":                    r["city"],
                "risk_level":              round(float(r["risk_level"]), 3),
                "expected_stranding_mins": round(float(r["expected_stranding_mins"]), 1)
                                           if r["expected_stranding_mins"] else None,
                "cycle_id":                str(r["cycle_id"]),
                "timestamp":               r["timestamp"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 7. RIDER HEALTH
# ══════════════════════════════════════════════════════════════

@router.get("/rider-health", dependencies=[Depends(require_api_key)])
async def get_rider_health(
    status_filter: str = Query(
        default="at_risk",
        description="'all' | 'healthy' | 'watch' | 'at_risk' | 'critical' — maps to health_score bands",
    ),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Return rider health snapshots from the Earnings Guardian Agent.
    health_score bands: healthy>=75, watch>=50, at_risk>=40, critical<40.
    """
    band_filters = {
        "all":      (0.0,  100.0),
        "healthy":  (75.0, 100.0),
        "watch":    (50.0,  75.0),
        "at_risk":  (40.0,  50.0),
        "critical": (0.0,   40.0),
    }
    if status_filter not in band_filters:
        raise HTTPException(400, f"status_filter must be one of: {list(band_filters)}")
    low, high = band_filters[status_filter]

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH latest_per_rider AS (
                SELECT DISTINCT ON (rider_id)
                    rider_id, cycle_id, health_score, current_eph,
                    projected_eph, persona_threshold, below_threshold, timestamp
                FROM rider_health_snapshots
                WHERE health_score >= $1 AND health_score < $2
                ORDER BY rider_id, timestamp DESC
            )
            SELECT lpr.*, r.name AS rider_name, r.persona_type
            FROM latest_per_rider lpr
            JOIN riders r ON r.id = lpr.rider_id
            ORDER BY lpr.health_score ASC
            LIMIT $3
            """,
            low, high, limit,
        )
    return {
        "status_filter": status_filter,
        "count":         len(rows),
        "riders": [
            {
                "rider_id":         str(r["rider_id"]),
                "name":             r["rider_name"],
                "persona_type":     r["persona_type"],
                "health_score":     round(float(r["health_score"]), 1),
                "current_eph":      round(float(r["current_eph"]), 2) if r["current_eph"] else None,
                "projected_eph":    round(float(r["projected_eph"]), 2) if r["projected_eph"] else None,
                "persona_threshold": float(r["persona_threshold"]) if r["persona_threshold"] else None,
                "below_threshold":  r["below_threshold"],
                "cycle_id":         str(r["cycle_id"]),
                "timestamp":        r["timestamp"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 6. RIDER ALERTS
# ══════════════════════════════════════════════════════════════

@router.get("/rider-alerts", dependencies=[Depends(require_api_key)])
async def get_rider_alerts(
    severity: str | None = Query(
        default=None,
        description="Filter by severity: 'low' | 'medium' | 'high' | 'critical'",
    ),
    alert_type: str | None = Query(
        default=None,
        description=(
            "Filter by type: 'restaurant_delay' | 'dead_zone_risk' | "
            "'earnings_below_threshold' | 'churn_risk' | "
            "'earnings_recovery' | 'long_distance_warning'"
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Return unresolved rider alerts ordered by severity + recency.
    Agents write here whenever a rider needs attention.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ra.id, ra.rider_id, r.name AS rider_name,
                   ra.alert_type, ra.message, ra.severity,
                   ra.metadata_json, ra.created_at, ra.cycle_id
            FROM rider_alerts ra
            JOIN riders r ON r.id = ra.rider_id
            WHERE ra.is_resolved = FALSE
              AND ($1 IS NULL OR ra.severity   = $1)
              AND ($2 IS NULL OR ra.alert_type = $2)
            ORDER BY
                CASE ra.severity
                    WHEN 'critical' THEN 1
                    WHEN 'high'     THEN 2
                    WHEN 'medium'   THEN 3
                    ELSE                 4
                END,
                ra.created_at DESC
            LIMIT $3
            """,
            severity, alert_type, limit,
        )
    return {
        "count": len(rows),
        "alerts": [
            {
                "alert_id":    str(r["id"]),
                "rider_id":    str(r["rider_id"]),
                "rider_name":  r["rider_name"],
                "alert_type":  r["alert_type"],
                "message":     r["message"],
                "severity":    r["severity"],
                "metadata":    r["metadata_json"],
                "cycle_id":    str(r["cycle_id"]) if r["cycle_id"] else None,
                "created_at":  r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 7. CHURN SIGNALS
# ══════════════════════════════════════════════════════════════

@router.get("/churn-signals", dependencies=[Depends(require_api_key)])
async def get_churn_signals(
    unescalated_only: bool = Query(
        default=True,
        description="True = only signals not yet escalated to operator",
    ),
    limit: int = Query(default=30, ge=1, le=100),
):
    """
    Return rider churn signals from the Earnings Guardian Agent.
    A signal is raised when a rider has N consecutive sessions below their
    EPH threshold. High signal_strength = high churn probability.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rcs.id, rcs.rider_id, r.name AS rider_name,
                   r.persona_type,
                   rcs.signal_strength, rcs.consecutive_bad_sessions,
                   rcs.avg_eph_last_n, rcs.trigger_reason,
                   rcs.is_escalated, rcs.created_at, rcs.cycle_id
            FROM rider_churn_signals rcs
            JOIN riders r ON r.id = rcs.rider_id
            WHERE ($1 = FALSE OR rcs.is_escalated = FALSE)
            ORDER BY rcs.signal_strength DESC, rcs.created_at DESC
            LIMIT $2
            """,
            unescalated_only, limit,
        )
    return {
        "unescalated_only": unescalated_only,
        "count":            len(rows),
        "churn_signals": [
            {
                "signal_id":               str(r["id"]),
                "rider_id":                str(r["rider_id"]),
                "rider_name":              r["rider_name"],
                "persona_type":            r["persona_type"],
                "signal_strength":         round(float(r["signal_strength"]), 3),
                "consecutive_bad_sessions": r["consecutive_bad_sessions"],
                "avg_eph_last_n":          round(float(r["avg_eph_last_n"]), 2) if r["avg_eph_last_n"] else None,
                "trigger_reason":          r["trigger_reason"],
                "is_escalated":            r["is_escalated"],
                "cycle_id":                str(r["cycle_id"]),
                "created_at":              r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 9. OPERATOR ALERTS
# ══════════════════════════════════════════════════════════════

@router.get("/operator-alerts", dependencies=[Depends(require_api_key)])
async def get_operator_alerts(
    alert_type: str | None = Query(
        default=None,
        description=(
            "Filter by type. Restaurant agent: 'restaurant_high_risk'. "
            "Zone agent: 'zone_stress' | 'system_zone_pressure'. "
            "Dead Run agent: 'dead_zone_risk' | 'system_dead_zone_pressure'. "
            "Earnings agent: 'churn_surge'."
        ),
    ),
    severity: str | None = Query(
        default=None,
        description="Filter by severity: 'low' | 'medium' | 'high' | 'critical'",
    ),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Return unresolved operator/dispatcher-level alerts ordered by severity + recency.
    These are system-level alerts for the operations team — distinct from rider-facing
    alerts. Written by agents:
      - Restaurant agent  → restaurant_high_risk
      - Zone agent        → zone_stress, system_zone_pressure
      - Dead Run agent    → dead_zone_risk, system_dead_zone_pressure
      - Earnings agent    → churn_surge
    Shown in the Operations Center panel of the frontend dashboard.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, cycle_id, agent_name, alert_type, severity,
                   title, message, metadata_json, created_at
            FROM operator_alerts
            WHERE is_resolved = FALSE
              AND ($1::text IS NULL OR alert_type = $1::text)
              AND ($2::text IS NULL OR severity   = $2::text)
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 1
                    WHEN 'high'     THEN 2
                    WHEN 'medium'   THEN 3
                    ELSE                 4
                END,
                created_at DESC
            LIMIT $3
            """,
            alert_type, severity, limit,
        )
    return {
        "count": len(rows),
        "operator_alerts": [
            {
                "alert_id":    str(r["id"]),
                "agent_name":  r["agent_name"],
                "alert_type":  r["alert_type"],
                "severity":    r["severity"],
                "title":       r["title"],
                "message":     r["message"],
                "metadata":    r["metadata_json"],
                "cycle_id":    str(r["cycle_id"]) if r["cycle_id"] else None,
                "created_at":  r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 11. RIDER INTERVENTIONS
# ══════════════════════════════════════════════════════════════

@router.get("/rider-interventions", dependencies=[Depends(require_api_key)])
async def get_rider_interventions(
    rider_id: str | None = Query(
        default=None,
        description="Filter to a specific rider UUID. Omit for all.",
    ),
    priority: str | None = Query(
        default=None,
        description="Filter by priority: 'high' | 'medium' | 'low'",
    ),
    limit: int = Query(default=30, ge=1, le=100),
):
    """
    Return rider intervention recommendations generated by the Earnings Guardian Agent.

    These are cross-agent-aware, actionable items for churn-risk riders.
    Each recommendation combines:
      - Zone agent context (if rider's zone is dead/low, Zone agent's recommended move)
      - Dead Run agent context (if rider had recent flagged high-risk orders)
      - Earnings trajectory guidance (EPH shortfall, peak-window advice)
      - Multi-session pattern advice (if consecutive below-threshold sessions detected)

    recommended_zone_id is NULL for non-zone interventions (EPH or schedule advice).
    was_acted_on is NULL = no feedback yet, TRUE/FALSE = outcome tracked.

    Ordered by priority (high first) then recency.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            -- Only return interventions from the latest completed cycle.
            -- Riders who recovered or were no longer flagged simply won't appear
            -- in the latest cycle, so their old interventions don't linger.
            WITH latest_cycle AS (
                SELECT cycle_id
                FROM rider_interventions
                ORDER BY created_at DESC
                LIMIT 1
            )
            SELECT
                ri.id,
                ri.rider_id,
                r.name          AS rider_name,
                r.persona_type,
                ri.cycle_id,
                ri.recommendation_text,
                ri.recommended_zone_id,
                z.name          AS recommended_zone_name,
                z.city          AS recommended_zone_city,
                ri.priority,
                ri.was_acted_on,
                ri.created_at
            FROM rider_interventions ri
            JOIN riders r ON r.id = ri.rider_id
            LEFT JOIN zones z ON z.id = ri.recommended_zone_id
            WHERE ri.cycle_id = (SELECT cycle_id FROM latest_cycle)
              AND ($1::uuid IS NULL OR ri.rider_id = $1::uuid)
              AND ($2::text IS NULL OR ri.priority  = $2::text)
            ORDER BY
                CASE ri.priority
                    WHEN 'high'   THEN 1
                    WHEN 'medium' THEN 2
                    ELSE               3
                END,
                ri.created_at DESC
            LIMIT $3
            """,
            rider_id, priority, limit,
        )
    return {
        "count": len(rows),
        "interventions": [
            {
                "intervention_id":     str(r["id"]),
                "rider_id":            str(r["rider_id"]),
                "rider_name":          r["rider_name"],
                "persona_type":        r["persona_type"],
                "recommendation_text": r["recommendation_text"],
                "recommended_zone_id": str(r["recommended_zone_id"]) if r["recommended_zone_id"] else None,
                "recommended_zone":    r["recommended_zone_name"],
                "recommended_zone_city": r["recommended_zone_city"],
                "priority":            r["priority"],
                "was_acted_on":        r["was_acted_on"],
                "cycle_id":            str(r["cycle_id"]),
                "created_at":          r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════
# 10. BOOTSTRAP STATUS
# ══════════════════════════════════════════════════════════════

@router.get("/bootstrap-status", dependencies=[Depends(require_api_key)])
async def get_bootstrap_status():
    """
    Check whether the system has been seeded with reference data.
    Returns counts for riders, restaurants, and zones.
    If any count is below the minimum threshold, is_ready=False is returned
    with a clear message — preventing silent empty results during demos.
    Use this before running the first agent cycle.
    """
    MIN_RIDERS      = 10
    MIN_RESTAURANTS = 5
    MIN_ZONES       = 10

    pool = get_pool()
    async with pool.acquire() as conn:
        riders      = await conn.fetchval("SELECT COUNT(*) FROM riders WHERE is_active = TRUE")
        restaurants = await conn.fetchval("SELECT COUNT(*) FROM restaurants WHERE is_active = TRUE")
        zones       = await conn.fetchval("SELECT COUNT(*) FROM zones WHERE is_active = TRUE")
        sessions_today = await conn.fetchval(
            "SELECT COUNT(*) FROM rider_sessions WHERE shift_end IS NULL"
        )

    riders      = int(riders)
    restaurants = int(restaurants)
    zones       = int(zones)

    issues = []
    if riders < MIN_RIDERS:
        issues.append(f"riders: {riders} (need >= {MIN_RIDERS}) — run seed_from_v2.py")
    if restaurants < MIN_RESTAURANTS:
        issues.append(f"restaurants: {restaurants} (need >= {MIN_RESTAURANTS}) — run seed_from_v2.py")
    if zones < MIN_ZONES:
        issues.append(f"zones: {zones} (need >= {MIN_ZONES}) — run seed_from_v2.py")

    is_ready = len(issues) == 0

    return {
        "is_ready":         is_ready,
        "riders":           riders,
        "restaurants":      restaurants,
        "zones":            zones,
        "sessions_today":   int(sessions_today),
        "issues":           issues,
        "message": "System ready — agent cycle can run." if is_ready
                   else "System NOT seeded. Run seed_from_v2.py before starting agents.",
    }
# ══════════════════════════════════════════════════════════════
# 8. SYSTEM STATUS
# ══════════════════════════════════════════════════════════════

@router.get("/system-status", dependencies=[Depends(require_api_key)])
async def get_system_status():
    """
    Live health check across all ARIA services.
    Returns DB connection state, Redis active-rider count,
    ML server reachability, and event-stream simulation state.
    """
    status: dict = {}

    # ── DB ────────────────────────────────────────────────────
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT COUNT(*) FROM riders WHERE is_active=TRUE")
        status["db"] = {"ok": True, "active_riders_in_db": int(result)}
    except Exception as e:
        status["db"] = {"ok": False, "error": str(e)}

    # ── Redis ─────────────────────────────────────────────────
    try:
        redis       = get_redis()
        online      = await redis.scard(key_active_riders())
        status["redis"] = {"ok": True, "active_riders_online": int(online)}
    except Exception as e:
        status["redis"] = {"ok": False, "error": str(e)}

    # ── ML server ─────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{ML_HOST}/health",
                                 headers={"X-Internal-Key": ML_INTERNAL_KEY})
        status["ml_server"] = {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        status["ml_server"] = {"ok": False, "error": str(e)}

    # ── Event stream ──────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{EVENT_STREAM_HOST}/simulation/status")
        status["event_stream"] = {"ok": True, **r.json()}
    except Exception as e:
        status["event_stream"] = {"ok": False, "error": str(e)}

    overall = all(v.get("ok") for v in status.values())
    return {"healthy": overall, "services": status}


# ══════════════════════════════════════════════════════════════
# 14. ZONE MAP
# ══════════════════════════════════════════════════════════════

@router.get("/zone-map", dependencies=[Depends(require_api_key)])
async def get_zone_map(
    type: str = Query(
        default="both",
        description=(
            "'geometry' — static zone boundaries only (long-cached by frontend). "
            "'stress'   — current stress levels only (refreshed each cycle). "
            "'both'     — geometry + stress merged (default)."
        ),
    ),
):
    """
    Zone map data for the frontend Leaflet map panel.

    Two-layer strategy:
      geometry  — zone boundaries (boundary_geojson), city, zone_type. Rarely changes.
                  Frontend caches at 1-hour TTL.
      stress    — latest stress_level + stress_ratio + rider_count per zone from
                  zone_stress_snapshots. Refreshed after each agent cycle.
                  Frontend invalidates this on cycle_complete WS event.

    Returns GeoJSON-ready zone features with all fields needed to colour and
    annotate each polygon.
    """
    import json
    from datetime import datetime, timezone

    pool = get_pool()
    async with pool.acquire() as conn:

        # ── Geometry (zones table) ────────────────────────────────────────
        geo_rows = []
        if type in ("geometry", "both"):
            geo_rows = await conn.fetch(
                """
                SELECT
                    id::text        AS zone_id,
                    name,
                    city,
                    centroid_lat,
                    centroid_lng,
                    boundary_geojson
                FROM zones
                ORDER BY city, name
                """
            )

        # ── Live stress (latest snapshot per zone) ────────────────────────
        # zone_stress_snapshots columns: stress_ratio, density_score, is_dead_zone, timestamp
        # stress_level is derived: is_dead_zone → "dead"; stress_ratio > 1.2 → "stressed";
        # stress_ratio < 0.7 → "low"; else "normal"
        stress_map: dict = {}
        if type in ("stress", "both"):
            stress_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (zone_id)
                    zone_id::text,
                    stress_ratio,
                    density_score,
                    is_dead_zone,
                    timestamp
                FROM zone_stress_snapshots
                ORDER BY zone_id, timestamp DESC
                """
            )
            for r in stress_rows:
                ratio      = float(r["stress_ratio"]) if r["stress_ratio"] is not None else None
                is_dead    = bool(r["is_dead_zone"])
                if is_dead:
                    stress_level = "dead"
                elif ratio is not None and ratio > 1.2:
                    stress_level = "stressed"
                elif ratio is not None and ratio < 0.7:
                    stress_level = "low"
                else:
                    stress_level = "normal"
                stress_map[r["zone_id"]] = {
                    "stress_level": stress_level,
                    "stress_ratio": ratio,
                }

        # ── Stress-only shortcut (no geometry needed) ─────────────────────
        if type == "stress":
            zones = [
                {
                    "zone_id":      zone_id,
                    "name":         "",
                    "city":         "",
                    "zone_type":    "residential",
                    "geometry":     {},
                    **stress_data,
                }
                for zone_id, stress_data in stress_map.items()
            ]
            return {
                "zones":        zones,
                "total":        len(zones),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

        # ── Merge geometry + stress ───────────────────────────────────────
        zones = []
        for r in geo_rows:
            zone_id = r["zone_id"]
            geo_raw = r["boundary_geojson"]

            # Parse boundary_geojson — holds metadata {zone_type, city_tier}, NOT polygon coords
            if isinstance(geo_raw, str):
                try:
                    meta = json.loads(geo_raw)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            elif isinstance(geo_raw, dict):
                meta = geo_raw
            else:
                meta = {}

            # Extract zone_type from metadata properties
            zone_type = "residential"
            if isinstance(meta, dict):
                props = meta.get("properties") or meta  # handle flat or nested
                zone_type = props.get("zone_type", zone_type)

            # Build a GeoJSON circle-approximation polygon from centroid.
            # 12-sided polygon (dodecagon) centred on lat/lng — looks better than a square.
            # d = 0.009° ≈ 1 km radius. Zones are clustered ~1.5–3 km apart so this
            # keeps them visually distinct while still showing the city cluster.
            lat  = float(r["centroid_lat"])
            lng  = float(r["centroid_lng"])
            d    = 0.009   # ~1 km radius
            n    = 12      # number of sides
            coords = [
                [lng + d * math.cos(2 * math.pi * i / n),
                 lat + d * math.sin(2 * math.pi * i / n)]
                for i in range(n)
            ]
            coords.append(coords[0])   # close ring
            geometry = {"type": "Polygon", "coordinates": [coords]}

            stress = stress_map.get(zone_id, {})
            zones.append({
                "zone_id":      zone_id,
                "name":         r["name"],
                "city":         r["city"],
                "zone_type":    zone_type,
                "centroid":     {"lat": lat, "lng": lng},
                "geometry":     geometry,
                "stress_level": stress.get("stress_level", "unknown"),
                "stress_ratio": stress.get("stress_ratio"),
            })

        return {
            "zones":        zones,
            "total":        len(zones),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }


@router.get("/order-summary", dependencies=[Depends(require_api_key)])
async def get_order_summary():
    """
    Live order pipeline snapshot.
    Returns counts grouped by status, throughput for the last cycle window (15 real min),
    and fleet utilisation from the latest rider_sessions snapshot.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                -- In-flight orders (currently being worked on)
                COUNT(*) FILTER (WHERE status IN ('assigned', 'rider_inbound', 'picked_up'))
                    AS active_orders,
                -- Waiting for a rider to be assigned
                COUNT(*) FILTER (WHERE status = 'created')
                    AS pending_queue,
                -- Delivered in the last 15 real minutes (≈ one agent cycle window)
                COUNT(*) FILTER (WHERE status = 'delivered'
                                   AND delivered_at > NOW() - INTERVAL '15 minutes')
                    AS delivered_cycle,
                -- Failed in the last 15 real minutes
                COUNT(*) FILTER (WHERE status = 'failed'
                                   AND failed_at > NOW() - INTERVAL '15 minutes')
                    AS failed_cycle,
                -- All-time totals for context
                COUNT(*) FILTER (WHERE status = 'delivered') AS total_delivered,
                COUNT(*) FILTER (WHERE status = 'failed')    AS total_failed,
                COUNT(*)                                      AS total_orders,
                -- Avg delivery time for delivered orders in the last 15 min (real seconds → mins)
                ROUND(
                    AVG(
                        EXTRACT(EPOCH FROM (delivered_at - assigned_at)) / 60.0
                    ) FILTER (
                        WHERE status = 'delivered'
                          AND delivered_at > NOW() - INTERVAL '15 minutes'
                          AND assigned_at IS NOT NULL
                    )::numeric, 1
                ) AS avg_delivery_mins_cycle
            FROM orders
            """
        )
        r = rows[0]

        # Active rider session count (open sessions = riders currently working)
        open_sessions = await conn.fetchval(
            "SELECT COUNT(DISTINCT rider_id) FROM rider_sessions WHERE shift_end IS NULL"
        )

    total = int(r["total_orders"] or 0)
    delivered_cycle = int(r["delivered_cycle"] or 0)
    failed_cycle    = int(r["failed_cycle"]    or 0)
    cycle_total     = delivered_cycle + failed_cycle
    failure_rate    = round(failed_cycle / cycle_total * 100, 1) if cycle_total > 0 else 0.0

    return {
        "active_orders":       int(r["active_orders"]    or 0),
        "pending_queue":       int(r["pending_queue"]    or 0),
        "delivered_cycle":     delivered_cycle,
        "failed_cycle":        failed_cycle,
        "failure_rate_pct":    failure_rate,
        "total_delivered":     int(r["total_delivered"]  or 0),
        "total_failed":        int(r["total_failed"]     or 0),
        "total_orders":        total,
        "avg_delivery_mins":   float(r["avg_delivery_mins_cycle"] or 0),
        "active_riders":       int(open_sessions or 0),
    }
