/**
 * GET /api/health
 * ================
 * Probes the MCP server health endpoint with a 3-second timeout.
 * Used by the client-side OfflinePage for auto-reconnect polling.
 * Returns { online: true } or { online: false }.
 */

import { NextResponse } from "next/server";

const MCP_INTERNAL = process.env.MCP_INTERNAL_URL ?? "http://aria-mcp-server:8001";
const MCP_API_KEY  = process.env.MCP_API_KEY       ?? "aria_mcp_key_change_me";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const res = await fetch(`${MCP_INTERNAL}/health`, {
      headers: { "X-API-Key": MCP_API_KEY },
      signal:  AbortSignal.timeout(3_000),
      next:    { revalidate: 0 },
    });
    if (res.ok) return NextResponse.json({ online: true });
    return NextResponse.json({ online: false }, { status: 503 });
  } catch {
    return NextResponse.json({ online: false }, { status: 503 });
  }
}
