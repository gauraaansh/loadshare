/**
 * /api/kpi-summary — BFF aggregation endpoint
 * =============================================
 * Assembles KPI strip data from the latest cycle briefing.
 * Single fetch from the client; server handles MCP orchestration.
 * Returns a flat KpiSummary shape validated against the Zod schema.
 */

import { NextResponse } from "next/server";
import { fetchTool } from "@/lib/api";
import { KpiSummarySchema } from "@/lib/schemas";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    // One tool call gets us everything — cycle_briefings returns the latest briefing
    // which aggregates all agent outputs.
    // Actual API: { briefings: [{ cycle_id, briefing: {...}, severity_level, timestamp }] }
    const raw = await fetchTool<{ briefings: Record<string, unknown>[] }>("cycle-briefing", { n: 1 });
    const row: Record<string, unknown> = raw.briefings?.[0] ?? {};
    // briefing may arrive as a stringified JSON (stored as text in DB) or already parsed.
    const briefingRaw = row.briefing;
    const content: Record<string, unknown> =
      typeof briefingRaw === "string"
        ? JSON.parse(briefingRaw)
        : (briefingRaw as Record<string, unknown>) ?? {};

    // briefing_json has NO agent_results key — KPI data lives entirely in financial_kpis.
    // Exact field names as written by supervisor analyze_patterns node:
    //   total_active_riders, avg_eph_rs_per_hr, at_risk_or_critical_count,
    //   total_earnings_shortfall_rs, dead_zone_count, high_risk_restaurant_count
    const f = (content.financial_kpis as Record<string, unknown>) ?? {};

    const summary = KpiSummarySchema.parse({
      active_riders:         Number(f.total_active_riders          ?? 0),
      dead_zones:            Number(f.dead_zone_count              ?? 0),
      at_risk_riders:        Number(f.at_risk_or_critical_count    ?? 0),
      avg_eph:               Number(f.avg_eph_rs_per_hr            ?? 0),
      high_risk_restaurants: Number(f.high_risk_restaurant_count   ?? 0),
      total_shortfall_inr:   Number(f.total_earnings_shortfall_rs  ?? 0),
      last_cycle_id:         String(row.cycle_id   ?? ""),
      last_cycle_at:         String(row.timestamp  ?? ""),
      severity:              String(row.severity_level ?? content.severity_level ?? "normal"),
    });

    return NextResponse.json(summary, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (err) {
    console.error("[kpi-summary] error:", err);
    // Return safe zeros — KPI strip shows stale rather than crashing
    return NextResponse.json(
      KpiSummarySchema.parse({
        active_riders: 0, dead_zones: 0, at_risk_riders: 0,
        avg_eph: 0, high_risk_restaurants: 0, total_shortfall_inr: 0,
        severity: "normal",
      }),
      { status: 200 },
    );
  }
}
