"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Store, ChevronLeft, ChevronRight } from "lucide-react";
import { clientFetchTool, QK } from "@/lib/api";
import { RestaurantRisksSchema, type RestaurantRisk } from "@/lib/schemas";
import { PanelError } from "@/components/shared/PanelError";
import { PanelSkeleton } from "@/components/shared/PanelSkeleton";
import { LastUpdated } from "@/components/shared/LastUpdated";

const PAGE_SIZE = 10;

function RiskBar({ score }: { score: number }) {
  const pct   = Math.round(score * 100);
  const color = score >= 0.8 ? "#EF4444" : score >= 0.65 ? "#F97316" : "#EAB308";
  return (
    <div className="flex items-center gap-2">
      <div className="w-16 h-1.5 bg-white/10 rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="text-xs tabular-nums" style={{ color }}>{pct}%</span>
    </div>
  );
}

/**
 * Parse the raw explanation string from the restaurant agent into a human-readable sentence.
 * Input:  "z=1.492, deviation=4.88 min, baseline=0.27 min, active_pickups=2, severity=critical"
 * Output: "4.9 min above baseline · 2 active pickups"
 */
function parseExplanation(explanation: string | null | undefined): string {
  if (!explanation) return "—";
  const get = (key: string) => {
    const m = explanation.match(new RegExp(`${key}=([\\d.+-]+)`));
    return m ? parseFloat(m[1]) : null;
  };
  const dev     = get("deviation");
  const base    = get("baseline");
  const pickups = get("active_pickups");

  const parts: string[] = [];
  if (dev != null && Math.abs(dev) > 0.1) {
    const dir = dev > 0 ? "above" : "below";
    parts.push(`${Math.abs(dev).toFixed(1)} min ${dir} baseline`);
  } else if (base != null) {
    parts.push(`baseline ${base.toFixed(1)} min`);
  }
  if (pickups != null) {
    parts.push(`${pickups} active pickup${pickups !== 1 ? "s" : ""}`);
  }
  return parts.length > 0 ? parts.join(" · ") : explanation.slice(0, 60);
}

function Row({ r }: { r: RestaurantRisk }) {
  const readable = parseExplanation(r.explanation);
  return (
    <tr className="border-t border-white/[0.04] hover:bg-white/[0.02] transition-colors">
      <td className="px-4 py-2.5 text-xs text-gray-200 max-w-[120px]">
        <span className="block truncate" title={r.name}>{r.name}</span>
      </td>
      <td className="px-4 py-2.5 text-xs text-gray-500">{r.city}</td>
      <td className="px-4 py-2.5"><RiskBar score={r.delay_risk_score} /></td>
      <td className="px-4 py-2.5 text-xs text-gray-400 tabular-nums">
        {r.expected_delay_mins != null ? `+${r.expected_delay_mins.toFixed(1)} min` : "—"}
      </td>
      <td
        className="px-4 py-2.5 text-xs text-gray-400"
        title={r.explanation ?? undefined}
      >
        {readable}
      </td>
    </tr>
  );
}

export function RestaurantTable() {
  const [page, setPage] = useState(1);

  const { data, isLoading, isError, refetch, dataUpdatedAt } = useQuery({
    queryKey: QK.restaurantRisk(page),
    queryFn:  () =>
      clientFetchTool<unknown>("restaurant-risks", { limit: 50 })
        .then((raw) => RestaurantRisksSchema.parse(raw)),
    staleTime:       10_000,
    refetchInterval: 90_000,   // fallback poll if WS drops; WS cycle_complete triggers immediately
    placeholderData: (prev) => prev,
  });

  const allRows    = data?.restaurants ?? [];
  const totalRows  = data?.count ?? 0;
  const start      = (page - 1) * PAGE_SIZE;
  const pageRows   = allRows.slice(start, start + PAGE_SIZE);
  const totalPages = Math.ceil(allRows.length / PAGE_SIZE) || 1;

  return (
    <div className="panel flex flex-col h-full">
      <div className="flex items-center justify-between px-4 pt-3 pb-2 border-b border-white/[0.06]">
        <div className="flex items-center gap-2">
          <Store className="w-4 h-4 text-ls-blue" />
          <h2 className="text-sm font-semibold text-white">Restaurant Risk</h2>
          {data && <span className="text-xs text-gray-500">({totalRows})</span>}
        </div>
        <LastUpdated timestamp={dataUpdatedAt ? new Date(dataUpdatedAt).toISOString() : null} />
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading && <PanelSkeleton rows={6} />}
        {isError   && <PanelError title="Restaurant data unavailable" onRetry={refetch} />}
        {data && !isLoading && (
          <table className="w-full">
            <thead>
              <tr className="text-[10px] uppercase text-gray-600 tracking-wider">
                <th className="px-4 py-2 text-left">Restaurant</th>
                <th className="px-4 py-2 text-left">City</th>
                <th className="px-4 py-2 text-left">Delay Risk</th>
                <th className="px-4 py-2 text-left">Exp. Delay</th>
                <th className="px-4 py-2 text-left">Reason</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-xs text-gray-600">
                    No high-risk restaurants this cycle
                  </td>
                </tr>
              ) : (
                pageRows.map((r) => <Row key={r.restaurant_id} r={r} />)
              )}
            </tbody>
          </table>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-between px-4 py-2 border-t border-white/[0.06]">
          <span className="text-[10px] text-gray-600">Page {page} of {totalPages}</span>
          <div className="flex gap-1">
            <button disabled={page === 1} onClick={() => setPage((p) => p - 1)}
              className="p-1 rounded hover:bg-white/[0.05] disabled:opacity-30 transition-colors">
              <ChevronLeft className="w-3.5 h-3.5 text-gray-400" />
            </button>
            <button disabled={page === totalPages} onClick={() => setPage((p) => p + 1)}
              className="p-1 rounded hover:bg-white/[0.05] disabled:opacity-30 transition-colors">
              <ChevronRight className="w-3.5 h-3.5 text-gray-400" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
