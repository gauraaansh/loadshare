"""
ARIA — Supervisor Agent
================================
LangGraph-powered agent. Runs at the end of every 15-minute cycle,
after all 4 sub-agents have completed.

Responsibility:
  Cross-agent synthesis: consume results from Zone Intelligence, Restaurant
  Intelligence, Dead Run Prevention, and Earnings Guardian; detect multi-agent
  patterns that no single agent can see; produce a single cycle_briefing with
  severity classification, financial KPIs, and operator-facing recommended actions.
  Phase 2 adds episodic memory RAG — past cycle outcomes are retrieved and
  injected into the LLM prompt to ground recommendations in what actually worked.

Pipeline (6 nodes, strictly linear):
  ground_past_outcomes → validate_inputs → analyze_patterns
       → retrieve_context → call_llm → write_and_publish

Phase 1 nodes (1–4) — unchanged from Phase 1 design:

  validate_inputs:
    Classify each sub_result as ok/partial/failed/missing. Set
    observability_degraded if (failed+partial) >= 2. Replace bad results
    with safe defaults so downstream nodes never KeyError.

  analyze_patterns:
    Deterministic pattern detection with ratio+floor triggers. Named critical
    overrides (system_zone_pressure). Compound critical (churn_surge +
    dead_zone_pressure). Financial KPI block. Structured patterns_detected.

  call_llm:
    JSON prompt → vLLM. 4-step fallback JSON parser. Post-LLM normalization.
    Phase 2: conditionally prepends RAG snippet block if rag_context_used=True.

  write_and_publish:
    Idempotent cycle_briefings write (ON CONFLICT). Redis publish. Phase 2:
    also writes episode to supervisor_episode_memory for future retrieval.

Phase 2 additions:

  Node 0 — ground_past_outcomes:
    Grounds outcome_1cycle and outcome_3cycle for past ungrounded episodes.
    Uses cycle_briefings timestamp ORDER to find +1/+3 cycles reliably —
    avoids fragile "exactly 15 minutes ago" time arithmetic.
    Computes absolute + pct deltas, per-pattern resolution, effectiveness_score
    (patterns_resolved / actionable_patterns). Runs before validate_inputs so
    grounded outcomes are available for this cycle's retrieval.

  Node 4 — retrieve_context:
    1. Build deterministic embed_input string (severity + pattern types + KPIs).
       Never embed the LLM's situation_summary — canonical text is stable across
       prompt/temperature/model changes.
    2. Call Ollama nomic-embed-text for 768-dim embedding. Log latency.
       On Ollama failure: skip RAG path, set embedding_status='failed' for episode.
    3. SQL: recency(30d) + severity_adjacent + city overlap + embedding_status='ok'
       + outcome_1cycle IS NOT NULL → ORDER BY vector similarity, LIMIT 10.
    4. Python: filter by pattern_types overlap (>=1 shared with current cycle).
    5. Python: filter similarity >= RAG_SIMILARITY_THRESHOLD (0.65).
    6. Minimum support gate: if < RAG_MIN_SUPPORT (2) pass → skip RAG.
    7. Rank top RAG_TOP_K (3) by (similarity DESC, effectiveness_score DESC).
    8. Build compressed snippets, cap total at RAG_SNIPPET_MAX_CHARS (1200).

  KPI mapping (actual exported fields from each agent):
    zone:       dead_zone_count, stressed_zone_count, total_zones_classified,
                riders_in_dead_zones
    restaurant: operator_alerts (≈ high-risk restaurant count), scores_written
    dead_run:   flagged_zones, total_earnings_at_risk_rs, system_pressure
    earnings:   at_risk_count, churn_risk_count, avg_eph,
                total_earnings_shortfall_rs, recovery_count, snapshots_written

  system_zone_pressure inferred from dead_zone_count/total_zones >= 0.50.
  cities_active: from zone agent result (cities_active field) with DB fallback.
"""

import json
import re
import time
import uuid
from typing import Any, Optional, TypedDict

import structlog
from langgraph.graph import END, StateGraph

from agents.base import BaseAgent
from config import (
    CYCLE_INTERVAL_MINS,
    RAG_MIN_SUPPORT,
    RAG_RECENCY_DAYS,
    RAG_SIMILARITY_THRESHOLD,
    RAG_SNIPPET_MAX_CHARS,
    RAG_TOP_K,
)
from embedding_client import get_embedding, vec_to_pgvector_str
from llm import call_llm
from redis_client import CHANNEL_CYCLE_COMPLETE

log = structlog.get_logger()

# ── Pattern trigger thresholds (ratio + absolute floor) ──────────────────────
_CHURN_SURGE_PCT    = 0.25
_CHURN_SURGE_ABS    = 3
_DEAD_ZONE_PCT      = 0.30
_DEAD_ZONE_ABS      = 2
_RESTAURANT_PCT     = 0.40
_RESTAURANT_ABS     = 3
_SYS_ZONE_PRESSURE  = 0.50

# ── Observability gate ────────────────────────────────────────────────────────
_OBSERVABILITY_GATE = 2

# ── LLM output hard limits ────────────────────────────────────────────────────
_MAX_SUMMARY_CHARS   = 2000
_MAX_REASONING_CHARS = 500
_MAX_ACTION_CHARS    = 300
_MAX_ACTIONS         = 5

# ── Severity adjacency for RAG pre-filter ────────────────────────────────────
_SEVERITY_ADJACENT: dict[str, list[str]] = {
    "critical": ["critical", "warning"],
    "warning":  ["critical", "warning", "normal"],
    "normal":   ["normal",   "warning"],
}


