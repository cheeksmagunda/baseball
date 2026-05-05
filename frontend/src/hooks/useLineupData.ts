"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import type { FilterOptimizeResponse, OptimizeStatus, WaitInfo } from "@/lib/types";
import { fetchLineups, fetchStatus, ApiError } from "@/lib/api";

interface UseLineupDataReturn {
  data: FilterOptimizeResponse | null;
  loading: boolean;
  error: { status: number; message: string } | null;
  waitInfo: WaitInfo | null;
  refetch: () => void;
}

const STATUS_POLL_INTERVAL_MS = 5_000;

function statusToWaitInfo(status: OptimizeStatus): WaitInfo {
  // Map backend phases onto the frontend's WaitInfo shape. The "no_slate"
  // backend phase shares the "initializing" UI state. The "ready" and
  // "failed" phases never reach this function — callers gate on status.ready
  // and status.phase first — but we map them defensively to "generating" to
  // satisfy the type checker.
  let phase: WaitInfo["phase"];
  if (status.phase === "before_lock" || status.phase === "generating") {
    phase = status.phase;
  } else {
    phase = "initializing";
  }
  return {
    phase,
    first_pitch_utc: status.first_pitch_utc,
    lock_time_utc: status.lock_time_utc,
    minutes_until_lock: status.minutes_until_lock,
  };
}

function statusToError(status: OptimizeStatus): { status: number; message: string } {
  // /optimize returns 503 when the cache is marked failed — mirror that here
  // so the user sees a consistent error code regardless of which endpoint
  // surfaced the failure.
  return {
    status: 503,
    message:
      status.error ??
      "T-65 pipeline failed — picks unavailable. Please retry shortly.",
  };
}

export function useLineupData(
  initialData: FilterOptimizeResponse | null = null,
  initialStatus: OptimizeStatus | null = null,
): UseLineupDataReturn {
  const [data, setData] = useState<FilterOptimizeResponse | null>(initialData);
  const [loading, setLoading] = useState(!initialData && !initialStatus);
  const [error, setError] = useState<{ status: number; message: string } | null>(
    initialStatus && initialStatus.phase === "failed" ? statusToError(initialStatus) : null,
  );
  const [waitInfo, setWaitInfo] = useState<WaitInfo | null>(
    initialStatus && !initialStatus.ready && initialStatus.phase !== "failed"
      ? statusToWaitInfo(initialStatus)
      : null,
  );

  const abortRef = useRef<AbortController | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  // Single-shot picks fetch — only called once /status reports ready: true.
  const fetchPicks = useCallback(async (signal: AbortSignal) => {
    try {
      const result = await fetchLineups(signal);
      if (!signal.aborted) {
        setData(result);
        setWaitInfo(null);
        setError(null);
        setLoading(false);
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (err instanceof ApiError) {
        setError({ status: err.status, message: err.message });
      } else {
        setError({ status: 0, message: "Network error. Please try again." });
      }
      setLoading(false);
    }
  }, []);

  // Poll /status until ready — cheap, in-memory backend check, no compute.
  // Once ready, fetch /optimize exactly once.
  const pollStatus = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const status = await fetchStatus(controller.signal);
      if (controller.signal.aborted) return;

      if (status.ready) {
        clearTimer();
        await fetchPicks(controller.signal);
        return;
      }

      // Pipeline crashed — surface the error and stop polling. A redeploy
      // will bust the deploy_id'd cache and re-run the pipeline on the
      // remaining games; the user can hit refresh to pick up new picks.
      if (status.phase === "failed") {
        clearTimer();
        setWaitInfo(null);
        setError(statusToError(status));
        setLoading(false);
        return;
      }

      // Not ready — render locked UI and schedule next poll.
      setWaitInfo(statusToWaitInfo(status));
      setLoading(false);
      setError(null);
      pollTimerRef.current = setTimeout(pollStatus, STATUS_POLL_INTERVAL_MS);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (err instanceof ApiError) {
        setError({ status: err.status, message: err.message });
      } else {
        setError({ status: 0, message: "Network error. Please try again." });
      }
      setLoading(false);
      // Retry on transient errors.
      pollTimerRef.current = setTimeout(pollStatus, STATUS_POLL_INTERVAL_MS);
    }
  }, [clearTimer, fetchPicks]);

  // refetch: force re-check status (e.g. user-triggered or slate change). If
  // already showing picks, this re-fetches /optimize directly.
  const refetch = useCallback(() => {
    clearTimer();
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    if (data) {
      void fetchPicks(controller.signal);
    } else {
      void pollStatus();
    }
  }, [clearTimer, data, fetchPicks, pollStatus]);

  useEffect(() => {
    // SSR pre-loaded picks → nothing to do.
    if (initialData) return;
    // SSR pre-loaded status (not ready) → start polling immediately.
    void pollStatus();

    return () => {
      abortRef.current?.abort();
      clearTimer();
    };
    // pollStatus is stable (only depends on stable refs); intentionally
    // exclude from deps so this only fires on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialData]);

  return { data, loading, error, waitInfo, refetch };
}
