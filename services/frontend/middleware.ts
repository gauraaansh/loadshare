/**
 * ARIA — Next.js Middleware
 * ==========================
 * Runs on every request before it hits any route.
 *
 * 1. Auth gate: verifies the session cookie.
 *    - Unauthenticated page requests → redirect to /login
 *    - Unauthenticated API requests  → 401 JSON
 *
 * 2. Security headers on all responses.
 *
 * Runs on the Edge runtime (no Node.js built-ins).
 * session.ts uses Web Crypto API which works in both Edge and Node.js.
 */

import { NextRequest, NextResponse } from "next/server";
import { verifySessionToken, COOKIE_NAME } from "@/lib/session";

// Paths that never require authentication
const PUBLIC = [
  "/login",
  "/api/auth/login",
  "/api/auth/logout",
  "/docs-chat",
  "/api/docs-chat",
];

// Static asset prefixes — skip entirely
const STATIC = ["/_next/", "/favicon.ico"];

export async function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;

  // Pass static assets through immediately
  if (STATIC.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  // Pass public auth routes through
  if (PUBLIC.some((p) => pathname.startsWith(p))) {
    return addSecurityHeaders(NextResponse.next());
  }

  // ── Auth check ───────────────────────────────────────────────
  const token = req.cookies.get(COOKIE_NAME)?.value;
  const valid  = token ? await verifySessionToken(token) : false;

  if (!valid) {
    if (pathname.startsWith("/api/")) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }

  return addSecurityHeaders(NextResponse.next());
}

function addSecurityHeaders(res: NextResponse): NextResponse {
  res.headers.set("X-Content-Type-Options", "nosniff");
  res.headers.set("X-Frame-Options", "DENY");
  res.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  return res;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
