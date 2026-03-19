/**
 * ARIA Frontend — Zod BFF Schemas
 * =================================
 * Validate and transform MCP tool payloads once, here.
 * UI components only consume these typed shapes — never raw API responses.
 * All field names match actual backend API response shapes.
 */

import { z } from "zod";

// ── Shared ────────────────────────────────────────────────────────────────────

export const SeveritySchema = z.enum(["critical", "warning", "normal"]);
export const AgentStatusSchema = z.enum(["ok", "partial", "failed", "missing"]);

// ── WS Event contract ─────────────────────────────────────────────────────────

export const WsEventSchema = z.object({
  type:          z.enum(["cycle_complete", "cycle_start", "agent_done", "ping"]),
  cycle_id:      z.string().optional(),
  event_version: z.number().default(1),
  sent_at:       z.string().optional(),
  payload:       z.record(z.string(), z.unknown()).optional(),
});
export type WsEvent = z.infer<typeof WsEventSchema>;

// ── KPI Summary (BFF-aggregated, /api/kpi-summary) ───────────────────────────

export const KpiSummarySchema = z.object({
  active_riders:         z.number(),
  dead_zones:            z.number(),
  at_risk_riders:        z.number(),
  avg_eph:               z.number(),
  high_risk_restaurants: z.number(),
  total_shortfall_inr:   z.number(),
  last_cycle_id:         z.string().optional(),
  last_cycle_at:         z.string().optional(),
  severity:              SeveritySchema,
});
export type KpiSummary = z.infer<typeof KpiSummarySchema>;

// ── Cycle Briefing (/tools/cycle-briefing — singular) ────────────────────────
// Actual API: { briefings: [{ cycle_id, briefing: {...}, severity_level, timestamp, ... }] }
// The `briefing` nested object is the full supervisor output (briefing_json from DB).

export const AgentResultSchema = z.object({
  status:       AgentStatusSchema.catch("missing"),
  summary_text: z.string().default(""),
  severity:     SeveritySchema.optional(),
  alert_count:  z.number().optional(),
}).passthrough();

// Full cycle briefing as consumed by UI (flat shape after BFF transform)
export const CycleBriefingSchema = z.object({
  cycle_id:        z.string(),
  timestamp:       z.string(),
  severity_level:  SeveritySchema.catch("normal"),
  alert_count:     z.number().optional(),
  execution_ms:    z.number().optional(),
  situation_summary:   z.string().default("No briefing available yet."),
  actions_taken:       z.array(z.string()).default([]),
  patterns_detected:   z.array(z.object({
    type:        z.string(),
    severity:    SeveritySchema.catch("normal"),
    description: z.string().default(""),
  })).default([]),
  agent_results:   z.object({
    zone:        AgentResultSchema.optional(),
    restaurant:  AgentResultSchema.optional(),
    dead_run:    AgentResultSchema.optional(),
    earnings:    AgentResultSchema.optional(),
  }).optional(),
  // Raw summary strings from each sub-agent (parsed for display)
  agent_summaries: z.object({
    zone:        z.string().optional(),
    restaurant:  z.string().optional(),
    dead_run:    z.string().optional(),
    earnings:    z.string().optional(),
  }).optional(),
  rag_context_used:  z.boolean().optional(),
  rag_used_count:    z.number().optional(),
  best_similarity:   z.number().optional(),
});
export type CycleBriefing = z.infer<typeof CycleBriefingSchema>;

// ── Zone Map (/api/zone-map) ──────────────────────────────────────────────────

export const ZoneFeatureSchema = z.object({
  zone_id:      z.string(),
  name:         z.string(),
  city:         z.string(),
  zone_type:    z.enum(["hub", "commercial", "residential", "peripheral"]).catch("residential"),
  stress_level: z.enum(["dead", "low", "normal", "stressed", "stale", "unknown"]).default("unknown"),
  stress_ratio: z.number().nullish(),
  rider_count:  z.number().nullish(),
  order_delta:  z.number().nullish(),
  geometry:     z.record(z.string(), z.unknown()),
});
export type ZoneFeature = z.infer<typeof ZoneFeatureSchema>;

export const ZoneMapSchema = z.object({
  zones:        z.array(ZoneFeatureSchema),
  total:        z.number(),
  last_updated: z.string(),
});
export type ZoneMap = z.infer<typeof ZoneMapSchema>;

// ── Rider Interventions (/tools/rider-interventions) ─────────────────────────
// Actual API: { count: N, interventions: [{intervention_id, rider_id, rider_name,
//   persona_type, recommendation_text, recommended_zone, priority, created_at, ...}] }

export const RiderInterventionSchema = z.object({
  intervention_id:       z.string(),
  rider_id:              z.string(),
  rider_name:            z.string().nullable().optional(),
  persona_type:          z.string().nullable().optional(),
  recommendation_text:   z.string(),
  recommended_zone_id:   z.string().nullable().optional(),
  recommended_zone:      z.string().nullable().optional(),
  recommended_zone_city: z.string().nullable().optional(),
  priority:              z.enum(["high", "medium", "low"]),
  was_acted_on:          z.boolean().nullable().optional(),
  cycle_id:              z.string(),
  created_at:            z.string(),
});
export type RiderIntervention = z.infer<typeof RiderInterventionSchema>;

export const RiderInterventionsSchema = z.object({
  count:         z.number(),
  interventions: z.array(RiderInterventionSchema),
});
export type RiderInterventions = z.infer<typeof RiderInterventionsSchema>;

// ── Restaurant Risks (/tools/restaurant-risks — plural) ──────────────────────
// Actual API: { threshold, count, restaurants: [{restaurant_id, name, city,
//   delay_risk_score, expected_delay_mins, confidence, explanation, ...}] }

export const RestaurantRiskSchema = z.object({
  restaurant_id:       z.string(),
  name:                z.string(),
  city:                z.string(),
  delay_risk_score:    z.number(),
  expected_delay_mins: z.number().nullable().optional(),
  confidence:          z.number().nullable().optional(),
  key_factors:         z.unknown().optional(),
  explanation:         z.string().nullable().optional(),
  cycle_id:            z.string(),
  timestamp:           z.string(),
});
export type RestaurantRisk = z.infer<typeof RestaurantRiskSchema>;

export const RestaurantRisksSchema = z.object({
  threshold:   z.number(),
  count:       z.number(),
  restaurants: z.array(RestaurantRiskSchema),
});
export type RestaurantRisks = z.infer<typeof RestaurantRisksSchema>;
