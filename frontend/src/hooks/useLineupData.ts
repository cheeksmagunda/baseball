"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import type { FilterOptimizeResponse } from "@/lib/types";
import { fetchLineups, ApiError } from "@/lib/api";

interface UseLineupDataReturn {
  data: FilterOptimizeResponse | null;
  loading: boolean;
  error: { status: number; message: string } | null;
  refetch: () => void;
}

export function useLineupData(): UseLineupDataReturn {
  const [data, setData] = useState<FilterOptimizeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setError(null);

    try {
      const result = await fetchLineups(controller.signal);
      if (!controller.signal.aborted) {
        setData(result);
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (err instanceof ApiError) {
        if (err.status === 404) {
          setError({ status: 404, message: "No slate available for today. Check back later." });
        } else {
          setError({ status: err.status, message: err.message });
        }
      } else {
        setError({ status: 0, message: "Network error. Please try again." });
      }
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    load();
    return () => abortRef.current?.abort();
  }, [load]);

  return { data, loading, error, refetch: load };
}
