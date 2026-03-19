/**
 * ARIA — WebSocket hook
 * ======================
 * Connects to /ws on the MCP server (via Next.js websocket proxy or direct).
 * Implements exponential backoff reconnect (1s → 2s → 4s → max 30s).
 * Parses WsEvent contract; on cycle_complete → invalidates TanStack Query keys.
 * Updates Zustand UI store for connection status and last cycle_id.
 */

"use client";

import { useEffect, useRef, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { WsEventSchema } from "@/lib/schemas";
import { QK } from "@/lib/api";
import { useUIStore } from "@/store/uiStore";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8001/ws";
const MAX_BACKOFF_MS = 30_000;

export function useWebSocket() {
  const qc              = useQueryClient();
  const setWsStatus     = useUIStore((s) => s.setWsStatus);
  const setLastCycle    = useUIStore((s) => s.setLastCycle);
  const setCycleRunning = useUIStore((s) => s.setCycleRunning);

  const wsRef           = useRef<WebSocket | null>(null);
  const backoffRef      = useRef<number>(1_000);
  const reconnectTimer  = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef      = useRef(true);

  const handleMessage = useCallback((raw: string) => {
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { return; }

    const result = WsEventSchema.safeParse(parsed);
    if (!result.success) return;

    const event = result.data;

    if (event.type === "cycle_complete" && event.cycle_id) {
      const cycleId = event.cycle_id;
      const sentAt  = event.sent_at ?? new Date().toISOString();

      setLastCycle(cycleId, sentAt);
      setCycleRunning(false);

      // Invalidate everything that depends on a completed cycle
      qc.invalidateQueries({ queryKey: QK.kpiSummary });
      qc.invalidateQueries({ queryKey: QK.cycleBriefing(cycleId) });
      qc.invalidateQueries({ queryKey: QK.cycleBriefing("latest") });
      qc.invalidateQueries({ queryKey: QK.zoneStress(cycleId) });
      // Tables: invalidate all pages (exact: false matches any page suffix)
      qc.invalidateQueries({ queryKey: ["rider-interventions"], exact: false });
      qc.invalidateQueries({ queryKey: ["restaurant-risk"],     exact: false });
      qc.invalidateQueries({ queryKey: ["order-summary"] });
    }

    if (event.type === "cycle_start") {
      setCycleRunning(true);
    }
  }, [qc, setLastCycle, setCycleRunning]);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    setWsStatus("connecting");

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      backoffRef.current = 1_000;   // reset on successful connect
      setWsStatus("connected");
    };

    ws.onmessage = (e) => handleMessage(e.data);

    ws.onerror = () => setWsStatus("error");

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setWsStatus("disconnected");
      const delay = backoffRef.current;
      backoffRef.current = Math.min(delay * 2, MAX_BACKOFF_MS);
      reconnectTimer.current = setTimeout(connect, delay);
    };
  }, [handleMessage, setWsStatus]);

  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);
}
