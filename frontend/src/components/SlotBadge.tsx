"use client";

import { formatSlotMult } from "@/lib/formatters";

interface SlotBadgeProps {
  slotIndex: number;
  slotMult: number;
}

export function SlotBadge({ slotIndex, slotMult }: SlotBadgeProps) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="rounded-md bg-white/10 px-1.5 py-0.5 text-fluid-xs font-bold text-text-secondary">
        Slot {slotIndex}
      </span>
      <span className="font-stats text-fluid-xs font-semibold text-brand-accent">
        {formatSlotMult(slotMult)}
      </span>
    </div>
  );
}
