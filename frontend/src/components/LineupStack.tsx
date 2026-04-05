"use client";

import { motion, AnimatePresence } from "framer-motion";
import type { FilterLineupOut, LineupTab } from "@/lib/types";
import { PlayerCard } from "./PlayerCard";
import { NumberTicker } from "./NumberTicker";
import { useReducedMotion } from "@/hooks/useReducedMotion";

interface LineupStackProps {
  lineup: FilterLineupOut;
  tab: LineupTab;
  direction: number;
}

export function LineupStack({ lineup, tab, direction }: LineupStackProps) {
  const reduced = useReducedMotion();
  const isMoonshot = tab === "moonshot";

  const variants = reduced
    ? { enter: {}, center: {}, exit: {} }
    : {
        enter: (d: number) => ({ x: d > 0 ? 300 : -300, opacity: 0 }),
        center: { x: 0, opacity: 1 },
        exit: (d: number) => ({ x: d > 0 ? -300 : 300, opacity: 0 }),
      };

  return (
    <div
      id={`panel-${tab}`}
      role="tabpanel"
      aria-label={isMoonshot ? "Moonshot lineup" : "Starting 5 lineup"}
      className="mx-auto w-full max-w-md px-4"
    >
      {/* Lineup header */}
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-fluid-lg font-bold text-text-primary">
            {isMoonshot ? "Moonshot" : "Starting 5"}
          </h2>
          <p className="text-fluid-xs text-text-muted">{lineup.strategy}</p>
        </div>
        <div className="text-right">
          <p className="text-fluid-xs text-text-muted">Total EV</p>
          <NumberTicker
            value={lineup.total_expected_value}
            decimals={2}
            duration={1000}
            className={`text-fluid-xl font-black ${
              isMoonshot ? "text-brand-moonshot" : "text-brand-primary"
            }`}
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
      <AnimatePresence mode="wait" custom={direction}>
        <motion.div
          key={tab}
          custom={direction}
          variants={variants}
          initial="enter"
          animate="center"
          exit="exit"
          transition={{ type: "spring", stiffness: 300, damping: 30 }}
          className="space-y-3 pb-8"
        >
          {lineup.lineup.map((slot, i) => (
            <PlayerCard key={slot.slot_index} slot={slot} index={i} isMoonshot={isMoonshot} />
          ))}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}
