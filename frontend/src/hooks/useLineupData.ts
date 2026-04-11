"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import type { FilterOptimizeResponse, WaitInfo } from "@/lib/types";
import { fetchLineups, ApiError } from "@/lib/api";

interface UseLineupDataReturn {
  data: FilterOptimizeResponse | null;
  loading: boolean;
  error: { status: number; message: string } | null;
  waitInfo: WaitInfo | null;
  refetch: () => void;
}

export function useLineupData(initialData: FilterOptimizeResponse | null = null): UseLineupDataReturn {
  const [data, setData] = useState<FilterOptimizeResponse | null>(initialData);
  const [loading, setLoading] = useState(!initialData);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);
  const [waitInfo, setWaitInfo] = useState<WaitInfo | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const autoRefetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async (isBackgroundRefresh = false) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    if (!isBackgroundRefresh) setLoading(true);
    setError(null);
    setWaitInfo(null);

    // Clear any pending auto-refetch
    if (autoRefetchTimer.current) {
      clearTimeout(autoRefetchTimer.current);
      autoRefetchTimer.current = null;
    }

    try {
      const result = await fetchLineups(controller.signal);
      if (!controller.signal.aborted) {
        setData(result);
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (err instanceof ApiError) {
        if (err.status === 425 && err.body) {
          // Wait state — parse timing info
          const info: WaitInfo = {
            phase: (err.body.phase as WaitInfo["phase"]) ?? "initializing",
            first_pitch_utc: (err.body.first_pitch_utc as string) ?? null,
            lock_time_utc: (err.body.lock_time_utc as string) ?? null,
            minutes_until_lock: (err.body.minutes_until_lock as number) ?? null,
          };
          if (!controller.signal.aborted) {
            setWaitInfo(info);

            // Auto-refetch when lock time arrives (+ 10s buffer for pipeline)
            if (info.lock_time_utc) {
              const lockMs = new Date(info.lock_time_utc).getTime();
              const delayMs = Math.max(0, lockMs - Date.now()) + 10_000;
              autoRefetchTimer.current = setTimeout(() => {
                load(false);
              }, delayMs);
            } else {
              // Initializing phase — retry in 15 seconds
              autoRefetchTimer.current = setTimeout(() => {
                load(false);
              }, 15_000);
            }
          }
        } else if (err.status === 404) {
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
    if (!initialData) {
      load(false);
    }
    return () => {
      abortRef.current?.abort();
      if (autoRefetchTimer.current) {
        clearTimeout(autoRefetchTimer.current);
      }
    };
  }, [load, initialData]);

  const refetch = useCallback(() => load(true), [load]);

  return { data, loading, error, waitInfo, refetch };
}
