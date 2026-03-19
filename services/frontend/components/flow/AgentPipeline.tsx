"use client";

/**
 * AgentPipeline — Intelligence Panel
 * ====================================
 * Full right-column panel. Absorbs CycleBriefingPanel.
 * Shows:
 *   1. Mini flow diagram (static SVG — 4 agents → supervisor)
 *   2. One card per sub-agent with real metrics from *_summary strings
 *   3. Supervisor section: situation_summary + recommended actions
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Brain, ChevronRight, Map, Store, Truck, TrendingUp, Sparkles, RefreshCw } from "lucide-react";
import { QK, clientFetchTool, normaliseBriefing } from "@/lib/api";
import { type CycleBriefing } from "@/lib/schemas";
import { useUIStore } from "@/store/uiStore";
import { PanelError } from "@/components/shared/PanelError";
import { PanelSkeleton } from "@/components/shared/PanelSkeleton";
import { LastUpdated } from "@/components/shared/LastUpdated";

// ── Status colours ────────────────────────────────────────────────────────────

const STATUS_COLOR: Record<string, string> = {
  ok:      "#22C55E",
  success: "#22C55E",
  partial: "#EAB308",
  failed:  "#EF4444",
  missing: "#6B7280",
  running: "#4280FF",
  idle:    "#374151",
};

function statusColor(s: string, cycleRunning: boolean) {
  return STATUS_COLOR[cycleRunning ? "running" : s] ?? STATUS_COLOR.idle;
}

// ── Mini SVG flow diagram ─────────────────────────────────────────────────────

function FlowDiagram({
  statuses,
  cycleRunning,
}: {
  statuses: Record<string, string>;
  cycleRunning: boolean;
}) {
  const agents = ["zone", "restaurant", "dead_run", "earnings"];
  const ys      = [18, 42, 66, 90];   // y centres for the 4 left nodes
  const supY    = 54;                 // supervisor y centre
  const nodeR   = 6;

  return (
    <svg width="100%" height="108" viewBox="0 0 260 108" className="overflow-visible">
      {/* Left agent nodes + edge lines */}
      {agents.map((id, i) => {
        const col = statusColor(statuses[id] ?? "idle", cycleRunning);
        return (
          <g key={id}>
            <line x1={70} y1={ys[i]} x2={186} y2={supY}
              stroke={col} strokeWidth={1.5} strokeOpacity={0.5}
              strokeDasharray={cycleRunning ? "4 3" : "none"} />
            <circle cx={62} cy={ys[i]} r={nodeR} fill={`${col}22`} stroke={col} strokeWidth={1.5} />
            <text x={74} y={ys[i] + 4} fill="#9CA3AF" fontSize={10} textAnchor="start">{
              { zone: "Zone Intel", restaurant: "Restaurant", dead_run: "Dead Run", earnings: "Earnings Guard" }[id]
            }</text>
          </g>
        );
      })}
      {/* Supervisor node */}
      {(() => {
        const col = statusColor(statuses.supervisor ?? "idle", cycleRunning);
        return (
          <g>
            <circle cx={194} cy={supY} r={nodeR + 2} fill={`${col}22`} stroke={col} strokeWidth={2} />
            <text x={206} y={supY + 4} fill="#E5E7EB" fontSize={10} fontWeight="600">Supervisor</text>
          </g>
        );
      })()}
    </svg>
  );
}

// ── Parse summary string into key metric chips ────────────────────────────────

function parseZone(s: string) {
  const dead     = s.match(/(\d+) dead/)?.[1];
  const stressed = s.match(/(\d+) stressed/)?.[1];
  const riders   = s.match(/(\d+) riders? recommended/)?.[1];
  return [
    dead     && { label: `${dead} dead zones`,     color: dead !== "0"     ? "#EF4444" : "#22C55E" },
    stressed && { label: `${stressed} stressed`,    color: stressed !== "0" ? "#EAB308" : "#22C55E" },
    riders   && { label: `${riders} riders repositioned`, color: "#4280FF" },
  ].filter(Boolean) as { label: string; color: string }[];
}

function parseRestaurant(s: string) {
  const high   = s.match(/(\d+) high-risk/)?.[1];
  const rider  = s.match(/\((\d+) rider/)?.[1];
  const op     = s.match(/(\d+) operator\)/)?.[1];
  return [
    high  && { label: `${high} high-risk`,       color: high !== "0"  ? "#EF4444" : "#22C55E" },
    rider && { label: `${rider} rider alerts`,    color: "#F97316" },
    op    && { label: `${op} operator alerts`,    color: "#EAB308" },
  ].filter(Boolean) as { label: string; color: string }[];
}

