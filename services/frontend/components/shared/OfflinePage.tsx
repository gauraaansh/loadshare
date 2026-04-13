"use client";

/**
 * OfflinePage
 * ============
 * Shown when the ARIA backend services are unreachable.
 * Auto-polls /api/health every 30 seconds and reloads
 * the page when the servers come back online.
 */

import { useState, useEffect, useCallback } from "react";

const SERVICES = [
  "Event Stream / Simulator",
  "MCP Server / Agent Orchestrator",
  "ML Inference Server",
  "vLLM  ·  Qwen2.5-32B-Instruct",
  "PostgreSQL + TimescaleDB",
  "Redis",
];

const POLL_INTERVAL = 30; // seconds between auto-checks

export function OfflinePage() {
  const [checking,  setChecking]  = useState(false);
  const [countdown, setCountdown] = useState(POLL_INTERVAL);

  const checkHealth = useCallback(async () => {
    setChecking(true);
    try {
      const res  = await fetch("/aria/api/health", { cache: "no-store" });
      const data = await res.json();
      if (data.online) {
        // Servers are back — reload into the dashboard
        window.location.reload();
        return;
      }
    } catch {
      // still offline
    }
    setChecking(false);
    setCountdown(POLL_INTERVAL);
  }, []);

  // Countdown ticker; fires checkHealth when it hits zero
  useEffect(() => {
    const id = setInterval(() => {
      setCountdown((c) => {
        if (c <= 1) {
          checkHealth();
          return POLL_INTERVAL;
        }
        return c - 1;
      });
    }, 1_000);
    return () => clearInterval(id);
  }, [checkHealth]);

  return (
    <div
      className="min-h-screen flex items-center justify-center px-4"
      style={{ background: "#0F1117" }}
    >
      <div
        className="w-full max-w-md rounded-xl p-10"
        style={{
          background: "#1A1F2E",
          border:     "1px solid rgba(255,255,255,0.06)",
          boxShadow:  "0 8px 32px rgba(0,0,0,0.5)",
        }}
      >
        {/* Brand */}
        <div className="text-center mb-8">
          <span className="text-3xl font-bold tracking-tight" style={{ color: "#4280FF" }}>
            ARIA
          </span>
          <p className="text-gray-500 text-sm mt-1">
            Autonomous Rider Intelligence &amp; Analytics
          </p>
        </div>

        {/* Status badge */}
        <div className="flex items-center justify-center gap-2 mb-6">
          <span className="w-2.5 h-2.5 rounded-full bg-red-500 animate-pulse" />
          <span className="text-red-400 text-sm font-semibold tracking-wide">
            Demo Environment Offline
          </span>
        </div>

        {/* Explanation */}
        <p className="text-gray-400 text-sm leading-relaxed text-center mb-8">
          The backend services are currently stopped to conserve GPU and compute
          resources. The system will be available again when the stack is brought
          online — this page checks automatically.
        </p>

        {/* Service list */}
        <div className="space-y-1.5 mb-8">
          {SERVICES.map((name) => (
            <div
              key={name}
              className="flex items-center justify-between px-3 py-2 rounded-lg"
              style={{ background: "#0F1117" }}
            >
              <span className="text-sm text-gray-400">{name}</span>
              <div className="flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-gray-700" />
                <span className="text-xs text-gray-600">Offline</span>
              </div>
            </div>
          ))}
        </div>

        {/* Reconnect button */}
        <button
          onClick={checkHealth}
          disabled={checking}
          className="w-full py-2.5 rounded-lg text-sm font-semibold transition-opacity
                     disabled:opacity-50 disabled:cursor-not-allowed"
          style={{
            background: "#4280FF18",
            color:      "#4280FF",
            border:     "1px solid #4280FF44",
          }}
        >
          {checking
            ? "Checking…"
            : `Check Again  ·  ${countdown}s`}
        </button>

        {/* Footer note */}
        <p className="text-center text-xs text-gray-700 mt-6">
          Built on Loadshare Networks research · Portfolio project
        </p>
      </div>
    </div>
  );
}
