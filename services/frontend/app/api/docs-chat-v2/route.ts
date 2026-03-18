/**
 * /api/docs-chat-v2 — SSE proxy to MCP server /docs-chat/chat-v2
 * Forwards text/event-stream response for split-panel UI.
 */

import { NextRequest } from "next/server";

const MCP_INTERNAL = process.env.MCP_INTERNAL_URL ?? "http://mcp-server:8001";
const MCP_API_KEY  = process.env.MCP_API_KEY       ?? "aria_mcp_key_change_me";

export const runtime     = "nodejs";
export const maxDuration = 120;

export async function POST(req: NextRequest) {
  const body    = await req.json();
  const message: string = body?.message ?? "";
  const mode:    string = body?.mode    ?? "vector";

  if (!message.trim()) {
    return Response.json({ error: "message required" }, { status: 400 });
  }

  try {
    const upstream = await fetch(`${MCP_INTERNAL}/docs-chat/chat-v2`, {
      method:  "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key":    MCP_API_KEY,
      },
      body: JSON.stringify({ message, mode }),
    });

    if (!upstream.ok) {
      const err = await upstream.text();
      return Response.json({ error: err }, { status: upstream.status });
    }

    return new Response(upstream.body, {
      headers: {
        "Content-Type":  "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection":    "keep-alive",
      },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[docs-chat-v2 proxy]", msg);
    return Response.json({ error: "MCP server unreachable" }, { status: 502 });
  }
}
