"use client";

/**
 * OrdersBar — Live Order Pipeline Strip
 * =======================================
 * Sits between the KPI strip and main panels.
 * Shows the current order pipeline snapshot: active, queued, delivered/failed this cycle.
 * Polls every 30s + invalidated on WS cycle_complete.
 */

import { useQuery } from "@tanstack/react-query";
import { clientFetchTool } from "@/lib/api";
import { ShoppingBag, Clock, CheckCircle, XCircle, Bike, TrendingUp } from "lucide-react";

interface OrderSummary {
  active_orders:    number;
  pending_queue:    number;
  delivered_cycle:  number;
  failed_cycle:     number;
  failure_rate_pct: number;
  total_delivered:  number;
  total_failed:     number;
  total_orders:     number;
  avg_delivery_mins: number;
  active_riders:    number;
}

interface StatProps {
  icon:    React.ReactNode;
  label:   string;
  value:   string | number;
  sub?:    string;
  color?:  string;
}

function Stat({ icon, label, value, sub, color = "#9CA3AF" }: StatProps) {
  return (
    <div className="flex items-center gap-2 px-4 border-r border-white/[0.05] last:border-r-0">
      <span style={{ color }} className="shrink-0">{icon}</span>
      <div>
        <p className="text-[9px] uppercase tracking-wider text-gray-600 leading-none mb-0.5">{label}</p>
        <div className="flex items-baseline gap-1.5">
          <span className="text-sm font-semibold tabular-nums leading-none" style={{ color }}>
            {value}
          </span>
          {sub && <span className="text-[10px] text-gray-600 leading-none">{sub}</span>}
        </div>
      </div>
    </div>
  );
}

export function OrdersBar() {
  const { data } = useQuery<OrderSummary>({
    queryKey:        ["order-summary"],
    queryFn:         () => clientFetchTool<OrderSummary>("order-summary"),
    refetchInterval: 30_000,
    staleTime:       15_000,
  });

  const d = data ?? {
    active_orders: 0, pending_queue: 0, delivered_cycle: 0,
    failed_cycle: 0, failure_rate_pct: 0, total_delivered: 0,
    total_failed: 0, total_orders: 0, avg_delivery_mins: 0, active_riders: 0,
  };

  const failColor  = d.failure_rate_pct > 10 ? "#EF4444" : d.failure_rate_pct > 5 ? "#F97316" : "#22C55E";
  const queueColor = d.pending_queue > 10    ? "#F97316" : "#9CA3AF";
  const activeColor = d.active_orders > 0    ? "#4280FF" : "#9CA3AF";

  return (
    <div
      className="flex items-center w-full shrink-0"
      style={{
        height:       "36px",
        background:   "#111520",
        borderBottom: "1px solid rgba(255,255,255,0.04)",
      }}
    >
      {/* Section label */}
      <div className="flex items-center gap-1.5 px-4 border-r border-white/[0.05] h-full shrink-0">
        <ShoppingBag className="w-3 h-3 text-gray-600" />
        <span className="text-[9px] uppercase tracking-wider text-gray-600 font-semibold">Orders</span>
      </div>

      <div className="flex items-center flex-1 overflow-x-auto h-full">
        <Stat
          icon={<Bike className="w-3.5 h-3.5" />}
          label="In Flight"
          value={d.active_orders}
          sub="being delivered"
          color={activeColor}
        />
        <Stat
          icon={<Clock className="w-3.5 h-3.5" />}
          label="Queued"
          value={d.pending_queue}
          sub="awaiting rider"
          color={queueColor}
        />
        <Stat
          icon={<CheckCircle className="w-3.5 h-3.5" />}
          label="Delivered / 15min"
          value={d.delivered_cycle.toLocaleString()}
          color="#22C55E"
        />
        <Stat
          icon={<XCircle className="w-3.5 h-3.5" />}
          label="Failed / 15min"
          value={d.failed_cycle}
          sub={`${d.failure_rate_pct}% rate`}
          color={failColor}
        />
        <Stat
          icon={<TrendingUp className="w-3.5 h-3.5" />}
          label="Total Delivered"
          value={d.total_delivered.toLocaleString()}
          color="#6B7280"
        />
        <Stat
          icon={<ShoppingBag className="w-3.5 h-3.5" />}
          label="Total Orders"
          value={d.total_orders.toLocaleString()}
          color="#6B7280"
        />
      </div>
    </div>
  );
}
