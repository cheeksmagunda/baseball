import type { FilterOptimizeResponse } from "./types";

class ApiError extends Error {
  public body: Record<string, unknown> | null;
  constructor(public status: number, message: string, body: Record<string, unknown> | null = null) {
    super(message);
    this.name = "ApiError";
    this.body = body;
  }
}

export async function fetchLineups(signal?: AbortSignal): Promise<FilterOptimizeResponse> {
  const res = await fetch(`/api/filter-strategy/optimize`, {
    method: "GET",
    signal,
    cache: "no-store",
  });

  if (!res.ok) {
    let message = `API error: ${res.status}`;
    let body: Record<string, unknown> | null = null;
    try {
      body = await res.json();
      message = (body?.detail as string) ?? (body?.message as string) ?? message;
    } catch {}
    throw new ApiError(res.status, message, body);
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
