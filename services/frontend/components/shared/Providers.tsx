"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useRef } from "react";

export function Providers({ children }: { children: React.ReactNode }) {
  // Stable QueryClient per browser session
  const qcRef = useRef<QueryClient | null>(null);
  if (!qcRef.current) {
    qcRef.current = new QueryClient({
      defaultOptions: {
        queries: {
          // Tables: 60s stale time (their own TTL, not invalidated on cycle push)
          staleTime: 60_000,
          // Don't retry on 404 / 502 — show error boundary instead
          retry: (count, err) => {
            const msg = err instanceof Error ? err.message : "";
            if (msg.includes("404") || msg.includes("502")) return false;
            return count < 2;
          },
        },
      },
    });
  }

  return (
    <QueryClientProvider client={qcRef.current}>
      {children}
    </QueryClientProvider>
  );
}
