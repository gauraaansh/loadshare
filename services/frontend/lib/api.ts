/**
 * ARIA Frontend — API client helpers
 * =====================================
 * Server-side: calls MCP tools via internal Docker URL + injected API key.
 * Client-side: calls Next.js proxy routes (/api/tools/[tool]).
 */

import { CycleBriefingSchema, type CycleBriefing } from "./schemas";

const MCP_INTERNAL = process.env.MCP_INTERNAL_URL  ?? "http://mcp-server:8001";
const MCP_API_KEY  = process.env.MCP_API_KEY        ?? "aria_mcp_key_change_me";

// ── Server-side helper (used in Next API routes only) ─────────────────────────

export async function fetchTool<T>(
  tool: string,
  params?: Record<string, string | number | boolean>,
): Promise<T> {
  const url = new URL(`${MCP_INTERNAL}/tools/${tool}`);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)));
  }
  const res = await fetch(url.toString(), {
    headers: { "X-API-Key": MCP_API_KEY },
    next:    { revalidate: 0 },
  });
  if (!res.ok) throw new Error(`MCP tool ${tool} returned ${res.status}`);
  return res.json();
}

// ── Client-side helper (hits our own /api proxy routes) ──────────────────────

export async function clientFetchTool<T>(
  tool: string,
  params?: Record<string, string | number>,
): Promise<T> {
  const base = typeof window !== "undefined" ? window.location.origin : "";
  const url  = new URL(`/aria/api/tools/${tool}`, base);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)));
  }
  const res = await fetch(url.toString());
  if (!res.ok) throw new Error(`Tool ${tool} proxy returned ${res.status}`);
  return res.json();
}

// ── Briefing normaliser ───────────────────────────────────────────────────────
// The backend returns: { briefings: [{ cycle_id, briefing: {...}, severity_level, timestamp }] }
// `briefing` key holds the raw briefing_json (supervisor output).
// This flattens it to the UI's CycleBriefing shape.

export function normaliseBriefing(raw: Record<string, unknown>): CycleBriefing {
  // briefing may be a stringified JSON (stored as text in DB) or already parsed.
  const briefingRaw = raw.briefing;
  const content: Record<string, unknown> =
    typeof briefingRaw === "string"
      ? JSON.parse(briefingRaw)
      : (briefingRaw as Record<string, unknown>) ?? {};
  // agent_execution_meta: { zone: {status, alert_count, severity}, ... }
  const meta = (content.agent_execution_meta ?? {}) as Record<string, Record<string, unknown>>;

  return CycleBriefingSchema.parse({
    cycle_id:       raw.cycle_id,
    timestamp:      raw.timestamp,
    severity_level: raw.severity_level ?? content.severity_level,
    alert_count:    raw.alert_count,
    execution_ms:   raw.execution_ms,
    situation_summary:  content.situation_summary,
    actions_taken:      content.recommended_actions ?? content.actions_taken,
    patterns_detected:  content.patterns_detected,
    rag_context_used:   content.rag_context_used,
    rag_used_count:     content.rag_used_count,
    best_similarity:    content.best_similarity,
    // Map agent_execution_meta + *_summary strings into agent_results
    agent_results: {
      zone:       { ...meta.zone,       summary_text: content.zone_summary       ?? "" },
      restaurant: { ...meta.restaurant, summary_text: content.restaurant_summary ?? "" },
      dead_run:   { ...meta.dead_run,   summary_text: content.dead_run_summary   ?? "" },
      earnings:   { ...meta.earnings,   summary_text: content.earnings_summary   ?? "" },
    },
    agent_summaries: {
      zone:        content.zone_summary        as string | undefined,
      restaurant:  content.restaurant_summary  as string | undefined,
      dead_run:    content.dead_run_summary    as string | undefined,
      earnings:    content.earnings_summary    as string | undefined,
    },
  });
}

// ── Query keys (TanStack Query) ───────────────────────────────────────────────

export const QK = {
  kpiSummary:         ["kpi-summary"]                   as const,
  cycleBriefing:      (id?: string) => ["cycle-briefing", id ?? "latest"] as const,
  cycleHistory:       (page: number) => ["cycle-history", page]            as const,
  zoneGeometry:       ["zone-geometry"]                 as const,
  zoneStress:         (id?: string) => ["zone-stress", id ?? "latest"]     as const,
  riderInterventions: (page: number) => ["rider-interventions", page]      as const,
  restaurantRisk:     (page: number) => ["restaurant-risk", page]          as const,
} as const;