# ══════════════════════════════════════════════════════════════════════════════
# LangGraph state
# ══════════════════════════════════════════════════════════════════════════════

class SupervisorState(TypedDict):
    # ── Phase 1 fields ───────────────────────────────────────────
    cycle_id:               str
    sub_results:            dict
    agent_execution_meta:   dict
    failed_agents:          list
    partial_agents:         list
    observability_degraded: bool
    patterns_detected:      list
    severity_level:         str
    financial_kpis:         dict
    alert_count:            int
    llm_output:             dict
    final_briefing:         dict
    execution_start:        float
    # ── Phase 2 additions ────────────────────────────────────────
    grounding_updates_done: int
    cities_active:          list
    embed_input:            str       # canonical string used for embedding
    embedding_vec:          list      # 768-dim float list (or empty on failure)
    embedding_status:       str       # 'ok' | 'failed'
    embedding_latency_ms:   int
    rag_snippets:           list      # compressed episode text blocks
    rag_context_used:       bool
    rag_candidates:         int       # episodes that passed all filters
    rag_used_count:         int       # snippets actually injected into prompt
    best_similarity:        float


# ── Required keys each sub-agent result must provide ─────────────────────────
_REQUIRED_KEYS = {"status", "alert_count", "severity", "summary_text"}

_SAFE_DEFAULT: dict[str, Any] = {
    "status":       "missing",
    "alert_count":  0,
    "severity":     "normal",
    "summary_text": "Agent did not run.",
}


# ══════════════════════════════════════════════════════════════════════════════
# Outcome helpers (Phase 2)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_jsonb(val: Any) -> Any:
    """
    Safely decode a value from an asyncpg JSONB column.
    asyncpg natively decodes JSONB to Python list/dict — pass through as-is.
    Only decode if returned as a raw string (edge case with some asyncpg configs).
    Returns {} on None or undecodable input.
    """
    if val is None:
        return {}
    if isinstance(val, (list, dict)):
        return val          # asyncpg already decoded — do NOT call dict() on a list
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return {}
    return val


