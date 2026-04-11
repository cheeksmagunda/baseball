"use client";

import { useState, useEffect, useCallback } from "react";
import type { WaitInfo } from "@/lib/types";

interface WaitStateProps {
  waitInfo: WaitInfo;
  onReady: () => void;
}

function formatCountdown(ms: number): string {
  if (ms <= 0) return "0s";
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}

function formatLocalTime(utcIso: string): string {
  const d = new Date(utcIso);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function WaitState({ waitInfo, onReady }: WaitStateProps) {
  const [remaining, setRemaining] = useState<number>(0);

  const tick = useCallback(() => {
    if (!waitInfo.lock_time_utc) return;
    const diff = Math.max(0, new Date(waitInfo.lock_time_utc).getTime() - Date.now());
    setRemaining(diff);
    if (diff <= 0) onReady();
  }, [waitInfo.lock_time_utc, onReady]);

  useEffect(() => {
    if (!waitInfo.lock_time_utc) return;
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [tick, waitInfo.lock_time_utc]);

  const isInitializing = waitInfo.phase === "initializing";

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center px-4 text-center">
      <div className="mb-4 flex h-20 w-20 items-center justify-center rounded-full bg-brand-primary/15">
        <svg
          className={`h-9 w-9 text-brand-primary${isInitializing ? " animate-spin" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <circle cx="12" cy="12" r="10" />
          <path d="M12 6v6l4 2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>

      {isInitializing ? (
        <>
          <h2 className="text-fluid-xl font-bold text-text-primary">
            Preparing Today&apos;s Picks
          </h2>
          <p className="mt-2 max-w-xs text-fluid-sm text-text-muted">
            The pipeline is starting up. This page will refresh automatically.
          </p>
          <div className="mt-6 h-1.5 w-40 overflow-hidden rounded-full bg-surface-elevated">
            <div className="h-full w-1/3 animate-pulse rounded-full bg-brand-primary" />
          </div>
        </>
      ) : (
        <>
          <h2 className="text-fluid-xl font-bold text-text-primary">
            Picks Available at{" "}
            <span className="text-brand-primary">
              {waitInfo.lock_time_utc ? formatLocalTime(waitInfo.lock_time_utc) : "--:--"}
            </span>
          </h2>

          <div className="mt-4 font-mono text-fluid-2xl font-black tabular-nums text-brand-primary">
            {formatCountdown(remaining)}
          </div>

          <p className="mt-3 max-w-xs text-fluid-sm text-text-muted">
            Picks generate 65 minutes before first pitch
            {waitInfo.first_pitch_utc && (
              <> at {formatLocalTime(waitInfo.first_pitch_utc)}</>
            )}
            . This page will refresh automatically.
          </p>
        </>
      )}
    </div>
  );
}
