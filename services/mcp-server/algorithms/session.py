"""
ARIA — Algorithmic Module: Rider Session
=========================================
Pure Python. No ML. No side effects.

Functions:
  compute_current_eph          — live EPH for an active rider session (DB read)
  compute_session_health_score — 0-100 health score (pure computation, no DB)
  compute_dead_run_cost        — earnings cost of a dead run (pure math, no DB)
  compute_churn_signal         — multi-session churn detection (DB read)

Design:
  compute_current_eph and compute_churn_signal read from the DB.
  compute_session_health_score and compute_dead_run_cost are pure functions —
  the Earnings Guardian Agent calls them after it already has EPH + ML projection.

EPH targets from Loadshare 2023 research:
  Platform EPH was Rs.70-85/hr vs rider expectation Rs.90-100/hr.
  Rs.90 is the intervention threshold (supplementary earner target).
  Rs.100 is the dedicated earner target.
"""

import os
from datetime import datetime, timezone

import structlog

log = structlog.get_logger()

# ── Constants ─────────────────────────────────────────────────
EPH_TARGET_SUPPLEMENTARY = float(os.getenv("EPH_TARGET_SUPPLEMENTARY", "90.0"))
EPH_TARGET_DEDICATED     = float(os.getenv("EPH_TARGET_DEDICATED",     "100.0"))
# Assumed average EPH for dead run cost calculation
# (matches inference.py ASSUMED_EPH_RS_PER_HR)
ASSUMED_EPH_RS_PER_HR    = float(os.getenv("ASSUMED_EPH_RS_PER_HR",    "82.0"))
# Consecutive below-threshold sessions before churn escalation
CHURN_SIGNAL_SESSIONS    = int(os.getenv("CHURN_SIGNAL_SESSIONS",      "3"))
# Health score threshold: below this = rider is at risk
HEALTH_SCORE_THRESHOLD   = float(os.getenv("HEALTH_SCORE_THRESHOLD",   "40.0"))


# ══════════════════════════════════════════════════════════════
# FUNCTION 1 — compute_current_eph
# ══════════════════════════════════════════════════════════════

async def compute_current_eph(rider_id: str, conn) -> dict:
    """
    Compute the current earnings per hour for a rider's active session.

    Queries today's open session (shift_end IS NULL) from rider_sessions.
    EPH = total_earnings / hours_since_shift_start.

    Also returns raw session fields so the Earnings Guardian Agent
    can pass them directly to the ML server (Model 4).

    Returns:
        has_active_session, current_eph, earnings_so_far, hours_elapsed,
        orders_completed, idle_time_mins, dead_runs_count, long_distance_count,
        session_id, shift_start_iso
    """
    session = await conn.fetchrow(
        """
        SELECT id, shift_start, total_earnings, total_orders,
               idle_time_mins, dead_runs_count, long_distance_count
        FROM rider_sessions
        WHERE rider_id = $1
          AND session_date = CURRENT_DATE
          AND shift_end IS NULL
        """,
        rider_id,
    )

    if session is None:
        return {
            "rider_id":            rider_id,
            "has_active_session":  False,
            "current_eph":         0.0,
            "earnings_so_far":     0.0,
            "hours_elapsed":       0.0,
            "orders_completed":    0,
            "idle_time_mins":      0.0,
            "dead_runs_count":     0,
            "long_distance_count": 0,
            "session_id":          None,
            "shift_start_iso":     None,
        }

    now          = datetime.now(timezone.utc)
    shift_start  = session["shift_start"]
    # Minimum 1 minute to avoid division by near-zero at session open
    hours_elapsed = max((now - shift_start).total_seconds() / 3600, 1 / 60)
    earnings      = float(session["total_earnings"])
    current_eph   = earnings / hours_elapsed if hours_elapsed > 0 else 0.0

    return {
        "rider_id":            rider_id,
        "has_active_session":  True,
        "session_id":          str(session["id"]),
        "current_eph":         round(current_eph, 2),
        "earnings_so_far":     round(earnings, 2),
        "hours_elapsed":       round(hours_elapsed, 3),
        "orders_completed":    int(session["total_orders"]),
        "idle_time_mins":      round(float(session["idle_time_mins"]), 1),
        "dead_runs_count":     int(session["dead_runs_count"]),
        "long_distance_count": int(session["long_distance_count"]),
        "shift_start_iso":     shift_start.isoformat(),
    }


# ══════════════════════════════════════════════════════════════
# FUNCTION 2 — compute_session_health_score
# ══════════════════════════════════════════════════════════════

