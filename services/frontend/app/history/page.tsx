/**
 * /history — Past cycle briefings
 * =================================
 * Server component — fetches briefings via internal MCP URL at request time.
 * No WebSocket needed. Read-only, post-shift review.
 */

import { fetchTool } from "@/lib/api";
import { CycleBriefingSchema } from "@/lib/schemas";
import { formatDistanceToNow } from "date-fns";
import Link from "next/link";
import { ChevronLeft, Sparkles } from "lucide-react";

export const dynamic = "force-dynamic";

async function getBriefings() {
  try {
    // Actual API: { briefings: [{ cycle_id, briefing: {...}, severity_level, timestamp }] }
    const raw = await fetchTool<{ briefings: Record<string, unknown>[] }>(
      "cycle-briefing", { n: 50 }
    );
    return (raw.briefings ?? []).map((row) => {
      const content = (row.briefing as Record<string, unknown>) ?? {};
      return CycleBriefingSchema.safeParse({
        cycle_id:           row.cycle_id,
        timestamp:          row.timestamp,
        severity_level:     row.severity_level ?? content.severity_level,
        alert_count:        row.alert_count,
        execution_ms:       row.execution_ms,
        situation_summary:  content.situation_summary,
        actions_taken:      content.actions_taken,
        patterns_detected:  content.patterns_detected,
        agent_results:      content.agent_results,
        rag_context_used:   content.rag_context_used,
        rag_used_count:     content.rag_used_count,
        best_similarity:    content.best_similarity,
      });
    }).filter((r) => r.success).map((r) => r.data!);
  } catch {
    return [];
  }
}

export default async function HistoryPage() {
  const briefings = await getBriefings();

  return (
    <div className="min-h-screen p-6 max-w-4xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-4 mb-8">
        <Link
          href="/"
          className="flex items-center gap-1 text-xs text-gray-500 hover:text-ls-blue transition-colors"
        >
          <ChevronLeft className="w-3.5 h-3.5" />
          Dashboard
        </Link>
        <div>
          <h1 className="text-xl font-bold text-white flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-ls-blue" />
            Cycle History
          </h1>
          <p className="text-xs text-gray-500 mt-0.5">{briefings.length} cycles recorded</p>
        </div>
      </div>

      {/* Briefing list */}
      {briefings.length === 0 ? (
        <div className="panel p-12 text-center text-sm text-gray-600">
          No cycle briefings recorded yet. Run a cycle first.
        </div>
      ) : (
        <div className="space-y-3">
          {briefings.map((b) => (
            <div key={b.cycle_id} className="panel px-5 py-4 animate-fade-in">
              {/* Row header */}
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-2">
                  <span className={`badge badge-${b.severity_level}`}>{b.severity_level}</span>
                  <span className="text-[10px] text-gray-600 font-mono">
                    {b.cycle_id?.slice(0, 12)}…
                  </span>
                </div>
                <span className="text-[10px] text-gray-600">
                  {formatDistanceToNow(new Date(b.timestamp), { addSuffix: true })}
                </span>
              </div>

              {/* Situation */}
              <p className="text-sm text-gray-300 leading-relaxed mb-2">
                {b.situation_summary}
              </p>

              {/* Patterns */}
              {b.patterns_detected.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {b.patterns_detected.map((p, i) => (
                    <span key={i} className={`badge badge-${p.severity} text-[10px]`}>
                      {p.type.replace(/_/g, " ")}
                    </span>
                  ))}
                </div>
              )}

              {/* RAG badge */}
              {b.rag_context_used && (
                <p className="text-[10px] text-ls-blue mt-2">
                  RAG: {b.rag_used_count} episode(s) · similarity {b.best_similarity?.toFixed(2)}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
