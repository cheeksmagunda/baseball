"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { TraitBreakdown as TraitBreakdownType } from "@/lib/types";
import { traitDisplayName } from "@/lib/formatters";

interface TraitBreakdownProps {
  breakdowns: TraitBreakdownType[];
  teamColor: string;
}

export function TraitBreakdown({ breakdowns, teamColor }: TraitBreakdownProps) {
  const [open, setOpen] = useState(false);

  if (breakdowns.length === 0) return null;

  return (
    <div className="mt-2 border-t border-border-subtle pt-2">
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center justify-between text-fluid-xs font-medium text-text-muted transition-colors hover:text-text-secondary focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none"
      >
        <span>Trait Breakdown</span>
        <motion.span
          animate={{ rotate: open ? 180 : 0 }}
          transition={{ duration: 0.2 }}
          className="text-sm"
        >
          &#9662;
        </motion.span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-2 space-y-1.5">
              {breakdowns.map((trait) => {
                const pct = trait.max_score > 0 ? (trait.score / trait.max_score) * 100 : 0;
                return (
                  <div key={trait.trait_name} className="space-y-0.5">
                    <div className="flex items-center justify-between">
                      <span className="text-fluid-xs text-text-secondary">
                        {traitDisplayName(trait.trait_name)}
                      </span>
                      <span className="font-stats text-fluid-xs text-text-primary">
                        {trait.score.toFixed(1)}/{trait.max_score.toFixed(0)}
                      </span>
                    </div>
                    <div className="h-1 overflow-hidden rounded-full bg-white/5">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${pct}%` }}
                        transition={{ duration: 0.5, delay: 0.1 }}
                        className="h-full rounded-full"
                        style={{ backgroundColor: `${teamColor}cc` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
