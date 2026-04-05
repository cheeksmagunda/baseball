"use client";

import { formatBoost } from "@/lib/formatters";

interface BoostIndicatorProps {
  boost: number;
}

export function BoostIndicator({ boost }: BoostIndicatorProps) {
  const intensity =
    boost >= 2.5
      ? "bg-brand-success/20 text-brand-success border-brand-success/30"
      : boost >= 1.5
        ? "bg-brand-accent/20 text-brand-accent border-brand-accent/30"
        : boost > 0
          ? "bg-brand-primary/20 text-brand-primary border-brand-primary/30"
          : "bg-white/5 text-text-muted border-white/10";

  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 font-stats text-fluid-xs font-bold ${intensity}`}
    >
      {formatBoost(boost)}
    </span>
  );
}