function parseDeadRun(s: string) {
  const scored  = s.match(/(\d+) scored/)?.[1];
  const flagged = s.match(/(\d+) flagged/)?.[1];
  const eph     = s.match(/Rs\.(\d+) EPH at risk/)?.[1];
  return [
    scored  && { label: `${scored} orders scored`,   color: "#9CA3AF" },
    flagged && { label: `${flagged} flagged`,         color: flagged !== "0" ? "#EF4444" : "#22C55E" },
    eph     && { label: `Rs.${eph} EPH at risk`,     color: eph !== "0"     ? "#F97316" : "#22C55E" },
  ].filter(Boolean) as { label: string; color: string }[];
}

function parseEarnings(s: string) {
  const atRisk  = s.match(/(\d+) at-risk/)?.[1];
  const churn   = s.match(/(\d+) churn-risk/)?.[1];
  const eph     = s.match(/avg EPH Rs\.(\d+)/)?.[1];
  const short   = s.match(/shortfall Rs\.(\d+)/)?.[1];
  return [
    eph    && { label: `avg EPH Rs.${eph}/hr`,      color: "#4280FF" },
    atRisk && { label: `${atRisk} at-risk`,          color: atRisk !== "0" ? "#EF4444" : "#22C55E" },
    churn  && { label: `${churn} churn-risk`,        color: churn  !== "0" ? "#F97316" : "#22C55E" },
    short  && { label: `Rs.${short} shortfall`,      color: "#9CA3AF" },
  ].filter(Boolean) as { label: string; color: string }[];
}

// ── Agent card ────────────────────────────────────────────────────────────────

const AGENT_CONFIG = [
  {
    id:      "zone",
    label:   "Zone Intelligence",
    Icon:    Map,
    desc:    "Density · Dead zone detection · Repositioning",
    parse:   parseZone,
    what:    "ML-scored zone snapshots → recommends rider moves",
  },
  {
    id:      "restaurant",
    label:   "Restaurant Risk",
    Icon:    Store,
    desc:    "Delay risk · Ripple detection · Operator alerts",
    parse:   parseRestaurant,
    what:    "Statistical z-score vs historical baseline → flags high-delay restaurants",
  },
  {
    id:      "dead_run",
    label:   "Dead Run Prevention",
    Icon:    Truck,
    desc:    "Order risk scoring · Dispatch guard",
    parse:   parseDeadRun,
    what:    "ML model scores every pending order for dead zone probability before dispatch",
  },
  {
    id:      "earnings",
    label:   "Earnings Guardian",
    Icon:    TrendingUp,
    desc:    "EPH trajectory · Churn escalation",
    parse:   parseEarnings,
    what:    "ML model projects final-shift EPH per rider → flags churn risk early",
  },
];

