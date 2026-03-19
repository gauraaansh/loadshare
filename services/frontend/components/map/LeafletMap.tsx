"use client";

/**
 * LeafletMap — dynamically imported (no SSR)
 * ===========================================
 * Renders react-leaflet MapContainer with GeoJSON zone polygons.
 * Each polygon is colored by stress_level.
 * Click → tooltip with zone name, city, stress, rider count.
 */

import "leaflet/dist/leaflet.css";
import { MapContainer, TileLayer, GeoJSON, Tooltip } from "react-leaflet";
import L from "leaflet";
import type { ZoneFeature } from "@/lib/schemas";

// Fix Leaflet default icon paths (webpack issue)
// @ts-expect-error — Leaflet internal, no type for _getIconUrl
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
  iconUrl:       "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
  shadowUrl:     "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
});

const STRESS_COLOR: Record<string, string> = {
  dead:     "#EF4444",
  low:      "#F97316",
  stressed: "#EAB308",
  normal:   "#22C55E",
  stale:    "#6B7280",
  unknown:  "#374151",
};

interface Props {
  zones: ZoneFeature[];
}

// Center of India — shows all 12 cities
const INDIA_CENTER: [number, number] = [20.5937, 78.9629];

export default function LeafletMap({ zones }: Props) {
  return (
    <MapContainer
      center={INDIA_CENTER}
      zoom={5}
      style={{ height: "100%", width: "100%" }}
      scrollWheelZoom={true}
    >
      {/* Dark tile layer */}
      <TileLayer
        url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>'
        subdomains="abcd"
        maxZoom={19}
      />

      {/* Zone polygons */}
      {zones.map((zone) => {
        const color = STRESS_COLOR[zone.stress_level] ?? STRESS_COLOR.unknown;

        return (
          <GeoJSON
            key={zone.zone_id}
            data={zone.geometry as unknown as GeoJSON.GeoJsonObject}
            style={{
              color,
              weight:      1.5,
              opacity:     0.9,
              fillColor:   color,
              fillOpacity: zone.stress_level === "normal" ? 0.12 : 0.3,
            }}
          >
            <Tooltip sticky>
              <div style={{ fontFamily: "Inter, sans-serif", fontSize: 12, lineHeight: 1.6 }}>
                <strong style={{ color }}>{zone.name}</strong>
                <br />
                {zone.city} · {zone.zone_type}
                <br />
                Status: <strong style={{ color }}>{zone.stress_level}</strong>
                {zone.rider_count != null && (
                  <><br />Riders: {zone.rider_count}</>
                )}
                {zone.stress_ratio != null && (
                  <><br />Stress ratio: {zone.stress_ratio.toFixed(2)}</>
                )}
              </div>
            </Tooltip>
          </GeoJSON>
        );
      })}
    </MapContainer>
  );
}
