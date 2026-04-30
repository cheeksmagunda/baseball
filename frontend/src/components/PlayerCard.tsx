"use client";

import React from "react";
import { motion } from "framer-motion";
import type { FilterSlotOut } from "@/lib/types";
import { useTeamColors } from "@/hooks/useTeamColors";
import { useReducedMotion } from "@/hooks/useReducedMotion";
import { SlotBadge } from "./SlotBadge";
import { EnvFactorChip } from "./EnvFactorChip";
import { NumberTicker } from "./NumberTicker";
import { TraitBreakdown } from "./TraitBreakdown";
import { formatScore, formatEV } from "@/lib/formatters";

function StatBlock({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-0.5">
      <p className="text-fluid-xs text-text-muted">{label}</p>
      {children}
    </div>
  );
}

interface PlayerCardProps {
  slot: FilterSlotOut;
  index: number;
}

export function PlayerCard({ slot, index }: PlayerCardProps) {
  const { primary, glowShadow, gradientBg, borderColor } = useTeamColors(slot.team);
  const reduced = useReducedMotion();

  return (
    <motion.article
      initial={reduced ? {} : { opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: index * 0.08 }}
      whileHover={reduced ? {} : { scale: 1.02 }}
      whileTap={reduced ? {} : { scale: 0.98 }}
      onClick={() => navigator.vibrate?.(10)}
      className="relative overflow-hidden rounded-xl border transition-shadow"
      style={{
        background: gradientBg,
        borderColor,
        boxShadow: glowShadow,
      }}
    >
      {/* Accent stripe */}
      <div
        className="absolute left-0 top-0 h-full w-1"
        style={{ backgroundColor: primary }}
      />

      <div className="relative p-4 pl-5">
        {/* Top row: slot badge */}
        <div className="flex items-center gap-2">
          <SlotBadge slotIndex={slot.slot_index} slotMult={slot.slot_mult} />
        </div>

        {/* Player info */}
        <div className="mt-2 flex flex-wrap items-baseline gap-2">
          <h3 className="text-fluid-lg font-bold text-text-primary">{slot.player_name}</h3>
          <span
            className="rounded-md px-1.5 py-0.5 text-fluid-xs font-bold"
            style={{ backgroundColor: `${primary}25`, color: primary }}
          >
            {slot.team}
          </span>
          <span className="text-fluid-xs text-text-muted">{slot.position}</span>
          {slot.is_two_way_pitcher && (
            <span className="rounded-md bg-brand-accent/15 px-1.5 py-0.5 text-fluid-xs font-bold text-brand-accent">
              2-WAY SP
            </span>
          )}
        </div>

        {/* Score row */}
        <div className="mt-3 grid grid-cols-3 gap-3">
          <StatBlock label="Rating">
            <p className="font-stats text-fluid-base font-bold text-text-primary">
              {formatScore(slot.total_score)}
            </p>
          </StatBlock>
          <StatBlock label="EV">
            <p className="font-stats text-fluid-base font-bold text-text-accent">
              {formatEV(slot.filter_ev)}
            </p>
          </StatBlock>
          <StatBlock label="Slot Value">
            <NumberTicker
              value={slot.expected_slot_value}
              decimals={2}
              className="text-fluid-base font-bold text-brand-success"
            />
          </StatBlock>
        </div>

        {/* Env factors */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          {slot.env_factors.slice(0, 3).map((factor, i) => (
            <EnvFactorChip key={i} factor={factor} />
          ))}
          {slot.env_factors.length > 3 && (
            <span className="rounded-md bg-surface-elevated px-2 py-0.5 text-fluid-xs text-text-muted">
              +{slot.env_factors.length - 3} more
            </span>
          )}
        </div>

        {/* Trait breakdown accordion */}
        <TraitBreakdown breakdowns={slot.breakdowns} teamColor={primary} />
      </div>
    </motion.article>
  );
}
