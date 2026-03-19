"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Users, AlertTriangle, TrendingDown, Store, Clock, LogOut } from "lucide-react";
import { QK } from "@/lib/api";
import { KpiSummarySchema, type KpiSummary } from "@/lib/schemas";
import { useUIStore, type WsStatus } from "@/store/uiStore";
import { LastUpdated } from "@/components/shared/LastUpdated";

// ── WS status dot ─────────────────────────────────────────────────────────────

function WsDot({ status }: { status: WsStatus }) {
  const map: Record<WsStatus, { color: string; label: string }> = {
    connected:    { color: "bg-green-400",  label: "Live" },
    connecting:   { color: "bg-yellow-400 animate-pulse", label: "Connecting…" },
    disconnected: { color: "bg-gray-500",   label: "Disconnected" },
    error:        { color: "bg-red-400",    label: "WS error" },
  };
  const { color, label } = map[status];
  return (
    <div className="flex items-center gap-1.5">
      <span className={`w-2 h-2 rounded-full ${color}`} />
      <span className="text-xs text-gray-400">{label}</span>
    </div>
  );
}

// ── Individual KPI tile ───────────────────────────────────────────────────────

interface TileProps {
  label:    string;
  value:    string | number;
  icon:     React.ReactNode;
  accent?:  boolean;
  warn?:    boolean;
  danger?:  boolean;
}

function Tile({ label, value, icon, accent, warn, danger }: TileProps) {
  const valueColor = danger
    ? "text-red-400"
    : warn
    ? "text-orange-400"
    : accent
    ? "text-ls-blue"
    : "text-white";

  return (
    <div className="flex items-center gap-3 px-5 py-3 border-r border-white/[0.06] last:border-r-0">
      <div className="text-gray-500">{icon}</div>
      <div>
        <p className="text-xs text-gray-500 leading-none mb-1">{label}</p>
        <p className={`text-lg font-semibold leading-none tabular-nums ${valueColor}`}>
          {value}
        </p>
      </div>
    </div>
  );
}

// ── Sim Clock ─────────────────────────────────────────────────────────────────

