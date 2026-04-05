"use client";

import type { SlateClassificationOut } from "@/lib/types";

interface StickyHeaderProps {
  slate?: SlateClassificationOut | null;
}

export function StickyHeader({ slate }: StickyHeaderProps) {
  const today = new Date().toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
  });

  return (
    <header className="glass-strong sticky top-0 z-50 px-4 py-3">
      <div className="mx-auto flex max-w-md items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-primary/20">
            <span className="text-fluid-lg font-bold text-brand-primary">D</span>
          </div>
          <div>
            <h1 className="text-fluid-sm font-bold tracking-tight text-text-primary">
              DFS Predictor
            </h1>
            <p className="text-fluid-xs text-text-muted">{today}</p>
          </div>
        </div>

        {slate && (
          <div className="flex items-center gap-2">
            <span className="rounded-full bg-surface-elevated px-2.5 py-0.5 text-fluid-xs font-medium text-text-secondary">
              {slate.game_count} {slate.game_count === 1 ? "game" : "games"}
            </span>
            <span className="rounded-full bg-brand-primary/15 px-2.5 py-0.5 text-fluid-xs font-semibold capitalize text-brand-primary">
              {slate.slate_type.replace("_", " ")}
            </span>
          </div>
        )}
      </div>
    </header>
  );
}
