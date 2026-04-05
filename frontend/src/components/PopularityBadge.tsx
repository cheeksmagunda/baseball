"use client";

interface PopularityBadgeProps {
  popularity: "FADE" | "TARGET" | "NEUTRAL";
}

const STYLES: Record<string, { bg: string; text: string; label: string }> = {
  FADE: { bg: "bg-red-500/15 border-red-500/25", text: "text-red-400", label: "FADE" },
  TARGET: { bg: "bg-emerald-500/15 border-emerald-500/25", text: "text-emerald-400", label: "TARGET" },
  NEUTRAL: { bg: "bg-white/5 border-white/10", text: "text-text-muted", label: "NEUTRAL" },
};

export function PopularityBadge({ popularity }: PopularityBadgeProps) {
  const style = STYLES[popularity] ?? STYLES.NEUTRAL;
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-fluid-xs font-bold ${style.bg} ${style.text}`}
    >
      {style.label}
    </span>
  );
}