function AgentCard({
  label,
  Icon,
  what,
  parse,
  result,
  cycleRunning,
}: {
  label:        string;
  Icon:         React.ComponentType<{ className?: string; style?: React.CSSProperties }>;
  what:         string;
  parse:        (s: string) => { label: string; color: string }[];
  result?:      { status?: string; summary_text?: string; alert_count?: number; severity?: string };
  cycleRunning: boolean;
}) {
  const status  = cycleRunning ? "running" : (result?.status ?? "idle");
  const col     = statusColor(status, false);
  const metrics = useMemo(() => parse(result?.summary_text ?? ""), [result?.summary_text]);

  return (
    <div
      className="rounded-lg px-3 py-2.5 flex flex-col gap-1"
      style={{ background: `${col}0A`, border: `1px solid ${col}30` }}
    >
      {/* Row 1: icon + name + status */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <Icon className="w-3.5 h-3.5 shrink-0" style={{ color: col }} />
          <span className="text-xs font-semibold text-white truncate">{label}</span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {result?.alert_count != null && result.alert_count > 0 && (
            <span className="text-[10px] tabular-nums" style={{ color: col }}>
              {result.alert_count} alerts
            </span>
          )}
          <span
            className="text-[9px] font-bold px-1.5 py-0.5 rounded"
            style={{ color: col, background: `${col}22`, border: `1px solid ${col}44` }}
          >
            {status}
          </span>
        </div>
      </div>

      {/* Row 2: what it does (faint) */}
      <p className="text-[10px] text-gray-600 leading-none">{what}</p>

      {/* Row 3: metric chips */}
      {metrics.length > 0 ? (
        <div className="flex flex-wrap gap-1 mt-0.5">
          {metrics.map((m, i) => (
            <span
              key={i}
              className="text-[10px] px-1.5 py-0.5 rounded"
              style={{ color: m.color, background: `${m.color}18`, border: `1px solid ${m.color}30` }}
            >
              {m.label}
            </span>
          ))}
        </div>
      ) : (
        <p className="text-[10px] text-gray-600 italic">{cycleRunning ? "Running…" : "No data yet"}</p>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function AgentPipeline() {
  const cycleRunning = useUIStore((s) => s.cycleRunning);
  const lastCycleAt  = useUIStore((s) => s.lastCycleAt);

  const { data, isLoading, isError, refetch } = useQuery<CycleBriefing>({
    queryKey: QK.cycleBriefing("latest"),
    queryFn:  () =>
      clientFetchTool<{ briefings: Record<string, unknown>[] }>("cycle-briefing", { n: 1 })
        .then(({ briefings }) => normaliseBriefing(briefings[0] ?? {})),
    staleTime: 60_000,
  });

  const results = data?.agent_results ?? {};
  const statuses = useMemo(() => {
    const s: Record<string, string> = {};
    for (const id of ["zone", "restaurant", "dead_run", "earnings", "supervisor"]) {
      const r = (results as Record<string, { status?: string } | undefined>)[id];
      s[id] = r?.status ?? "idle";
    }
    return s;
  }, [results]);

  if (isLoading) return <div className="panel h-full"><PanelSkeleton rows={10} /></div>;
  if (isError)   return (
    <div className="panel h-full">
      <PanelError title="Intelligence panel unavailable" message="Agent cycle has not completed yet." onRetry={refetch} />
    </div>
  );

  const severity = data?.severity_level ?? "normal";
  const sevColor = severity === "critical" ? "#EF4444" : severity === "warning" ? "#EAB308" : "#22C55E";

  return (
    <div className="panel h-full flex flex-col overflow-hidden">

      {/* ── Header ── */}
      <div className="px-4 pt-3 pb-2 border-b border-white/[0.06] flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2">
          <Brain className="w-4 h-4 text-ls-blue" />
          <h2 className="text-sm font-semibold text-white">Intelligence Pipeline</h2>
        </div>
        <div className="flex items-center gap-2">
          {cycleRunning && (
            <span className="flex items-center gap-1 text-[10px] text-ls-blue animate-pulse">
              <RefreshCw className="w-3 h-3 animate-spin" /> Running
            </span>
          )}
          {data?.rag_context_used && (
            <span className="text-[10px] text-ls-blue">RAG ({data.rag_used_count ?? 0})</span>
          )}
          <span
            className="text-[10px] font-bold px-2 py-0.5 rounded"
            style={{ color: sevColor, background: `${sevColor}22`, border: `1px solid ${sevColor}40` }}
          >
            {severity}
          </span>
          <LastUpdated timestamp={lastCycleAt} className="text-[10px]" />
        </div>
      </div>

      {/* ── Scrollable body ── */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2 min-h-0">

        {/* Mini flow diagram */}
        <div className="px-1">
          <FlowDiagram statuses={statuses} cycleRunning={cycleRunning} />
        </div>

        {/* 4 agent cards */}
        {AGENT_CONFIG.map(({ id, label, Icon, what, parse }) => (
          <AgentCard
            key={id}
            label={label}
            Icon={Icon}
            what={what}
            parse={parse}
            result={(results as Record<string, { status?: string; summary_text?: string; alert_count?: number } | undefined>)[id]}
            cycleRunning={cycleRunning}
          />
        ))}

        {/* ── Supervisor section ── */}
        <div
          className="rounded-lg px-3 py-2.5 space-y-2"
          style={{ background: "#4280FF0A", border: "1px solid #4280FF30" }}
        >
          <div className="flex items-center gap-1.5">
            <Sparkles className="w-3.5 h-3.5 text-ls-blue" />
            <span className="text-xs font-semibold text-white">Supervisor Briefing</span>
            <span className="text-[10px] text-gray-500 ml-auto">RAG + LLM synthesis</span>
          </div>

          {data?.situation_summary && data.situation_summary !== "No briefing available yet." ? (
            <p className="text-xs text-gray-300 leading-relaxed">{data.situation_summary}</p>
          ) : (
            <p className="text-xs text-gray-600 italic">Awaiting first cycle…</p>
          )}

          {data?.patterns_detected && data.patterns_detected.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {data.patterns_detected.map((p, i) => (
                <span
                  key={i}
                  className={`badge badge-${p.severity} text-[10px]`}
                >
                  {p.type.replace(/_/g, " ")}
                </span>
              ))}
            </div>
          )}

          {data?.actions_taken && data.actions_taken.length > 0 && (
            <ul className="space-y-1">
              {data.actions_taken.map((a, i) => (
                <li key={i} className="flex items-start gap-1.5 text-[11px] text-gray-400 leading-snug">
                  <ChevronRight className="w-3 h-3 text-ls-blue mt-0.5 shrink-0" />
                  {a}
                </li>
              ))}
            </ul>
          )}
        </div>

      </div>

      {/* ── Footer ── */}
      <div className="px-4 py-1.5 border-t border-white/[0.06] flex items-center justify-between shrink-0">
        <span className="text-[10px] text-gray-700 font-mono">{data?.cycle_id?.slice(0, 8)}…</span>
        <span className="text-[10px] text-gray-600">{data?.alert_count ?? 0} alerts · {data?.execution_ms ?? 0}ms</span>
      </div>
    </div>
  );
}
