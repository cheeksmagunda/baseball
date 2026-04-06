import { ClientHome } from "./ClientHome";
import type { FilterOptimizeResponse } from "@/lib/types";

async function getInitialLineups(): Promise<FilterOptimizeResponse | null> {
  try {
    const backendUrl = process.env.API_URL ?? process.env.BACKEND_URL ?? "http://localhost:8000";

    // Use AbortController to add timeout
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000); // 5 second timeout

    try {
      // First check if backend is healthy
      const healthRes = await fetch(`${backendUrl}/api/health`, {
        signal: controller.signal,
        cache: "no-store",
      });

      if (!healthRes.ok) return null;

      // If healthy, fetch lineups
      const res = await fetch(`${backendUrl}/api/filter-strategy/optimize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cards: [], games: [] }),
        signal: controller.signal,
        cache: "no-store",
      });

      if (!res.ok) return null;
      return await res.json();
    } finally {
      clearTimeout(timeoutId);
    }
  } catch (error) {
    console.error("Failed to fetch initial lineups on server:", error);
    return null;
  }
}

export default async function Home() {
  const initialData = await getInitialLineups();
  return <ClientHome initialData={initialData} />;
}
