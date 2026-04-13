/**
 * ARIA Dashboard — Main Page
 * ============================
 * Layout (scrollable):
 *   [KPI Strip — fixed top]
 *   [Zone Map 60% | Intelligence Pipeline 40%]  ← fills viewport height
 *   [Rider Interventions 60% | Restaurant Risk 40%]  ← scroll down to see
 */

import { cookies }        from "next/headers";
import { redirect }        from "next/navigation";
import { KpiStrip }       from "@/components/kpi/KpiStrip";
import { OrdersBar }       from "@/components/kpi/OrdersBar";
import { MapPanel }        from "@/components/map/MapPanel";
import { AgentPipeline }   from "@/components/flow/AgentPipeline";
import { RiderTable }      from "@/components/tables/RiderTable";
import { RestaurantTable } from "@/components/tables/RestaurantTable";
import { DashboardShell }  from "@/components/shared/DashboardShell";
import { OfflinePage }     from "@/components/shared/OfflinePage";
import { isMcpReachable }  from "@/lib/serverHealth";
import { verifySessionToken, COOKIE_NAME } from "@/lib/session";

export default async function Dashboard() {
  const cookieStore = await cookies();
  const token = cookieStore.get(COOKIE_NAME)?.value;
  const valid = token ? await verifySessionToken(token) : false;
  if (!valid) redirect("/login");

  const online = await isMcpReachable();
  if (!online) return <OfflinePage />;
  return (
    <DashboardShell>
      <div className="flex flex-col h-screen overflow-hidden">

        {/* ── KPI Strip — fixed, never scrolls ── */}
        <KpiStrip />

        {/* ── Orders pipeline bar — fixed, never scrolls ── */}
        <OrdersBar />

        {/* ── Scrollable content area ── */}
        <div className="flex-1 min-h-0 overflow-y-auto">
          <div className="p-3 space-y-3">

            {/* Top section: Zone Map + Intelligence Pipeline
                Height = viewport minus KPI strip (~56px) and padding/gap (~24px) */}
            <div
              className="grid gap-3"
              style={{
                gridTemplateColumns: "60% 1fr",
                height: "calc(100svh - 136px)",
              }}
            >
              <MapPanel />
              <AgentPipeline />
            </div>

            {/* Bottom section: tables — scroll down to reach */}
            <div
              className="grid gap-3"
              style={{
                gridTemplateColumns: "60% 1fr",
                height: "300px",
              }}
            >
              <RiderTable />
              <RestaurantTable />
            </div>

          </div>
        </div>
      </div>
    </DashboardShell>
  );
}