def compute_session_health_score(
    current_eph:      float,
    projected_eph:    float,
    dead_runs_count:  int,
    idle_time_mins:   float,
    hours_elapsed:    float,
    persona_type:     str = "supplementary",
) -> dict:
    """
    Compute session health score 0–100. Pure function — no DB.

    Called by Earnings Guardian Agent AFTER getting the ML server
    projection (Model 4 projected_final_eph). Both current and projected
    EPH are combined to make this forward-looking, not just a snapshot.

    Scoring breakdown:
      EPH component      (0–60 pts): weighted 70% projected + 30% current
      Efficiency component (0–25 pts): penalised by dead runs and idle time
      Trend component    (0–15 pts): bonus for being above EPH ratio

    Returns:
        health_score, eph_score, efficiency_score, trend_score,
        classification, eph_target, projected_eph, current_eph
    """
    eph_target = (
        EPH_TARGET_DEDICATED
        if persona_type == "dedicated"
        else EPH_TARGET_SUPPLEMENTARY
    )

    # ── EPH component (0–60 pts) ───────────────────────────────
    projected_ratio = min(projected_eph / eph_target, 1.5) if eph_target > 0 else 0.0
    current_ratio   = min(current_eph   / eph_target, 1.5) if eph_target > 0 else 0.0
    # Projected weighted more heavily — we care where they're headed
    blended_ratio   = 0.7 * projected_ratio + 0.3 * current_ratio
    eph_score       = min(60.0, max(0.0, blended_ratio * 60.0))

    # ── Efficiency component (0–25 pts) ───────────────────────
    # Dead runs: 5 pts penalty each, capped at 15 pts
    dead_run_penalty = min(15.0, dead_runs_count * 5.0)
    # Idle time: fraction of total session time spent idle
    total_session_mins = max(hours_elapsed * 60, 1.0)
    idle_fraction      = idle_time_mins / total_session_mins
    # 20% idle → 4 pts; 50% idle → 10 pts (max)
    idle_penalty       = min(10.0, idle_fraction * 20.0)
    efficiency_score   = max(0.0, 25.0 - dead_run_penalty - idle_penalty)

    # ── Trend component (0–15 pts) ────────────────────────────
    # Simple forward signal: are they above the EPH target ratio?
    # Agents add trend context from lag features; this scores the snapshot.
    trend_score = min(15.0, max(0.0, blended_ratio * 15.0))

    health_score = round(eph_score + efficiency_score + trend_score, 1)

    if health_score >= 75:
        classification = "healthy"
    elif health_score >= 50:
        classification = "watch"
    elif health_score >= HEALTH_SCORE_THRESHOLD:
        classification = "at_risk"
    else:
        classification = "critical"

    return {
        "health_score":      health_score,
        "eph_score":         round(eph_score, 1),
        "efficiency_score":  round(efficiency_score, 1),
        "trend_score":       round(trend_score, 1),
        "classification":    classification,
        "eph_target":        eph_target,
        "projected_eph":     projected_eph,
        "current_eph":       current_eph,
    }


# ══════════════════════════════════════════════════════════════
# FUNCTION 3 — compute_dead_run_cost
# ══════════════════════════════════════════════════════════════

def compute_dead_run_cost(
    stranding_mins: float,
    assumed_eph:    float = ASSUMED_EPH_RS_PER_HR,
) -> dict:
    """
    Estimate the earnings cost of a dead run stranding. Pure math — no DB.

    Called by Dead Run Prevention Agent before each order assignment
    (after Model 3 confirms high dead zone risk).

    The Rs.82/hr default matches ASSUMED_EPH_RS_PER_HR in inference.py.
    Loadshare article context: platform EPH was Rs.70-85 — Rs.82 is the midpoint.

    Returns:
        stranding_mins, earnings_lost_rs, opportunity_cost_rs,
        assumed_eph, health_score_impact (negative = penalty)
    """
    stranding_mins  = max(0.0, stranding_mins)
    earnings_lost   = (stranding_mins / 60.0) * assumed_eph

    # Health score impact:
    # Base penalty of 5 pts per dead run event,
    # scaled by severity (earnings lost relative to 1-hour baseline)
    base_penalty     = 5.0
    severity_ratio   = earnings_lost / assumed_eph  # fraction of 1hr earnings lost
    scaled_penalty   = base_penalty + min(10.0, severity_ratio * 20.0)

    return {
        "stranding_mins":     round(stranding_mins, 1),
        "earnings_lost_rs":   round(earnings_lost, 2),
        "assumed_eph":        assumed_eph,
        "health_score_impact": round(-scaled_penalty, 1),  # negative = penalty
    }


