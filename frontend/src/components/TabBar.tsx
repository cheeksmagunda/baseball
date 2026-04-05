"use client";

import { motion } from "framer-motion";
import type { LineupTab } from "@/lib/types";

interface TabBarProps {
  activeTab: LineupTab;
  onTabChange: (tab: LineupTab) => void;
}

const TABS: { key: LineupTab; label: string; icon: string }[] = [
  { key: "starting5", label: "Starting 5", icon: "S5" },
  { key: "moonshot", label: "Moonshot", icon: "MS" },
];

export function TabBar({ activeTab, onTabChange }: TabBarProps) {
  return (
    <div
      role="tablist"
      aria-label="Lineup selection"
      className="glass-strong sticky top-[60px] z-40 mx-auto flex max-w-md gap-1 px-4 py-2"
    >
      {TABS.map((tab) => {
        const isActive = activeTab === tab.key;
        return (
          <button
            key={tab.key}
            role="tab"
            aria-selected={isActive}
            aria-controls={`panel-${tab.key}`}
            onClick={() => {
              if (!isActive) {
                navigator.vibrate?.(10);
                onTabChange(tab.key);
              }
            }}
            className={`relative flex-1 rounded-xl px-4 py-2.5 text-fluid-sm font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:ring-offset-2 focus-visible:ring-offset-surface-base focus-visible:outline-none ${
              isActive ? "text-text-primary" : "text-text-muted hover:text-text-secondary"
            }`}
          >
            {isActive && (
              <motion.div
                layoutId="tab-indicator"
                className={`absolute inset-0 rounded-xl ${
                  tab.key === "moonshot"
                    ? "bg-brand-moonshot/15 shadow-[0_0_16px_rgba(168,85,247,0.15)]"
                    : "bg-brand-primary/15 shadow-[0_0_16px_rgba(59,130,246,0.15)]"
                }`}
                transition={{ type: "spring", stiffness: 400, damping: 30 }}
              />
            )}
            <span className="relative z-10 flex items-center justify-center gap-2">
              <span
                className={`flex h-5 w-5 items-center justify-center rounded-md text-[10px] font-bold ${
                  isActive
                    ? tab.key === "moonshot"
                      ? "bg-brand-moonshot/30 text-brand-moonshot"
                      : "bg-brand-primary/30 text-brand-primary"
                    : "bg-surface-elevated text-text-muted"
                }`}
              >
                {tab.icon}
              </span>
              {tab.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}
