import { ClientHome } from "./ClientHome";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import type { FilterOptimizeResponse, OptimizeStatus } from "@/lib/types";

interface InitialPayload {
  initialData: FilterOptimizeResponse | null;
  initialStatus: OptimizeStatus | null;
}

async function getInitialPayload(): Promise<InitialPayload> {
  const backendUrl = process.env.API_URL ?? process.env.BACKEND_URL ?? "http://localhost:8000";
  console.log(`[SSR] Fetching status from: ${backendUrl}`);

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);

  try {
    // Step 1: ask the backend whether picks are ready. /status is a cheap
    // in-memory check — no DB or external API calls.
    const statusRes = await fetch(`${backendUrl}/api/filter-strategy/status`, {
      signal: controller.signal,
      cache: "no-store",
    });
    if (!statusRes.ok) {
      console.warn(`[SSR] /status returned ${statusRes.status}`);
      return { initialData: null, initialStatus: null };
    }
    const initialStatus: OptimizeStatus = await statusRes.json();
    console.log(`[SSR] /status phase=${initialStatus.phase} ready=${initialStatus.ready}`);

    // Step 2: only fetch picks when the backend has signalled they're ready.
    // The frontend renders a locked countdown UI until then; never returns
    // a partial/empty picks state that the user could mistake for real picks.
    if (!initialStatus.ready) {
      return { initialData: null, initialStatus };
    }

    const optimizeRes = await fetch(`${backendUrl}/api/filter-strategy/optimize`, {
      signal: controller.signal,
      cache: "no-store",
    });
    if (!optimizeRes.ok) {
      console.warn(`[SSR] /optimize returned ${optimizeRes.status} despite ready=true`);
      return { initialData: null, initialStatus };
    }
    const initialData: FilterOptimizeResponse = await optimizeRes.json();
    console.log("[SSR] Successfully fetched initial lineups");
    return { initialData, initialStatus };
  } catch (error) {
    console.error("[SSR] Failed to fetch initial state:", error);
    return { initialData: null, initialStatus: null };
  } finally {
    clearTimeout(timeoutId);
  }
}

export default async function Home() {
  const { initialData, initialStatus } = await getInitialPayload();
  return (
    <ErrorBoundary>
      <ClientHome initialData={initialData} initialStatus={initialStatus} />
    </ErrorBoundary>
  );
}