# ══════════════════════════════════════════════════════════════
# FUNCTION 4 — compute_churn_signal
# ══════════════════════════════════════════════════════════════

async def compute_churn_signal(
    rider_id:          str,
    conn,
    lookback_sessions: int = CHURN_SIGNAL_SESSIONS,
) -> dict:
    """
    Detect multi-session churn patterns for a rider.

    Queries the last N completed sessions. Computes:
      - consecutive_bad_sessions: streak of below_threshold = TRUE from most recent
      - avg_eph_last_n: average EPH over the lookback window
      - signal_strength: weighted composite 0–1

    Signal strength formula:
      40% — consecutive bad session fraction (max at CHURN_SIGNAL_SESSIONS)
      40% — EPH deficit fraction vs supplementary target
      20% — trend: recent EPH declining vs older sessions

    is_churn_risk = signal_strength >= 0.5 OR consecutive_bad >= threshold

    From Loadshare 2023 research: retention dropped to 30% at crisis peak.
    Early detection of 3+ consecutive below-threshold sessions is the key signal.

    Returns:
        rider_id, signal_strength, consecutive_bad_sessions,
        avg_eph_last_n, sessions_sampled, is_churn_risk, trigger_details
    """
    rows = await conn.fetch(
        """
        SELECT session_date, eph, below_threshold, health_score,
               total_orders, idle_time_mins, dead_runs_count
        FROM rider_sessions
        WHERE rider_id = $1
          AND shift_end IS NOT NULL
        ORDER BY session_date DESC
        LIMIT $2
        """,
        rider_id,
        lookback_sessions,
    )

    if not rows:
        return {
            "rider_id":                  rider_id,
            "signal_strength":           0.0,
            "consecutive_bad_sessions":  0,
            "avg_eph_last_n":            None,
            "sessions_sampled":          0,
            "is_churn_risk":             False,
            "trigger_details":           [],
        }

    sessions = list(rows)
    n        = len(sessions)

    # Consecutive below-threshold from most recent
    consecutive_bad = 0
    for s in sessions:
        if s["below_threshold"]:
            consecutive_bad += 1
        else:
            break

    # Average EPH
    ephs    = [float(s["eph"]) for s in sessions if s["eph"] is not None]
    avg_eph = sum(ephs) / len(ephs) if ephs else None

    # Trend: are EPH values declining from older → newer sessions?
    trend_penalty = 0.0
    if len(ephs) >= 3:
        recent_avg = sum(ephs[:2]) / 2          # 2 most recent sessions
        older_avg  = sum(ephs[2:4]) / len(ephs[2:4])  # sessions before that
        if older_avg > 0:
            decline_rate = max(0.0, (older_avg - recent_avg) / older_avg)
            trend_penalty = min(1.0, decline_rate)

    # Signal strength — weighted composite
    consec_score = min(1.0, consecutive_bad / max(CHURN_SIGNAL_SESSIONS, 1))
    eph_deficit  = (
        max(0.0, EPH_TARGET_SUPPLEMENTARY - avg_eph) / EPH_TARGET_SUPPLEMENTARY
        if avg_eph is not None
        else 0.5
    )
    signal_strength = (
        0.4 * consec_score
        + 0.4 * eph_deficit
        + 0.2 * trend_penalty
    )
    signal_strength = round(min(1.0, max(0.0, signal_strength)), 4)

    is_churn_risk = (
        signal_strength >= 0.5
        or consecutive_bad >= CHURN_SIGNAL_SESSIONS
    )

    trigger_details = []
    if consecutive_bad >= 2:
        trigger_details.append(
            f"{consecutive_bad} consecutive sessions below EPH threshold"
        )
    if avg_eph is not None and avg_eph < EPH_TARGET_SUPPLEMENTARY:
        trigger_details.append(
            f"avg EPH Rs.{avg_eph:.0f}/hr vs Rs.{EPH_TARGET_SUPPLEMENTARY:.0f} target"
        )
    if trend_penalty > 0.2:
        trigger_details.append("EPH declining across recent sessions")

    return {
        "rider_id":                  rider_id,
        "signal_strength":           signal_strength,
        "consecutive_bad_sessions":  consecutive_bad,
        "avg_eph_last_n":            round(avg_eph, 2) if avg_eph is not None else None,
        "sessions_sampled":          n,
        "is_churn_risk":             is_churn_risk,
        "trigger_details":           trigger_details,
    }
