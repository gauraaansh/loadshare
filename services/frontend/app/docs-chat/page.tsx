import { isMcpReachable }  from "@/lib/serverHealth";
import { OfflinePage }     from "@/components/shared/OfflinePage";
import DocsChatClient      from "@/components/shared/DocsChatClient";

export default async function DocsChatPage() {
  const online = await isMcpReachable();
  if (!online) return <OfflinePage />;
  return <DocsChatClient />;
}