interface SimStatus {
  sim_time:   string;
  time_scale: number;
  running:    boolean;
}

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function SimClock() {
  const qc = useQueryClient();

  // Poll event-stream status every 5s
  const { data } = useQuery<SimStatus>({
    queryKey:        ["sim-status"],
    queryFn:         () => fetch("/aria/api/simulation").then((r) => r.json()),
    refetchInterval: 5_000,
    staleTime:       4_000,
  });

  // Locally advance sim_time between polls using the known time_scale
  const [displayTime, setDisplayTime] = useState<Date | null>(null);
  const lastFetchedAt  = useRef<number>(Date.now());
  const lastFetchedSim = useRef<Date | null>(null);

  useEffect(() => {
    if (!data?.sim_time) return;
    lastFetchedAt.current  = Date.now();
    lastFetchedSim.current = new Date(data.sim_time);
    setDisplayTime(new Date(data.sim_time));
  }, [data?.sim_time]);

  // Tick every real second — advance display by time_scale sim-seconds
  useEffect(() => {
    const scale = data?.time_scale ?? 1;
    const id = setInterval(() => {
      if (!lastFetchedSim.current) return;
      const realElapsed = (Date.now() - lastFetchedAt.current) / 1000;
      const simElapsed  = realElapsed * scale;
      setDisplayTime(new Date(lastFetchedSim.current.getTime() + simElapsed * 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [data?.time_scale]);

  // Inline time_scale editor
  const [editing, setEditing]   = useState(false);
  const [inputVal, setInputVal] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const setScale = useMutation({
    mutationFn: (val: number) =>
      fetch(`/aria/api/simulation?value=${val}`, { method: "POST" }).then((r) => r.json()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sim-status"] });
      setEditing(false);
    },
  });

  function openEditor() {
    setInputVal(String(data?.time_scale ?? 10));
    setEditing(true);
    setTimeout(() => inputRef.current?.select(), 0);
  }

  function commitEdit() {
    const n = parseFloat(inputVal);
    if (!isNaN(n) && n > 0 && n <= 500) setScale.mutate(n);
    else setEditing(false);
  }

  const scale    = data?.time_scale ?? "—";
  const dayName  = displayTime ? DAY_NAMES[displayTime.getDay()] : "—";
  const simHHMM  = displayTime
    ? displayTime.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false })
    : "--:--";

  return (
    <div className="flex items-center gap-3 px-5 border-r border-white/[0.06] h-full">
      <Clock className="w-4 h-4 text-gray-500 shrink-0" />
      <div>
        <p className="text-xs text-gray-500 leading-none mb-1">Sim Clock</p>
        <div className="flex items-baseline gap-1.5">
          <span className="text-lg font-semibold tabular-nums text-ls-blue leading-none">
            {dayName} {simHHMM}
          </span>
          {/* Clickable time_scale badge */}
          {editing ? (
            <input
              ref={inputRef}
              type="number"
              value={inputVal}
              onChange={(e) => setInputVal(e.target.value)}
              onBlur={commitEdit}
              onKeyDown={(e) => { if (e.key === "Enter") commitEdit(); if (e.key === "Escape") setEditing(false); }}
              className="w-12 text-[10px] bg-gray-800 border border-ls-blue rounded px-1 py-0.5 text-white outline-none tabular-nums"
              min={1} max={500} step={1}
            />
          ) : (
            <button
              onClick={openEditor}
              title="Click to change simulation speed"
              className="text-[10px] font-bold px-1.5 py-0.5 rounded tabular-nums transition-colors"
              style={{
                color: "#4280FF",
                background: "#4280FF22",
                border: "1px solid #4280FF44",
              }}
            >
              ×{scale}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Main KPI Strip ────────────────────────────────────────────────────────────

export function KpiStrip() {
  const router      = useRouter();
  const lastCycleAt = useUIStore((s) => s.lastCycleAt);
  const wsStatus    = useUIStore((s) => s.wsStatus);

  async function handleLogout() {
    await fetch("/aria/api/auth/logout", { method: "POST" });
    router.push("/login");
  }

  const { data, isError } = useQuery<KpiSummary>({
    queryKey:  QK.kpiSummary,
    queryFn:   () =>
      fetch("/aria/api/kpi-summary")
        .then((r) => r.json())
        .then((raw) => KpiSummarySchema.parse(raw)),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const d = data ?? {
    active_riders: "—", dead_zones: "—", at_risk_riders: "—",
    avg_eph: "—", high_risk_restaurants: "—", total_shortfall_inr: "—",
    severity: "normal",
  };

  return (
    <div
      className="flex items-center justify-between w-full h-14"
      style={{
        background: "linear-gradient(90deg, #1A1F2E 0%, #151922 100%)",
        borderBottom: "1px solid rgba(255,255,255,0.06)",
      }}
    >
      {/* Brand wordmark */}
      <div className="flex items-center gap-2 px-5 border-r border-white/[0.06] h-full">
        <span className="text-ls-blue font-bold text-lg tracking-tight">ARIA</span>
        <span className="text-gray-600 text-xs hidden sm:block">by Loadshare</span>
      </div>

      {/* Sim Clock */}
      <SimClock />

      {/* KPI tiles */}
      <div className="flex items-center flex-1 overflow-x-auto">
        <Tile
          label="Active Riders"
          value={isError ? "—" : Number(d.active_riders).toLocaleString()}
          icon={<Users className="w-4 h-4" />}
          accent
        />
        <Tile
          label="Dead Zones"
          value={isError ? "—" : Number(d.dead_zones)}
          icon={<AlertTriangle className="w-4 h-4" />}
          danger={Number(d.dead_zones) > 5}
          warn={Number(d.dead_zones) > 0 && Number(d.dead_zones) <= 5}
        />
        <Tile
          label="At-Risk Riders"
          value={isError ? "—" : Number(d.at_risk_riders)}
          icon={<Users className="w-4 h-4" />}
          warn={Number(d.at_risk_riders) > 0}
        />
        <Tile
          label="Avg EPH"
          value={isError ? "—" : `₹${Number(d.avg_eph).toFixed(0)}`}
          icon={<TrendingDown className="w-4 h-4" />}
          danger={Number(d.avg_eph) < 80}
          warn={Number(d.avg_eph) >= 80 && Number(d.avg_eph) < 95}
        />
        <Tile
          label="High-Risk Restaurants"
          value={isError ? "—" : Number(d.high_risk_restaurants)}
          icon={<Store className="w-4 h-4" />}
          warn={Number(d.high_risk_restaurants) > 0}
        />
        <Tile
          label="Earnings Shortfall"
          value={isError ? "—" : `₹${Number(d.total_shortfall_inr).toFixed(0)}`}
          icon={<TrendingDown className="w-4 h-4" />}
          danger={Number(d.total_shortfall_inr) > 5000}
        />
      </div>

      {/* Right: WS status + last updated + logout */}
      <div className="flex items-center gap-4 px-5 border-l border-white/[0.06] h-full">
        <WsDot status={wsStatus} />
        <LastUpdated timestamp={lastCycleAt} />
        <button
          onClick={handleLogout}
          title="Sign out"
          className="text-gray-600 hover:text-gray-400 transition-colors"
        >
          <LogOut className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
