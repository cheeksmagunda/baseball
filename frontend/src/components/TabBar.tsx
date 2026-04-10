"use client";

import { useRef, useEffect, useState, useCallback } from "react";
import type { LineupTab } from "@/lib/types";
import styles from "./TabBar.module.css";

interface TabBarProps {
  activeTab: LineupTab;
  onTabChange: (tab: LineupTab) => void;
}

const TABS: { key: LineupTab; label: string }[] = [
  { key: "starting5", label: "Starting 5" },
  { key: "moonshot", label: "Moonshot" },
];

export function TabBar({ activeTab, onTabChange }: TabBarProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const btnRefs = useRef<Map<string, HTMLButtonElement>>(new Map());
  const [pillStyle, setPillStyle] = useState<React.CSSProperties>({});

  const measurePill = useCallback(() => {
    const wrap = wrapRef.current;
    const btn = btnRefs.current.get(activeTab);
    if (!wrap || !btn) return;

    const wrapRect = wrap.getBoundingClientRect();
    const btnRect = btn.getBoundingClientRect();

    setPillStyle({
      width: btnRect.width,
      transform: `translateX(${btnRect.left - wrapRect.left - 3}px)`,
    });
  }, [activeTab]);

  useEffect(() => {
    measurePill();
  }, [measurePill]);

  useEffect(() => {
    window.addEventListener("resize", measurePill);
    return () => window.removeEventListener("resize", measurePill);
  }, [measurePill]);

  return (
    <div className="pb-2 pt-2 lg:hidden">
      <div className="mx-auto max-w-md px-4">
        <div
          ref={wrapRef}
          role="tablist"
          aria-label="Lineup selection"
          className={styles.wrap}
        >
          <div className={styles.pill} style={pillStyle} />
          {TABS.map((tab) => {
            const isActive = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                ref={(el) => {
                  if (el) btnRefs.current.set(tab.key, el);
                  else btnRefs.current.delete(tab.key);
                }}
                role="tab"
                aria-selected={isActive}
                aria-controls={`panel-${tab.key}`}
                onClick={() => {
                  if (!isActive) {
                    navigator.vibrate?.(10);
                    onTabChange(tab.key);
                  }
                }}
                className={`${styles.btn} ${isActive ? styles.active : ""}`}
                type="button"
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
