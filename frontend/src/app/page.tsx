import { ClientHome } from "./ClientHome";
import type { FilterOptimizeResponse } from "@/lib/types";

async function getInitialLineups(): Promise<FilterOptimizeResponse | null> {
  try {
    const backendUrl = process.env.API_URL ?? process.env.BACKEND_URL ?? "http://localhost:8000";
    console.log(`[SSR] Fetching initial lineups from: ${backendUrl}`);

    // Use AbortController to add timeout
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000); // 5 second timeout

    try {
      // First check if backend is healthy
      console.log(`[SSR] Checking backend health at: ${backendUrl}/api/health`);
      const healthRes = await fetch(`${backendUrl}/api/health`, {
        signal: controller.signal,
        cache: "no-store",
      });

      if (!healthRes.ok) {
        console.warn(`[SSR] Backend health check failed with status: ${healthRes.status}`);
        return null;
      }

      // If healthy, fetch lineups
      const res = await fetch(`${backendUrl}/api/filter-strategy/optimize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cards: [], games: [] }),
        signal: controller.signal,
        cache: "no-store",
      });

      if (!res.ok) {
        console.warn(`[SSR] Optimize endpoint returned status: ${res.status}`);
        return null;
      }
      console.log("[SSR] Successfully fetched initial lineups");
      return await res.json();
    } finally {
      clearTimeout(timeoutId);
    }
  } catch (error) {
    console.error("[SSR] Failed to fetch initial lineups on server:", error);
    return null;
  }
}

export default async function Home() {
  const initialData = await getInitialLineups();
  return <ClientHome initialData={initialData} />;
}
