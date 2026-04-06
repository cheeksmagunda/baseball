import { ClientHome } from "./ClientHome";
import type { FilterOptimizeResponse } from "@/lib/types";

async function getInitialLineups(): Promise<FilterOptimizeResponse | null> {
  try {
    const backendUrl = process.env.API_URL ?? process.env.BACKEND_URL ?? "http://localhost:8000";
    const res = await fetch(`${backendUrl}/api/filter-strategy/optimize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cards: [], games: [] }),
      cache: "no-store",
    });

    if (!res.ok) return null;
    return await res.json();
  } catch (error) {
    console.error("Failed to fetch initial lineups on server:", error);
    return null;
  }
}

export default async function Home() {
  const initialData = await getInitialLineups();
  return <ClientHome initialData={initialData} />;
}
