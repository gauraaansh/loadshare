"use client";

/**
 * Zone Map Panel
 * ===============
 * Two-layer fetch strategy (accepted suggestion #6):
 *   - Zone geometry (/api/zone-map?type=geometry):  long TTL (1hr), rarely changes
 *   - Zone stress   (/api/zone-map?type=stress):     invalidated on cycle_complete
 *
 * Merged client-side before rendering GeoJSON polygons.
 * Uses react-leaflet with dark tile layer.
 * Color-coded by stress_level using zone.* Tailwind tokens.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Map as MapIcon } from "lucide-react";
import { clientFetchTool, QK } from "@/lib/api";
import { ZoneMapSchema, type ZoneFeature } from "@/lib/schemas";
import { useUIStore } from "@/store/uiStore";
import { PanelError } from "@/components/shared/PanelError";
import { LastUpdated } from "@/components/shared/LastUpdated";

// ── Stress-level colors ───────────────────────────────────────────────────────

const STRESS_COLOR: Record<string, string> = {
  dead:     "#EF4444",
  low:      "#F97316",
  stressed: "#EAB308",
  normal:   "#22C55E",
  stale:    "#6B7280",
  unknown:  "#374151",
};

// ── Legend ────────────────────────────────────────────────────────────────────

function Legend() {
  const items = [
    { label: "Dead",     color: STRESS_COLOR.dead },
    { label: "Low",      color: STRESS_COLOR.low },
    { label: "Stressed", color: STRESS_COLOR.stressed },
    { label: "Normal",   color: STRESS_COLOR.normal },
    { label: "Stale",    color: STRESS_COLOR.stale },
  ];
  return (
    <div className="absolute bottom-4 right-4 z-[1000] panel px-3 py-2 flex flex-col gap-1.5">
      {items.map(({ label, color }) => (
        <div key={label} className="flex items-center gap-2">
          <div className="w-3 h-3 rounded-sm" style={{ background: color, opacity: 0.8 }} />
          <span className="text-[10px] text-gray-400">{label}</span>
        </div>
      ))}
    </div>
  );
}

// ── Main map component (client-only, no SSR) ──────────────────────────────────

import dynamic from "next/dynamic";

// Leaflet must be dynamically imported to avoid SSR issues
const LeafletMap = dynamic(() => import("./LeafletMap"), { ssr: false });

export function MapPanel() {
  const lastCycleId  = useUIStore((s) => s.lastCycleId);
  const lastCycleAt  = useUIStore((s) => s.lastCycleAt);

  // Static geometry — long cache, only refetch on explicit invalidation
  const {
    data: geometryData,
    isError: geoError,
    refetch: refetchGeo,
  } = useQuery({
    queryKey: QK.zoneGeometry,
    queryFn:  () =>
      clientFetchTool<unknown>("zone-map", { type: "geometry" })
        .then((r) => ZoneMapSchema.parse(r)),
    staleTime: 60 * 60 * 1_000,   // 1 hour
    gcTime:    2 * 60 * 60 * 1_000,
  });

  // Dynamic stress — refreshes on cycle_complete (via TanStack Query invalidation)
  const { data: stressData } = useQuery({
    queryKey: QK.zoneStress(lastCycleId ?? "latest"),
    queryFn:  () =>
      clientFetchTool<unknown>("zone-map", { type: "stress" })
        .then((r) => ZoneMapSchema.parse(r)),
    staleTime: 15 * 60 * 1_000,   // 15 min (one cycle)
  });

  // Merge: geometry + latest stress level
  const zones: ZoneFeature[] = useMemo(() => {
    if (!geometryData) return [];
    const stressMap = new Map(
      (stressData?.zones ?? []).map((z) => [z.zone_id, z])
    );
    return geometryData.zones.map((z) => ({
      ...z,
      stress_level: stressMap.get(z.zone_id)?.stress_level ?? z.stress_level,
      stress_ratio: stressMap.get(z.zone_id)?.stress_ratio ?? z.stress_ratio,
      rider_count:  stressMap.get(z.zone_id)?.rider_count  ?? z.rider_count,
    }));
  }, [geometryData, stressData]);

  if (geoError) {
    return (
      <div className="panel h-full">
        <PanelError
          title="Zone map unavailable"
          message="Could not load zone geometry."
          onRetry={refetchGeo}
        />
      </div>
    );
  }

  return (
    <div className="panel h-full flex flex-col overflow-hidden relative">
      {/* Header */}
      <div className="flex items-center justify-between px-4 pt-3 pb-2 border-b border-white/[0.06] z-10">
        <div className="flex items-center gap-2">
          <MapIcon className="w-4 h-4 text-ls-blue" />
          <h2 className="text-sm font-semibold text-white">Zone Map</h2>
          {zones.length > 0 && (
            <span className="text-xs text-gray-500">({zones.length} zones)</span>
          )}
        </div>
        <LastUpdated timestamp={lastCycleAt} />
      </div>

      {/* Map */}
      <div className="flex-1 relative">
        <LeafletMap zones={zones} />
        <Legend />
      </div>
    </div>
  );
}
