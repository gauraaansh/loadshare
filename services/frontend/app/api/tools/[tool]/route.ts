/**
 * /api/tools/[tool] — MCP proxy route
 * ======================================
 * Injects X-API-Key server-side so the key never reaches the browser.
 * Validates tool name against allowlist (exact backend route names).
 * Forwards query params as-is to the MCP server.
 */

import { NextRequest, NextResponse } from "next/server";

const MCP_INTERNAL = process.env.MCP_INTERNAL_URL ?? "http://mcp-server:8001";
const MCP_API_KEY  = process.env.MCP_API_KEY       ?? "aria_mcp_key_change_me";

// Exact backend route names (must match router.py @router.get paths)
const ALLOWED_TOOLS = new Set([
  "cycle-briefing",        // /tools/cycle-briefing
  "zone-intelligence",     // /tools/zone-intelligence
  "zone-recommendations",  // /tools/zone-recommendations
  "zone-map",              // /tools/zone-map  (added for frontend)
  "restaurant-risks",      // /tools/restaurant-risks
  "dead-run-risks",        // /tools/dead-run-risks
  "dead-zone-snapshots",   // /tools/dead-zone-snapshots
  "rider-health",          // /tools/rider-health
  "rider-alerts",          // /tools/rider-alerts
  "churn-signals",         // /tools/churn-signals
  "operator-alerts",       // /tools/operator-alerts
  "rider-interventions",   // /tools/rider-interventions
  "order-summary",         // /tools/order-summary
  "bootstrap-status",      // /tools/bootstrap-status
  "system-status",         // /tools/system-status
]);

export async function GET(
  req: NextRequest,
  { params }: { params: { tool: string } },
) {
  const { tool } = params;

  if (!ALLOWED_TOOLS.has(tool)) {
    return NextResponse.json({ error: `Unknown tool: ${tool}` }, { status: 404 });
  }

  const upstream = new URL(`${MCP_INTERNAL}/tools/${tool}`);
  req.nextUrl.searchParams.forEach((val, key) => {
    upstream.searchParams.set(key, val);
  });

  try {
    const res  = await fetch(upstream.toString(), {
      headers: { "X-API-Key": MCP_API_KEY },
      next:    { revalidate: 0 },
    });
    const body = await res.json();
    return NextResponse.json(body, {
      status:  res.status,
      headers: { "Cache-Control": "no-store" },
    });
  } catch (err) {
    console.error(`[proxy] tool=${tool}`, err);
    return NextResponse.json({ error: "MCP server unreachable" }, { status: 502 });
  }
}
