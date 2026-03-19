/**
 * /api/simulation — Simulation proxy (via MCP server)
 * =====================================================
 * All calls go through MCP server so event-stream is never
 * exposed to the public internet.
 *
 * GET  → MCP /simulation/status
 * POST → MCP /simulation/set-timescale?value=N
 */

import { NextRequest, NextResponse } from "next/server";

const MCP_INTERNAL = process.env.MCP_INTERNAL_URL ?? "http://aria-mcp-server:8001";
const MCP_API_KEY  = process.env.MCP_API_KEY       ?? "aria_mcp_key_change_me";

const headers = { "X-API-Key": MCP_API_KEY };

export async function GET() {
  try {
    const res  = await fetch(`${MCP_INTERNAL}/simulation/status`, {
      headers,
      next: { revalidate: 0 },
    });
    const body = await res.json();
    return NextResponse.json(body, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return NextResponse.json({ error: "MCP server unreachable" }, { status: 502 });
  }
}

export async function POST(req: NextRequest) {
  const value = req.nextUrl.searchParams.get("value");
  if (!value || isNaN(Number(value))) {
    return NextResponse.json({ error: "value query param required (number)" }, { status: 400 });
  }
  const n = Number(value);
  if (n < 1 || n > 300) {
    return NextResponse.json({ error: "value must be between 1 and 300" }, { status: 400 });
  }
  try {
    const res  = await fetch(`${MCP_INTERNAL}/simulation/set-timescale?value=${n}`, {
      method: "POST",
      headers,
      next: { revalidate: 0 },
    });
    const body = await res.json();
    return NextResponse.json(body, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return NextResponse.json({ error: "MCP server unreachable" }, { status: 502 });
  }
}
