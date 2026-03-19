/**
 * ARIA — Zustand UI Store
 * ========================
 * UI/session state only. Server data lives in TanStack Query.
 *
 * Holds:
 *   - WebSocket connection status
 *   - Last received cycle_id (drives selective invalidation)
 *   - cycle_running flag (for agent pipeline animation)
 *   - Active city filter (for map)
 *   - Panel expanded states
 */

import { create } from "zustand";

export type WsStatus = "connecting" | "connected" | "disconnected" | "error";

interface UIState {
  // WebSocket
  wsStatus:         WsStatus;
  lastCycleId:      string | null;
  lastCycleAt:      string | null;
  cycleRunning:     boolean;

  // Map filter
  activeCityFilter: string | null;

  // Actions
  setWsStatus:       (s: WsStatus) => void;
  setLastCycle:      (cycleId: string, sentAt: string) => void;
  setCycleRunning:   (running: boolean) => void;
  setActiveCityFilter: (city: string | null) => void;
}

export const useUIStore = create<UIState>((set) => ({
  wsStatus:          "connecting",
  lastCycleId:       null,
  lastCycleAt:       null,
  cycleRunning:      false,
  activeCityFilter:  null,

  setWsStatus:       (wsStatus)   => set({ wsStatus }),
  setLastCycle:      (lastCycleId, lastCycleAt) => set({ lastCycleId, lastCycleAt }),
  setCycleRunning:   (cycleRunning) => set({ cycleRunning }),
  setActiveCityFilter: (activeCityFilter) => set({ activeCityFilter }),
}));
