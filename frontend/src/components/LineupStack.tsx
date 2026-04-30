"use client";

import type { FilterLineupOut } from "@/lib/types";
import { PlayerCard } from "./PlayerCard";
import { NumberTicker } from "./NumberTicker";

interface LineupStackProps {
  lineup: FilterLineupOut;
}

export function LineupStack({ lineup }: LineupStackProps) {
  return (
    <div
      role="region"
      aria-label="Optimized lineup"
      className="mx-auto w-full max-w-md lg:max-w-none"
    >
      {/* Lineup header */}
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-fluid-lg font-bold text-text-primary">Lineup</h2>
          <p className="text-fluid-xs text-text-muted">{lineup.strategy}</p>
        </div>
        <div className="text-right">
          <p className="text-fluid-xs text-text-muted">Total EV</p>
          <NumberTicker
            value={lineup.total_expected_value}
            decimals={2}
            duration={1000}
            className="text-fluid-xl font-black text-brand-primary"
          />
        </div>
      </div>

      {/* Warnings */}
      {lineup.warnings.length > 0 && (
        <div className="mb-3 rounded-lg border border-brand-accent/30 bg-brand-accent/10 px-3 py-2">
          {lineup.warnings.map((w, i) => (
            <p key={i} className="text-fluid-xs text-brand-accent">
              {w}
            </p>
          ))}
        </div>
      )}

      {/* Card stack */}
      <div className="space-y-3 pb-8">
        {lineup.lineup.map((slot, i) => (
          <PlayerCard key={slot.slot_index} slot={slot} index={i} />
        ))}
      </div>
    </div>
  );
}
