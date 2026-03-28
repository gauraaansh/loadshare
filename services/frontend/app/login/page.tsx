"use client";

/**
 * ARIA — Login Page
 * ==================
 * Single shared-password gate for the dashboard.
 * Matches the dark ops theme of the main dashboard.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [pw, setPw]           = useState("");
  const [error, setError]     = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const res = await fetch("/aria/api/auth/login", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ password: pw }),
      });

      if (res.ok) {
        router.push("/");
        router.refresh();
      } else {
        setError("Invalid password. Try again.");
        setPw("");
      }
    } catch {
      setError("Could not reach server. Try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{ background: "#0F1117" }}
    >
      <div
        className="w-full max-w-sm rounded-xl p-8 animate-fade-in"
        style={{
          background:  "#1A1F2E",
          border:      "1px solid rgba(255,255,255,0.06)",
          boxShadow:   "0 8px 32px rgba(0,0,0,0.4)",
        }}
      >
        {/* Brand */}
        <div className="text-center mb-8">
          <span className="text-3xl font-bold text-ls-blue tracking-tight">ARIA</span>
          <p className="text-gray-500 text-sm mt-1">
            Autonomous Rider Intelligence &amp; Analytics
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs text-gray-500 mb-1.5 tracking-wide uppercase">
              Password
            </label>
            <input
              type="password"
              value={pw}
              onChange={(e) => setPw(e.target.value)}
              placeholder="Enter access password"
              required
              autoFocus
              disabled={loading}
              className="w-full px-3 py-2.5 rounded-lg text-sm text-white outline-none
                         transition-colors placeholder-gray-600 disabled:opacity-50"
              style={{
                background: "#0F1117",
                border:     "1px solid rgba(255,255,255,0.1)",
              }}
              onFocus={(e) => (e.currentTarget.style.borderColor = "#4280FF88")}
              onBlur={(e)  => (e.currentTarget.style.borderColor = "rgba(255,255,255,0.1)")}
            />
          </div>

          {error && (
            <p className="text-red-400 text-xs">{error}</p>
          )}

          <button
            type="submit"
            disabled={loading || !pw}
            className="w-full py-2.5 rounded-lg text-sm font-semibold transition-opacity
                       disabled:opacity-40 disabled:cursor-not-allowed"
            style={{ background: "#4280FF", color: "#fff" }}
          >
            {loading ? "Verifying…" : "Access Dashboard"}
          </button>
        </form>
      </div>
    </div>
  );
}
