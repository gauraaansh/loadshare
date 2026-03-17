/**
 * /api/docs-chat — proxy to MCP server /docs-chat/chat
 * Injects X-API-Key server-side, forwards body, streams response back.
 */

import { NextRequest } from "next/server";

const MCP_INTERNAL = process.env.MCP_INTERNAL_URL ?? "http://mcp-server:8001";
const MCP_API_KEY  = process.env.MCP_API_KEY       ?? "aria_mcp_key_change_me";

export const runtime    = "nodejs";
export const maxDuration = 60;

export async function POST(req: NextRequest) {
  const body = await req.json();
  const message: string = body?.message ?? "";

  if (!message.trim()) {
    return Response.json({ error: "message required" }, { status: 400 });
  }

  try {
    const upstream = await fetch(`${MCP_INTERNAL}/docs-chat/chat`, {
      method:  "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key":    MCP_API_KEY,
      },
      body: JSON.stringify({ message }),
    });

    if (!upstream.ok) {
      const err = await upstream.text();
      return Response.json({ error: err }, { status: upstream.status });
    }

    // Forward the stream directly to the client
    return new Response(upstream.body, {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[docs-chat proxy]", msg);
    return Response.json({ error: "MCP server unreachable" }, { status: 502 });
  }
}
