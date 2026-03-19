"use client";

/**
 * DashboardShell
 * ================
 * Initialises the WebSocket connection for the session.
 * Wraps the dashboard in the TanStack Query provider.
 * Children = dashboard panels.
 */

import { useWebSocket } from "@/hooks/useWebSocket";

export function DashboardShell({ children }: { children: React.ReactNode }) {
  useWebSocket();   // single WS connection for the whole session
  return <>{children}</>;
}
