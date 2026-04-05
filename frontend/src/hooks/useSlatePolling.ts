"use client";

import { useEffect, useRef, useCallback } from "react";
import { fetchSlates } from "@/lib/api";

const POLL_INTERVAL = 60_000; // 60 seconds
const STALE_THRESHOLD = 5 * 60_000; // 5 minutes

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

  // Regular polling interval
  useEffect(() => {
    const id = setInterval(poll, POLL_INTERVAL);
    return () => clearInterval(id);
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