def _compute_outcome(
    patterns:    list[dict],
    before_kpis: dict,
    after_kpis:  dict,
) -> dict:
    """
    Compute outcome deltas between before (episode cycle) and after (+N cycle) KPIs.
    Returns both absolute and percentage deltas, pattern resolution map,
    effectiveness_score (0–1), and boolean effective.
    """
    # Core metric deltas
    at_risk_before  = int(before_kpis.get("at_risk_or_critical_count", 0) or 0)
    at_risk_after   = int(after_kpis.get("at_risk_or_critical_count",  0) or 0)
    avg_eph_before  = float(before_kpis.get("avg_eph_rs_per_hr", 0) or 0)
    avg_eph_after   = float(after_kpis.get("avg_eph_rs_per_hr",  0) or 0)
    dead_before     = int(before_kpis.get("dead_zone_count",    0) or 0)
    dead_after      = int(after_kpis.get("dead_zone_count",     0) or 0)
    rest_before     = int(before_kpis.get("high_risk_restaurant_count", 0) or 0)
    rest_after      = int(after_kpis.get("high_risk_restaurant_count",  0) or 0)

    at_risk_delta_abs = at_risk_after - at_risk_before
    avg_eph_delta_abs = round(avg_eph_after - avg_eph_before, 2)

    at_risk_delta_pct = round(
        (at_risk_delta_abs / max(at_risk_before, 1)) * 100, 1
    )
    avg_eph_delta_pct = round(
        (avg_eph_delta_abs / max(avg_eph_before, 0.01)) * 100, 1
    )

    # Per-pattern resolution
    pattern_types = [p["type"] for p in patterns if isinstance(p, dict)]
    resolved:  list[str] = []
    persisted: list[str] = []

    _resolution_check = {
        "churn_surge":         at_risk_after  < at_risk_before,
        "dead_zone_pressure":  dead_after     < dead_before,
        "restaurant_cascade":  rest_after     < rest_before,
        "system_zone_pressure": dead_after    < dead_before,
    }
    for pt in pattern_types:
        if pt == "observability_degraded":
            continue   # not a resolvable operational pattern
        if _resolution_check.get(pt, False):
            resolved.append(pt)
        else:
            persisted.append(pt)

    actionable = [p for p in pattern_types if p != "observability_degraded"]
    effectiveness_score = round(
        len(resolved) / max(len(actionable), 1), 3
    ) if actionable else 0.0

    effective = effectiveness_score > 0 or avg_eph_delta_abs > 0

    return {
        "at_risk_delta_abs":   at_risk_delta_abs,
        "at_risk_delta_pct":   at_risk_delta_pct,
        "avg_eph_delta_abs":   avg_eph_delta_abs,
        "avg_eph_delta_pct":   avg_eph_delta_pct,
        "dead_zone_delta_abs": dead_after - dead_before,
        "rest_delta_abs":      rest_after - rest_before,
        "patterns_resolved":   resolved,
        "patterns_persisted":  persisted,
        "effectiveness_score": effectiveness_score,
        "effective":           effective,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Node 0 — ground_past_outcomes
# ══════════════════════════════════════════════════════════════════════════════

async def _ground_past_outcomes(state: SupervisorState, conn) -> SupervisorState:
    """
    Ground outcome_1cycle and outcome_3cycle for past ungrounded episodes.

    Uses cycle_briefings ORDER BY timestamp to find the next +1 and +3 cycles
    after each episode — avoids fragile time arithmetic assuming exactly
    CYCLE_INTERVAL_MINS seconds between cycles. Handles scheduler delays
    and manual /cycle/run triggers correctly.
    """
    updates_done = 0
    try:
        # Episodes missing at least one outcome, old enough to have a next cycle
        ungrounded = await conn.fetch(
            """
            SELECT id, cycle_id, patterns_detected
            FROM supervisor_episode_memory
            WHERE (outcome_1cycle IS NULL OR outcome_3cycle IS NULL)
              AND created_at < NOW() - ($1 * INTERVAL '1 minute')
            ORDER BY created_at ASC
            LIMIT 20
            """,
            CYCLE_INTERVAL_MINS,
        )

        for ep in ungrounded:
            ep_id       = ep["id"]
            ep_cycle_id = str(ep["cycle_id"])
            patterns    = _parse_jsonb(ep["patterns_detected"])
            if isinstance(patterns, dict):
                patterns = [patterns]

            # Fetch before-state KPIs from this episode's own cycle briefing
            before_row = await conn.fetchrow(
                """
                SELECT briefing_json FROM cycle_briefings
                WHERE cycle_id = $1::uuid
                """,
                ep_cycle_id,
            )
            if not before_row:
                continue

            before_kpis = _parse_jsonb(before_row["briefing_json"]).get("financial_kpis", {})

            # Next 3 cycle briefings after this episode (by timestamp, not time delta)
            next_cycles = await conn.fetch(
                """
                SELECT briefing_json FROM cycle_briefings
                WHERE timestamp > (
                    SELECT timestamp FROM cycle_briefings WHERE cycle_id = $1::uuid
                )
                ORDER BY timestamp ASC
                LIMIT 3
                """,
                ep_cycle_id,
            )

            if not next_cycles:
                continue

            # outcome_1cycle — +1 cycle
            if ep["outcome_1cycle"] is None and len(next_cycles) >= 1:
                after_kpis = _parse_jsonb(next_cycles[0]["briefing_json"]).get("financial_kpis", {})
                outcome    = _compute_outcome(patterns, before_kpis, after_kpis)
                await conn.execute(
                    "UPDATE supervisor_episode_memory SET outcome_1cycle = $1::jsonb WHERE id = $2",
                    json.dumps(outcome),
                    ep_id,
                )
                updates_done += 1

            # outcome_3cycle — +3 cycles
            if ep["outcome_3cycle"] is None and len(next_cycles) >= 3:
                after_kpis = _parse_jsonb(next_cycles[2]["briefing_json"]).get("financial_kpis", {})
                outcome    = _compute_outcome(patterns, before_kpis, after_kpis)
                await conn.execute(
                    "UPDATE supervisor_episode_memory SET outcome_3cycle = $1::jsonb WHERE id = $2",
                    json.dumps(outcome),
                    ep_id,
                )
                updates_done += 1

    except Exception as exc:
        log.warning("ground_past_outcomes_failed", error=str(exc))

    return {**state, "grounding_updates_done": updates_done}


# ══════════════════════════════════════════════════════════════════════════════
# Node 1 — validate_inputs  (Phase 1, unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def _validate_inputs(state: SupervisorState) -> SupervisorState:
    sub_results = state["sub_results"]
    agent_names = ["zone", "restaurant", "dead_run", "earnings"]

    meta: dict[str, dict] = {}
    failed:  list[str]    = []
    partial: list[str]    = []
    cleaned: dict         = {}

    for name in agent_names:
        raw = sub_results.get(name, {})

        if not raw or not isinstance(raw, dict):
            status_class = "missing"
        elif raw.get("status") == "failed":
            status_class = "failed"
        elif raw.get("status") == "partial":
            status_class = "partial"
        elif not _REQUIRED_KEYS.issubset(raw.keys()):
            status_class = "partial"
        else:
            status_class = "ok"

        if status_class in ("failed", "missing"):
            failed.append(name)
        elif status_class == "partial":
            partial.append(name)

        meta[name] = {
            "status":      status_class,
            "alert_count": raw.get("alert_count", 0) if raw else 0,
            "severity":    raw.get("severity",    "normal") if raw else "normal",
        }
        cleaned[name] = raw if raw else _SAFE_DEFAULT.copy()

    obs_degraded = (len(failed) + len(partial)) >= _OBSERVABILITY_GATE
    if obs_degraded:
        log.warning("supervisor_observability_degraded", failed=failed, partial=partial)

    return {
        **state,
        "sub_results":            cleaned,
        "agent_execution_meta":   meta,
        "failed_agents":          failed,
        "partial_agents":         partial,
        "observability_degraded": obs_degraded,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Node 2 — analyze_patterns  (Phase 1, unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def _analyze_patterns(state: SupervisorState) -> SupervisorState:
    sub  = state["sub_results"]
    meta = state["agent_execution_meta"]

    zone       = sub["zone"]
    restaurant = sub["restaurant"]
    dead_run   = sub["dead_run"]
    earnings   = sub["earnings"]

    # Zone KPIs
    dead_zone_count      = int(zone.get("dead_zone_count",        0))
    stressed_zone_count  = int(zone.get("stressed_zone_count",    0))
    total_zones          = int(zone.get("total_zones_classified",  0)) or 180
    riders_in_dead_zones = int(zone.get("riders_in_dead_zones",   0))
    dead_zone_pct        = dead_zone_count / total_zones
    system_zone_pressure = dead_zone_pct >= _SYS_ZONE_PRESSURE

    # cities_active: zone agent first, fallback resolved in retrieve_context
    cities_active: list[str] = zone.get("cities_active") or []

    # Restaurant KPIs
    # Use above_threshold_count (delay_risk_score >= RESTAURANT_RISK_THRESHOLD) so the
    # supervisor's "high-risk restaurant" number matches exactly what the panel displays.
    # Fall back to operator_alerts for backward compatibility with older agent results.
    high_risk_restaurant_count = int(
        restaurant.get("above_threshold_count", restaurant.get("operator_alerts", 0))
    )
    total_restaurants          = int(restaurant.get("scores_written",   1)) or 1
    restaurant_pct             = high_risk_restaurant_count / total_restaurants

    # Dead run KPIs
    flagged_zone_count        = int(dead_run.get("flagged_zones",           0))
    total_earnings_at_risk_rs = float(dead_run.get("total_earnings_at_risk_rs", 0.0))
    dead_run_sys_pressure     = bool(dead_run.get("system_pressure",        False))
    dead_zone_risk_pct        = flagged_zone_count / total_zones

    # Earnings KPIs
    total_active_riders = int(earnings.get("snapshots_written",         0)) or 1
    at_risk_count       = int(earnings.get("at_risk_count",             0))
    churn_risk_count    = int(earnings.get("churn_risk_count",          0))
    avg_eph             = float(earnings.get("avg_eph",                 0.0))
    total_shortfall_inr = float(earnings.get("total_earnings_shortfall_rs", 0.0))
    recovery_count      = int(earnings.get("recovery_count",            0))
    at_risk_pct         = at_risk_count / total_active_riders

    financial_kpis = {
        "total_active_riders":          total_active_riders,
        "avg_eph_rs_per_hr":            round(avg_eph, 2),
        "at_risk_or_critical_count":    at_risk_count,
        "churn_risk_count":             churn_risk_count,
        "recovery_count":               recovery_count,
        "total_earnings_shortfall_rs":  round(total_shortfall_inr, 2),
        "dead_run_earnings_at_risk_rs": round(total_earnings_at_risk_rs, 2),
        "dead_zone_count":              dead_zone_count,
        "stressed_zone_count":          stressed_zone_count,
        "riders_in_dead_zones":         riders_in_dead_zones,
        "high_risk_restaurant_count":   high_risk_restaurant_count,
        "flagged_dead_run_zones":       flagged_zone_count,
        "system_zone_pressure":         system_zone_pressure,
    }

    # Pattern detection
    patterns: list[dict] = []

    if at_risk_count >= _CHURN_SURGE_ABS and at_risk_pct >= _CHURN_SURGE_PCT:
        patterns.append({
            "type": "churn_surge", "is_critical_override": False,
            "trigger_values":  {"at_risk_count": at_risk_count, "at_risk_pct": round(at_risk_pct, 3)},
            "thresholds_hit":  {"abs_floor": _CHURN_SURGE_ABS, "pct_floor": _CHURN_SURGE_PCT},
        })

    if flagged_zone_count >= _DEAD_ZONE_ABS and dead_zone_risk_pct >= _DEAD_ZONE_PCT:
        patterns.append({
            "type": "dead_zone_pressure", "is_critical_override": False,
            "trigger_values":  {"flagged_zone_count": flagged_zone_count, "dead_zone_risk_pct": round(dead_zone_risk_pct, 3)},
            "thresholds_hit":  {"abs_floor": _DEAD_ZONE_ABS, "pct_floor": _DEAD_ZONE_PCT},
        })

    if high_risk_restaurant_count >= _RESTAURANT_ABS and restaurant_pct >= _RESTAURANT_PCT:
        patterns.append({
            "type": "restaurant_cascade", "is_critical_override": False,
            "trigger_values":  {"high_risk_restaurant_count": high_risk_restaurant_count, "restaurant_pct": round(restaurant_pct, 3)},
            "thresholds_hit":  {"abs_floor": _RESTAURANT_ABS, "pct_floor": _RESTAURANT_PCT},
        })

    if system_zone_pressure or dead_run_sys_pressure:
        patterns.append({
            "type": "system_zone_pressure", "is_critical_override": True,
            "trigger_values":  {"dead_zone_count": dead_zone_count, "dead_zone_pct": round(dead_zone_pct, 3), "dead_run_flag": dead_run_sys_pressure},
            "thresholds_hit":  {"zone_pct_floor": _SYS_ZONE_PRESSURE},
        })

    if state["observability_degraded"]:
        patterns.append({
            "type": "observability_degraded", "is_critical_override": False,
            "trigger_values":  {"failed_agents": state["failed_agents"], "partial_agents": state["partial_agents"]},
            "thresholds_hit":  {"degraded_agent_count": _OBSERVABILITY_GATE},
        })

    # Severity
    pattern_types     = {p["type"] for p in patterns}
    agent_severities  = [meta[a]["severity"] for a in meta]
    has_crit_override = any(p["is_critical_override"] for p in patterns)
    compound_critical = "churn_surge" in pattern_types and "dead_zone_pressure" in pattern_types

    if has_crit_override or compound_critical or "critical" in agent_severities:
        severity = "critical"
    elif len([p for p in patterns if not p["is_critical_override"]]) >= 2 \
            or "warning" in agent_severities:
        severity = "warning"
    else:
        severity = "normal"

    return {
        **state,
        "patterns_detected": patterns,
        "severity_level":    severity,
        "financial_kpis":    financial_kpis,
        "alert_count":       sum(meta[a]["alert_count"] for a in meta),
        "cities_active":     cities_active,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Node 3 — retrieve_context  (Phase 2)
# ══════════════════════════════════════════════════════════════════════════════

async def _retrieve_context(state: SupervisorState, conn) -> SupervisorState:
    """
    Build canonical embed_input → embed via Ollama → hybrid SQL query →
    Python filters (pattern overlap, similarity gate, min support) →
    compress top-k snippets.
    """
    patterns  = state["patterns_detected"]
    kpis      = state["financial_kpis"]
    severity  = state["severity_level"]
    cities    = state["cities_active"]

    # ── Resolve cities_active: zone agent first, DB fallback ─────────────────
    if not cities:
        try:
            rows = await conn.fetch(
                """
                SELECT DISTINCT z.city
                FROM zones z
                JOIN zone_density_snapshots zds ON zds.zone_id = z.id
                WHERE zds.timestamp > NOW() - INTERVAL '20 minutes'
                LIMIT 20
                """
            )
            cities = [r["city"] for r in rows]
        except Exception as exc:
            log.warning("cities_fallback_query_failed", error=str(exc))
            cities = []

    # ── Build deterministic embed_input (canonical, never the LLM summary) ───
    pattern_types_current = sorted(
        p["type"] for p in patterns if p["type"] != "observability_degraded"
    )
    embed_input = (
        f"severity={severity} "
        f"patterns={','.join(pattern_types_current) or 'none'} "
        f"at_risk={kpis.get('at_risk_or_critical_count', 0)} "
        f"dead_zones={kpis.get('dead_zone_count', 0)} "
        f"avg_eph={kpis.get('avg_eph_rs_per_hr', 0)} "
        f"shortfall={kpis.get('total_earnings_shortfall_rs', 0)} "
        f"restaurants={kpis.get('high_risk_restaurant_count', 0)}"
    )

    # ── Embed ─────────────────────────────────────────────────────────────────
    embedding, embed_latency = await get_embedding(embed_input)

    if embedding is None:
        # Ollama failed — skip RAG, continue without context
        log.warning("rag_skipped_embedding_failed")
        return {
            **state,
            "embed_input":         embed_input,
            "embedding_vec":       [],
            "embedding_status":    "failed",
            "embedding_latency_ms": embed_latency,
            "rag_snippets":        [],
            "rag_context_used":    False,
            "rag_candidates":      0,
            "rag_used_count":      0,
            "best_similarity":     0.0,
            "cities_active":       cities,
        }

    # ── Hybrid SQL query ──────────────────────────────────────────────────────
    vec_str           = vec_to_pgvector_str(embedding)
    severity_adjacent = _SEVERITY_ADJACENT.get(severity, [severity])

    try:
        rows = await conn.fetch(
            """
            SELECT
                id,
                situation_summary,
                pattern_types,
                actions_taken,
                severity,
                city,
                outcome_1cycle,
                created_at,
                1 - (embedding <=> $1::vector) AS similarity
            FROM supervisor_episode_memory
            WHERE embedding_status = 'ok'
              AND created_at > NOW() - ($2 * INTERVAL '1 day')
              AND severity = ANY($3::text[])
              AND ($4::text[] = '{}'::text[] OR city && $4::text[])
              AND outcome_1cycle IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT 10
            """,
            vec_str,
            RAG_RECENCY_DAYS,
            severity_adjacent,
            cities or [],
        )
    except Exception as exc:
        log.warning("rag_query_failed", error=str(exc))
        rows = []

    # ── Python filters ────────────────────────────────────────────────────────
    current_pattern_set = set(pattern_types_current)
    candidates = []

    for row in rows:
        sim = float(row["similarity"])

        # 1. Similarity gate
        if sim < RAG_SIMILARITY_THRESHOLD:
            continue

        # 2. Pattern overlap: at least 1 shared pattern type
        ep_patterns = set(row["pattern_types"] or [])
        if current_pattern_set and not current_pattern_set.intersection(ep_patterns):
            continue

        outcome = _parse_jsonb(row["outcome_1cycle"])
        effectiveness = float(outcome.get("effectiveness_score", 0.0))

        candidates.append({
            "situation_summary": row["situation_summary"],
            "pattern_types":     list(ep_patterns),
            "actions_taken":     list(row["actions_taken"] or []),
            "severity":          row["severity"],
            "outcome":           outcome,
            "effective":         bool(outcome.get("effective", False)),
            "effectiveness_score": effectiveness,
            "created_at":        row["created_at"],
            "similarity":        sim,
        })

    # 3. Minimum support gate
    if len(candidates) < RAG_MIN_SUPPORT:
        log.info(
            "rag_skipped_min_support",
            candidates=len(candidates),
            min_required=RAG_MIN_SUPPORT,
        )
        return {
            **state,
            "embed_input":         embed_input,
            "embedding_vec":       embedding,
            "embedding_status":    "ok",
            "embedding_latency_ms": embed_latency,
            "rag_snippets":        [],
            "rag_context_used":    False,
            "rag_candidates":      len(candidates),
            "rag_used_count":      0,
            "best_similarity":     candidates[0]["similarity"] if candidates else 0.0,
            "cities_active":       cities,
        }

    # 4. Rank: primary by similarity, secondary by effectiveness_score
    candidates.sort(key=lambda c: (c["similarity"], c["effectiveness_score"]), reverse=True)
    top = candidates[:RAG_TOP_K]

    # 5. Build compressed snippets
    snippets = [_compress_episode(ep) for ep in top]
    total_len = sum(len(s) for s in snippets)
    if total_len > RAG_SNIPPET_MAX_CHARS:
        # Truncate last snippet to fit budget
        budget_used = sum(len(s) for s in snippets[:-1])
        remaining   = max(0, RAG_SNIPPET_MAX_CHARS - budget_used)
        snippets[-1] = snippets[-1][:remaining].rstrip()

    log.info(
        "rag_context_retrieved",
        candidates=len(candidates),
        used=len(top),
        best_sim=round(top[0]["similarity"], 3) if top else 0,
    )

    return {
        **state,
        "embed_input":         embed_input,
        "embedding_vec":       embedding,
        "embedding_status":    "ok",
        "embedding_latency_ms": embed_latency,
        "rag_snippets":        snippets,
        "rag_context_used":    True,
        "rag_candidates":      len(candidates),
        "rag_used_count":      len(top),
        "best_similarity":     top[0]["similarity"],
        "cities_active":       cities,
    }


def _compress_episode(ep: dict) -> str:
    """
    4-5 line compressed snippet per episode for LLM prompt injection.
    Concise enough to inject 3 snippets without exceeding token budget.
    """
    ts      = ep["created_at"].strftime("%Y-%m-%d %H:%M") if ep.get("created_at") else "unknown"
    sim     = round(ep["similarity"], 2)
    outcome = ep["outcome"]
    actions = ep["actions_taken"][:3]

    eph_delta = outcome.get("avg_eph_delta_abs", 0)
    risk_delta = outcome.get("at_risk_delta_abs", 0)

    if outcome.get("effective"):
        outcome_line = (
            f"Outcome: at_risk {risk_delta:+d}, EPH {eph_delta:+.1f} Rs/hr"
            f", resolved={outcome.get('patterns_resolved', [])} — EFFECTIVE"
        )
    else:
        outcome_line = (
            f"Outcome: at_risk {risk_delta:+d}, EPH {eph_delta:+.1f} Rs/hr"
            f" — NOT EFFECTIVE"
        )

    actions_line = "; ".join(f'"{a}"' for a in actions) if actions else "none recorded"
    patterns_line = ", ".join(ep["pattern_types"]) or "none"

    return (
        f"[{ts} | severity={ep['severity']} | sim={sim}]\n"
        f"Patterns: {patterns_line}\n"
        f"Situation: {ep['situation_summary'][:200]}\n"
        f"Actions: {actions_line}\n"
        f"{outcome_line}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Node 4 — call_llm  (Phase 1 + Phase 2 RAG injection)
# ══════════════════════════════════════════════════════════════════════════════

async def _call_llm(state: SupervisorState) -> SupervisorState:
    sub      = state["sub_results"]
    patterns = state["patterns_detected"]
    kpis     = state["financial_kpis"]
    severity = state["severity_level"]
    meta     = state["agent_execution_meta"]
    obs_deg  = state["observability_degraded"]
    snippets = state.get("rag_snippets", [])

    if patterns:
        pattern_lines = "\n".join(
            "  - {type}{crit}: {trigger_values}".format(
                type=p["type"],
                crit=" [CRITICAL OVERRIDE]" if p.get("is_critical_override") else "",
                trigger_values=p["trigger_values"],
            )
            for p in patterns
        )
    else:
        pattern_lines = "  - none detected this cycle"

    agent_summary_lines = "\n".join(
        "  [{agent} | {status}] {summary}".format(
            agent=a.upper(),
            status=meta[a]["status"],
            summary=sub[a].get("summary_text", "No summary available."),
        )
        for a in ["zone", "restaurant", "dead_run", "earnings"]
    )

    obs_note = (
        "\nWARNING: Some agents returned degraded/partial data. "
        "Note data-quality uncertainty in your summary.\n"
        if obs_deg else ""
    )

    # ── Phase 2: inject RAG context block ────────────────────────────────────
    rag_block = ""
    if snippets:
        rag_block = (
            "\nRELEVANT PAST EXPERIENCE (retrieved from episodic memory):\n"
            + "\n\n".join(snippets)
            + "\n\nUse past experience to inform your recommended_actions where relevant.\n"
        )

    system_msg = (
        "You are ARIA's Supervisor Agent for Loadshare's last-mile logistics platform. "
        "You synthesise 15-minute operations cycles into concise operator briefings. "
        "You always respond with valid JSON only — no markdown, no preamble, no template text."
    )

    prompt = (
        f"{obs_note}"
        f"{rag_block}"
        f"CYCLE SEVERITY: {severity.upper()}\n\n"
        f"PATTERNS DETECTED:\n{pattern_lines}\n\n"
        f"FINANCIAL KPIs:\n"
        f"  Active riders: {kpis['total_active_riders']}\n"
        f"  Avg EPH: Rs.{kpis['avg_eph_rs_per_hr']}/hr\n"
        f"  At-risk / critical riders: {kpis['at_risk_or_critical_count']}\n"
        f"  Churn-risk riders: {kpis['churn_risk_count']}\n"
        f"  2-hr earnings shortfall: Rs.{kpis['total_earnings_shortfall_rs']}\n"
        f"  Dead-run earnings at risk: Rs.{kpis['dead_run_earnings_at_risk_rs']}\n"
        f"  Dead zones: {kpis['dead_zone_count']}  "
        f"Riders in dead zones: {kpis['riders_in_dead_zones']}\n"
        f"  High-risk restaurants: {kpis['high_risk_restaurant_count']}\n\n"
        f"AGENT SUMMARIES:\n{agent_summary_lines}\n\n"
        f"Write a JSON object with exactly these three fields filled with REAL content:\n"
        f"  situation_summary: 2-3 sentences describing what is happening operationally right now.\n"
        f"  recommended_actions: list of 3 specific actions ops should take this cycle.\n"
        f"  reasoning: 1 sentence explaining the severity classification.\n\n"
        f"Output JSON only. Example structure (do not copy this text, write real content):\n"
        f'{{"situation_summary": "...", "recommended_actions": ["...", "...", "..."], "reasoning": "..."}}'
    )

    raw        = await call_llm(prompt, max_tokens=450, temperature=0.2, system=system_msg)
    llm_output = _parse_llm_json(raw)

    llm_output["situation_summary"] = str(
        llm_output.get("situation_summary") or ""
    )[:_MAX_SUMMARY_CHARS]
    llm_output["reasoning"] = str(
        llm_output.get("reasoning") or ""
    )[:_MAX_REASONING_CHARS]
    actions = llm_output.get("recommended_actions", [])
    if not isinstance(actions, list):
        actions = []
    llm_output["recommended_actions"] = [
        str(a)[:_MAX_ACTION_CHARS] for a in actions[:_MAX_ACTIONS]
    ]

    return {**state, "llm_output": llm_output}


def _parse_llm_json(raw: str) -> dict:
    if not raw:
        return _llm_safe_default("LLM returned an empty response.")

    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$",          "", cleaned,     flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]+\}", cleaned)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    return _llm_safe_default(raw[:_MAX_SUMMARY_CHARS])


def _llm_safe_default(raw_text: str) -> dict:
    return {
        "situation_summary":   raw_text,
        "recommended_actions": [],
        "reasoning":           "LLM output could not be parsed as JSON.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Node 5 — write_and_publish  (Phase 1 + Phase 2 episode write)
# ══════════════════════════════════════════════════════════════════════════════

async def _write_and_publish(state: SupervisorState, conn, redis) -> SupervisorState:
    cycle_id         = state["cycle_id"]
    severity         = state["severity_level"]
    patterns         = state["patterns_detected"]
    kpis             = state["financial_kpis"]
    llm              = state["llm_output"]
    meta             = state["agent_execution_meta"]
    obs_deg          = state["observability_degraded"]
    alerts           = state["alert_count"]
    sub              = state["sub_results"]
    rag_context_used = state.get("rag_context_used",    False)
    rag_candidates   = state.get("rag_candidates",      0)
    rag_used_count   = state.get("rag_used_count",      0)
    best_similarity  = state.get("best_similarity",     0.0)
    grounding_done   = state.get("grounding_updates_done", 0)
    embed_latency    = state.get("embedding_latency_ms",   0)
    execution_ms     = int((time.monotonic() - state["execution_start"]) * 1000)

    briefing: dict[str, Any] = {
        "cycle_id":               cycle_id,
        "severity_level":         severity,
        "alert_count":            alerts,
        "patterns_detected":      patterns,
        "financial_kpis":         kpis,
        "agent_execution_meta":   meta,
        "observability_degraded": obs_deg,
        "situation_summary":      llm.get("situation_summary",   ""),
        "recommended_actions":    llm.get("recommended_actions", []),
        "reasoning":              llm.get("reasoning",           ""),
        # Phase 2 RAG observability fields
        "rag_context_used":       rag_context_used,
        "rag_candidates":         rag_candidates,
        "rag_used_count":         rag_used_count,
        "best_similarity":        round(best_similarity, 3),
        "grounding_updates_done": grounding_done,
        "embedding_latency_ms":   embed_latency,
        # Per-agent narrative
        "zone_summary":           sub["zone"].get("summary_text",       ""),
        "restaurant_summary":     sub["restaurant"].get("summary_text", ""),
        "dead_run_summary":       sub["dead_run"].get("summary_text",   ""),
        "earnings_summary":       sub["earnings"].get("summary_text",   ""),
    }

    # ── cycle_briefings write — idempotent ────────────────────────────────────
    try:
        await conn.execute(
            """
            INSERT INTO cycle_briefings
                (id, cycle_id, briefing_json, alert_count, severity_level, execution_ms)
            VALUES ($1, $2::uuid, $3, $4, $5, $6)
            ON CONFLICT (cycle_id) DO UPDATE
                SET briefing_json  = EXCLUDED.briefing_json,
                    alert_count    = EXCLUDED.alert_count,
                    severity_level = EXCLUDED.severity_level,
                    execution_ms   = EXCLUDED.execution_ms
            """,
            str(uuid.uuid4()),
            cycle_id,
            json.dumps(briefing, default=str),
            alerts,
            severity,
            execution_ms,
        )
        log.info(
            "cycle_briefing_written",
            cycle_id=cycle_id,
            severity=severity,
            alerts=alerts,
            rag=rag_context_used,
        )
    except Exception as exc:
        log.error("cycle_briefings_write_failed", cycle_id=cycle_id, error=str(exc))

    # ── Redis publish ─────────────────────────────────────────────────────────
    try:
        await redis.publish(
            CHANNEL_CYCLE_COMPLETE,
            json.dumps({
                "cycle_id":       cycle_id,
                "severity_level": severity,
                "alert_count":    alerts,
                "patterns":       [p["type"] for p in patterns],
            }, default=str),
        )
    except Exception as exc:
        log.warning("cycle_complete_publish_failed", cycle_id=cycle_id, error=str(exc))

    # ── supervisor_episode_memory write (Phase 2) ─────────────────────────────
    embedding_vec    = state.get("embedding_vec",    [])
    embedding_status = state.get("embedding_status", "failed")
    embed_input      = state.get("embed_input",      "")
    cities_active    = state.get("cities_active",    [])

    # Use zero vector for failed embeddings so the row is always written
    # (excluded from retrieval by embedding_status='failed' filter)
    if not embedding_vec or embedding_status == "failed":
        embedding_vec    = [0.0] * 768
        embedding_status = "failed"

    pattern_types_list = [p["type"] for p in patterns]
    actions_taken      = llm.get("recommended_actions", [])
    situation_summary  = llm.get("situation_summary", "")

    vec_str = vec_to_pgvector_str(embedding_vec)

    try:
        await conn.execute(
            """
            INSERT INTO supervisor_episode_memory
                (id, cycle_id, situation_summary, embed_input,
                 patterns_detected, pattern_types, actions_taken,
                 severity, city, embedding, embedding_status)
            VALUES ($1, $2::uuid, $3, $4, $5::jsonb, $6::text[], $7::text[],
                    $8, $9::text[], $10::vector, $11)
            ON CONFLICT (cycle_id) DO UPDATE
                SET situation_summary = EXCLUDED.situation_summary,
                    embed_input       = EXCLUDED.embed_input,
                    patterns_detected = EXCLUDED.patterns_detected,
                    pattern_types     = EXCLUDED.pattern_types,
                    actions_taken     = EXCLUDED.actions_taken,
                    severity          = EXCLUDED.severity,
                    city              = EXCLUDED.city,
                    embedding         = EXCLUDED.embedding,
                    embedding_status  = EXCLUDED.embedding_status
            """,
            str(uuid.uuid4()),
            cycle_id,
            situation_summary,
            embed_input,
            json.dumps(patterns, default=str),
            pattern_types_list,
            actions_taken,
            severity,
            cities_active,
            vec_str,
            embedding_status,
        )
    except Exception as exc:
        log.warning("episode_memory_write_failed", cycle_id=cycle_id, error=str(exc))

    return {**state, "final_briefing": briefing}


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly
# ══════════════════════════════════════════════════════════════════════════════

def _build_graph(conn, redis):
    """
    conn and redis injected via closure.
    Sync nodes: validate_inputs, analyze_patterns.
    Async nodes: ground_past_outcomes, retrieve_context, call_llm, write_and_publish.
    """
    g = StateGraph(SupervisorState)

    async def ground_past_outcomes(state):
        return await _ground_past_outcomes(state, conn)

    async def retrieve_context(state):
        return await _retrieve_context(state, conn)

    async def write_and_publish(state):
        return await _write_and_publish(state, conn, redis)

    g.add_node("ground_past_outcomes", ground_past_outcomes)
    g.add_node("validate_inputs",      _validate_inputs)
    g.add_node("analyze_patterns",     _analyze_patterns)
    g.add_node("retrieve_context",     retrieve_context)
    g.add_node("call_llm",             _call_llm)
    g.add_node("write_and_publish",    write_and_publish)

    g.set_entry_point("ground_past_outcomes")
    g.add_edge("ground_past_outcomes", "validate_inputs")
    g.add_edge("validate_inputs",      "analyze_patterns")
    g.add_edge("analyze_patterns",     "retrieve_context")
    g.add_edge("retrieve_context",     "call_llm")
    g.add_edge("call_llm",             "write_and_publish")
    g.add_edge("write_and_publish",    END)

    return g.compile()


# ══════════════════════════════════════════════════════════════════════════════
# Agent class
# ══════════════════════════════════════════════════════════════════════════════

class SupervisorAgent(BaseAgent):

    async def run(
        self,
        cycle_id:    str,
        sub_results: dict | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        t           = time.monotonic()
        sub_results = sub_results or {}

        try:
            graph = _build_graph(self.conn, self.redis)

            initial_state: SupervisorState = {
                "cycle_id":               cycle_id,
                "sub_results":            sub_results,
                "agent_execution_meta":   {},
                "failed_agents":          [],
                "partial_agents":         [],
                "observability_degraded": False,
                "patterns_detected":      [],
                "severity_level":         "normal",
                "financial_kpis":         {},
                "alert_count":            0,
                "llm_output":             {},
                "final_briefing":         {},
                "execution_start":        t,
                # Phase 2 defaults
                "grounding_updates_done": 0,
                "cities_active":          [],
                "embed_input":            "",
                "embedding_vec":          [],
                "embedding_status":       "failed",
                "embedding_latency_ms":   0,
                "rag_snippets":           [],
                "rag_context_used":       False,
                "rag_candidates":         0,
                "rag_used_count":         0,
                "best_similarity":        0.0,
            }

            final = await graph.ainvoke(initial_state)

            briefing = final["final_briefing"]
            severity = final["severity_level"]
            alerts   = final["alert_count"]
            patterns = [p["type"] for p in final["patterns_detected"]]

            status = "success"
            if final.get("observability_degraded"):
                status = "partial"
            if final.get("failed_agents"):
                status = "partial"

            summary = (
                f"Supervisor: severity={severity}, alerts={alerts}, "
                f"patterns={patterns or ['none']}, "
                f"rag={final.get('rag_context_used', False)}"
            )

        except Exception as exc:
            self.log.error("supervisor_agent_failed", error=str(exc), exc_info=True)
            briefing = {"status": "failed", "error": str(exc)}
            severity = "normal"
            alerts   = 0
            status   = "failed"
            summary  = f"SupervisorAgent failed: {exc}"

        execution_ms = int((time.monotonic() - t) * 1000)
        await self._log_to_db(cycle_id, briefing, summary, execution_ms, status)

        return {
            **briefing,
            "status":       status,
            "severity":     severity,
            "alert_count":  alerts,
            "summary_text": summary,
        }
