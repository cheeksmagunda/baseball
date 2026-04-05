import type { FilterOptimizeResponse } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export async function fetchLineups(signal?: AbortSignal): Promise<FilterOptimizeResponse> {
  const res = await fetch(`${API_URL}/api/filter-strategy/optimize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cards: [], games: [] }),
    signal,
    cache: "no-store",
  });

  if (!res.ok) {
    throw new ApiError(res.status, `API error: ${res.status}`);
  }

  return res.json();
}

export async function fetchSlates(signal?: AbortSignal) {
  const res = await fetch(`${API_URL}/api/slates`, { signal, cache: "no-store" });
  if (!res.ok) {
    throw new ApiError(res.status, `Slates error: ${res.status}`);
  }
  return res.json();
}

export async function checkHealth(signal?: AbortSignal): Promise<boolean> {
  try {
    const res = await fetch(`${API_URL}/api/health`, { signal, cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

export { ApiError };
