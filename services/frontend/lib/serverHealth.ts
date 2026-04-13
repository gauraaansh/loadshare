/**
 * serverHealth.ts — Server-only utility
 * =======================================
 * Probes the MCP server with a short timeout.
 * Import only in server components / route handlers (never in "use client" files).
 */

const MCP_INTERNAL = process.env.MCP_INTERNAL_URL ?? "http://aria-mcp-server:8001";
const MCP_API_KEY  = process.env.MCP_API_KEY       ?? "aria_mcp_key_change_me";

export async function isMcpReachable(): Promise<boolean> {
  try {
    const res = await fetch(`${MCP_INTERNAL}/health`, {
      headers: { "X-API-Key": MCP_API_KEY },
      signal:  AbortSignal.timeout(3_000),
      next:    { revalidate: 0 },
    });
    return res.ok;
  } catch {
    return false;
  }
}
