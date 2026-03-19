/**
 * ARIA — Session helpers
 * =======================
 * HMAC-SHA256 signed cookie tokens.
 * Uses Web Crypto API — works in both Node.js (API routes) and Edge (middleware).
 */

export const COOKIE_NAME = "aria_session";
const SESSION_MS         = 7 * 24 * 60 * 60 * 1000; // 7 days
const enc                = new TextEncoder();

async function hmacHex(secret: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const buf = await crypto.subtle.sign("HMAC", key, enc.encode(data));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function createSessionToken(): Promise<string> {
  const secret = process.env.ARIA_SESSION_SECRET ?? "";
  const ts     = String(Date.now());
  const sig    = await hmacHex(secret, ts);
  return `${ts}.${sig}`;
}

export async function verifySessionToken(value: string): Promise<boolean> {
  const secret = process.env.ARIA_SESSION_SECRET ?? "";
  const dot    = value.lastIndexOf(".");
  if (dot === -1) return false;

  const ts     = value.slice(0, dot);
  const sig    = value.slice(dot + 1);
  const tsNum  = parseInt(ts, 10);

  if (isNaN(tsNum) || Date.now() - tsNum > SESSION_MS) return false;

  const expected = await hmacHex(secret, ts);
  return expected === sig;
}
