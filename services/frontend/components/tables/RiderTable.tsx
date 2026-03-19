"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Users, ChevronLeft, ChevronRight, ChevronDown, ChevronUp } from "lucide-react";
import { clientFetchTool, QK } from "@/lib/api";
import { RiderInterventionsSchema, type RiderIntervention } from "@/lib/schemas";
import { PanelError } from "@/components/shared/PanelError";
import { PanelSkeleton } from "@/components/shared/PanelSkeleton";
import { LastUpdated } from "@/components/shared/LastUpdated";

const PAGE_SIZE = 10;

const PRIORITY_BADGE: Record<string, string> = {
  high:   "badge-critical",
  medium: "badge-warning",
  low:    "badge-partial",
};

function Row({ r, expanded, onToggle }: { r: RiderIntervention; expanded: boolean; onToggle: () => void }) {
  return (
    <>
      <tr
        className="border-t border-white/[0.04] hover:bg-white/[0.02] transition-colors cursor-pointer select-none"
        onClick={onToggle}
      >
        <td className="px-4 py-2.5">
          <p className="text-xs text-gray-200">{r.rider_name ?? "—"}</p>
          <p className="text-[10px] text-gray-600 font-mono">{r.rider_id.slice(0, 8)}</p>
        </td>
        <td className="px-4 py-2.5">
          <span className={`badge ${PRIORITY_BADGE[r.priority] ?? "badge-partial"}`}>
            {r.priority}
          </span>
        </td>
        <td className="px-4 py-2.5 text-xs text-gray-400 max-w-[240px]">
          {expanded ? (
            <span className="leading-relaxed whitespace-pre-wrap">{r.recommendation_text}</span>
          ) : (
            <span className="line-clamp-2 leading-relaxed">{r.recommendation_text}</span>
          )}
        </td>
        <td className="px-4 py-2.5 text-xs">
          {r.recommended_zone ? (
            <span className="text-ls-blue">{r.recommended_zone}</span>
          ) : (
            <span className="text-gray-600 italic">—</span>
          )}
          {r.recommended_zone_city && (
            <p className="text-[10px] text-gray-600">{r.recommended_zone_city}</p>
          )}
        </td>
        <td className="px-4 py-2.5 text-xs text-gray-500">{r.persona_type ?? "—"}</td>
        <td className="px-4 py-2.5">
          {r.was_acted_on === true  && <span className="badge badge-normal text-[9px]">Actioned</span>}
          {r.was_acted_on === false && <span className="badge badge-failed text-[9px]">Ignored</span>}
          {r.was_acted_on == null   && <span className="text-xs text-gray-600">Pending</span>}
        </td>
        <td className="px-4 py-2.5 text-gray-600">
          {expanded
            ? <ChevronUp className="w-3.5 h-3.5" />
            : <ChevronDown className="w-3.5 h-3.5" />
          }
        </td>
      </tr>
    </>
  );
}

export function RiderTable() {
  const [page, setPage]         = useState(1);
  const [expandedId, setExpanded] = useState<string | null>(null);

  const { data, isLoading, isError, refetch, dataUpdatedAt } = useQuery({
    queryKey: QK.riderInterventions(page),
    queryFn:  () =>
      clientFetchTool<unknown>("rider-interventions", { limit: 100 })
        .then((raw) => RiderInterventionsSchema.parse(raw)),
    staleTime:       10_000,
    refetchInterval: 90_000,   // fallback poll if WS drops; WS cycle_complete triggers immediately
    placeholderData: (prev) => prev,
  });

  const allRows    = data?.interventions ?? [];
  const totalRows  = data?.count ?? 0;
  const start      = (page - 1) * PAGE_SIZE;
  const pageRows   = allRows.slice(start, start + PAGE_SIZE);
  const totalPages = Math.ceil(allRows.length / PAGE_SIZE) || 1;

  return (
    <div className="panel flex flex-col h-full">
      <div className="flex items-center justify-between px-4 pt-3 pb-2 border-b border-white/[0.06]">
        <div className="flex items-center gap-2">
          <Users className="w-4 h-4 text-ls-blue" />
          <h2 className="text-sm font-semibold text-white">Rider Interventions</h2>
          {data && <span className="text-xs text-gray-500">({totalRows})</span>}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[10px] text-gray-600">Click row to expand</span>
          <LastUpdated timestamp={dataUpdatedAt ? new Date(dataUpdatedAt).toISOString() : null} />
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading && <PanelSkeleton rows={6} />}
        {isError   && <PanelError title="Rider data unavailable" onRetry={refetch} />}
        {data && !isLoading && (
          <table className="w-full">
            <thead>
              <tr className="text-[10px] uppercase text-gray-600 tracking-wider">
                <th className="px-4 py-2 text-left">Rider</th>
                <th className="px-4 py-2 text-left">Priority</th>
                <th className="px-4 py-2 text-left">Recommendation</th>
                <th className="px-4 py-2 text-left">Recommended Zone</th>
                <th className="px-4 py-2 text-left">Persona</th>
                <th className="px-4 py-2 text-left">Status</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {pageRows.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-xs text-gray-600">
                    No active interventions this cycle
                  </td>
                </tr>
              ) : (
                pageRows.map((r) => (
                  <Row
                    key={r.intervention_id}
                    r={r}
                    expanded={expandedId === r.intervention_id}
                    onToggle={() => setExpanded(
                      expandedId === r.intervention_id ? null : r.intervention_id
                    )}
                  />
                ))
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
