"use client";

import { useQuery } from "@tanstack/react-query";
import { Sparkles, Brain, ChevronRight } from "lucide-react";
import { clientFetchTool, normaliseBriefing, QK } from "@/lib/api";
import { type CycleBriefing } from "@/lib/schemas";
import { PanelError } from "@/components/shared/PanelError";
import { PanelSkeleton } from "@/components/shared/PanelSkeleton";
import { LastUpdated } from "@/components/shared/LastUpdated";

// ── Pattern chip ──────────────────────────────────────────────────────────────

function PatternChip({ type, severity }: { type: string; severity: string }) {
  const label = type.replace(/_/g, " ");
  const cls   = `badge badge-${severity} text-[10px]`;
  return <span className={cls}>{label}</span>;
}

// ── Action item ───────────────────────────────────────────────────────────────

function ActionItem({ text }: { text: string }) {
  return (
    <li className="flex items-start gap-2 text-xs text-gray-300 leading-relaxed">
      <ChevronRight className="w-3 h-3 text-ls-blue mt-0.5 shrink-0" />
      {text}
    </li>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function CycleBriefingPanel() {
  const { data, isLoading, isError, refetch } = useQuery<CycleBriefing>({
    queryKey: QK.cycleBriefing("latest"),
    queryFn: () =>
      clientFetchTool<{ briefings: Record<string, unknown>[] }>("cycle-briefing", { n: 1 })
        .then(({ briefings }) => normaliseBriefing(briefings[0] ?? {})),
    staleTime: 60_000,
  });

  if (isLoading) return <div className="panel h-full"><PanelSkeleton rows={6} /></div>;
  if (isError)   return (
    <div className="panel h-full">
      <PanelError
        title="Briefing unavailable"
        message="Agent cycle has not completed yet."
        onRetry={refetch}
      />
    </div>
  );
  if (!data)     return null;

  const severityBadge = `badge-${data.severity_level}`;

  return (
    <div className="panel h-full flex flex-col overflow-hidden">
      {/* Header */}
      <div className="px-4 pt-3 pb-2 border-b border-white/[0.06] flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-ls-blue" />
          <h2 className="text-sm font-semibold text-white">Cycle Briefing</h2>
        </div>
        <div className="flex items-center gap-2">
          {data.rag_context_used && (
            <span className="flex items-center gap-1 text-[10px] text-ls-blue">
              <Brain className="w-3 h-3" />
              RAG ({data.rag_used_count ?? 0} ep.)
            </span>
          )}
          <span className={`badge ${severityBadge}`}>{data.severity_level}</span>
        </div>
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
        {/* Situation */}
        <div>
          <p className="text-[10px] font-semibold uppercase text-gray-500 mb-1">Situation</p>
          <p className="text-sm text-gray-200 leading-relaxed">{data.situation_summary}</p>
        </div>

        {/* Patterns */}
        {data.patterns_detected.length > 0 && (
          <div>
            <p className="text-[10px] font-semibold uppercase text-gray-500 mb-1.5">Patterns Detected</p>
            <div className="flex flex-wrap gap-1.5">
              {data.patterns_detected.map((p, i) => (
                <PatternChip key={i} type={p.type} severity={p.severity} />
              ))}
            </div>
          </div>
        )}

        {/* Actions */}
        {data.actions_taken.length > 0 && (
          <div>
            <p className="text-[10px] font-semibold uppercase text-gray-500 mb-1.5">Recommended Actions</p>
            <ul className="space-y-1.5">
              {data.actions_taken.map((a, i) => (
                <ActionItem key={i} text={a} />
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="px-4 py-2 border-t border-white/[0.06] flex items-center justify-between">
        <span className="text-[10px] text-gray-600 font-mono">{data.cycle_id?.slice(0, 8)}…</span>
        <LastUpdated timestamp={data.timestamp} />
      </div>
    </div>
  );
}
