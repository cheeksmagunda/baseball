"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import type { LivePlayerStats } from "@/lib/types";
import { fetchLiveStats, ApiError } from "@/lib/api";

const POLL_INTERVAL_MS = 60_000;

/**
 * Polls /api/filter-strategy/live-stats every 60 seconds when picks are ready.
 * Returns a Map from player_name → LivePlayerStats for O(1) lookup in PlayerCard.
 * Stops polling when `active` is false (before picks are ready or after slate ends).
 */
export function useLiveStats(active: boolean): Map<string, LivePlayerStats> {
  const [statsMap, setStatsMap] = useState<Map<string, LivePlayerStats>>(new Map());
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const poll = useCallback(async () => {
    if (!active) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const result = await fetchLiveStats(controller.signal);
      if (!controller.signal.aborted) {
        const map = new Map<string, LivePlayerStats>();
        for (const p of result.players) {
          map.set(p.player_name, p);
        }
        setStatsMap(map);
      }
    } catch (err) {
      // 425 = picks not ready yet; silently ignore. Other errors also silently
      // ignored — live stats are best-effort and must never break the picks view.
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (err instanceof ApiError && err.status === 425) return;
    }

    if (active) {
      timerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
    }
  }, [active]);

  useEffect(() => {
    if (!active) {
      setStatsMap(new Map());
      return;
    }
    void poll();
    return () => {
      abortRef.current?.abort();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [active, poll]);

  return statsMap;
}
