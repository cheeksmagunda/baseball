import type { FilterOptimizeResponse } from "./types";

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export async function fetchLineups(signal?: AbortSignal): Promise<FilterOptimizeResponse> {
  const res = await fetch(`/api/filter-strategy/optimize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cards: [], games: [] }),
    signal,
    cache: "no-store",
  });

  if (!res.ok) {
    let message = `API error: ${res.status}`;
    try {
      const body = await res.json();
      message = body.detail ?? body.message ?? message;
    } catch {}
    throw new ApiError(res.status, message);
  }

  return res.json();
}

export async function fetchSlates(signal?: AbortSignal) {
  const res = await fetch(`/api/slates`, { signal, cache: "no-store" });
  if (!res.ok) {
    throw new ApiError(res.status, `Slates error: ${res.status}`);
  }
  return res.json();
}

export async function checkHealth(signal?: AbortSignal): Promise<boolean> {
  try {
    const res = await fetch(`/api/health`, { signal, cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

export { ApiError };
