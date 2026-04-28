"use client";

import { useEffect, useRef, useCallback } from "react";
import { fetchSlates } from "@/lib/api";

const POLL_INTERVAL = 60_000; // 60 seconds base
const POLL_JITTER = 10_000;   // ±10 s jitter to spread T-65 thundering herd
const STALE_THRESHOLD = 5 * 60_000; // 5 minutes

function jitteredInterval() {
  return POLL_INTERVAL + (Math.random() * 2 - 1) * POLL_JITTER;
}

export function useSlatePolling(onSlateChange: () => void) {
  const lastSlateRef = useRef<string | null>(null);
  const lastVisibleRef = useRef(Date.now());

  const poll = useCallback(async () => {
    try {
      const slates = await fetchSlates();
      if (!Array.isArray(slates) || slates.length === 0) return;

      const latest = slates[0];
      const key = `${latest.date}-${latest.status}`;

      if (lastSlateRef.current !== null && lastSlateRef.current !== key) {
        onSlateChange();
      }
      lastSlateRef.current = key;
    } catch {
      // Silently ignore polling errors
    }
  }, [onSlateChange]);

  // Polling with per-tick jitter to avoid thundering herd at T-65
  useEffect(() => {
    let id: ReturnType<typeof setTimeout>;
    function schedule() {
      id = setTimeout(() => {
        poll();
        schedule();
      }, jitteredInterval());
    }
    schedule();
    return () => clearTimeout(id);
  }, [poll]);

  // Page Visibility API: refetch if stale on return
  useEffect(() => {
    function handleVisibility() {
      if (document.visibilityState === "visible") {
        const stale = Date.now() - lastVisibleRef.current > STALE_THRESHOLD;
        if (stale) {
          onSlateChange();
        }
      } else {
        lastVisibleRef.current = Date.now();
      }
    }

    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, [onSlateChange]);
}
