/**
 * POST /api/auth/login
 * =====================
 * Validates the dashboard password and sets a signed session cookie.
 * Password is read server-side only — never reaches the browser.
 */

import { NextRequest, NextResponse } from "next/server";
import { createSessionToken, COOKIE_NAME } from "@/lib/session";

export async function POST(req: NextRequest) {
  let body: { password?: unknown } = {};
  try { body = await req.json(); } catch { /* malformed body → wrong password */ }

  const expected = process.env.ARIA_DASHBOARD_PASSWORD;
  if (!expected || typeof body.password !== "string" || body.password !== expected) {
    // Uniform response — don't reveal whether the env var is missing
    return NextResponse.json({ error: "Invalid password" }, { status: 401 });
  }

  const token = await createSessionToken();
  const res   = NextResponse.json({ ok: true });

  res.cookies.set(COOKIE_NAME, token, {
    httpOnly: true,
    secure:   process.env.NODE_ENV === "production",
    sameSite: "strict",
    path:     "/",
    maxAge:   60 * 60 * 24 * 7, // 7 days
  });

  return res;
}
