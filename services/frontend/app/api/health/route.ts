/**
 * GET /api/health
 * ================
 * Probes the MCP server health endpoint with a 3-second timeout.
 * Used by the client-side OfflinePage for auto-reconnect polling.
 * Returns { online: true } or { online: false }.
 */

import { NextResponse }    from "next/server";
import { isMcpReachable }  from "@/lib/serverHealth";

export const dynamic = "force-dynamic";

export async function GET() {
  const online = await isMcpReachable();
  if (online) return NextResponse.json({ online: true });
  return NextResponse.json({ online: false }, { status: 503 });
}
