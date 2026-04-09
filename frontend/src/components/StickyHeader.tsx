"use client";

import type { SlateClassificationOut } from "@/lib/types";
import styles from "./StickyHeader.module.css";

interface StickyHeaderProps {
  slate?: SlateClassificationOut | null;
}

export function StickyHeader({ slate }: StickyHeaderProps) {
  return (
    <header>
      <div className={styles.header}>
        <div className={styles.logo}>
          <img src="/icon.svg" alt="" className={styles.logoIcon} width={44} height={44} />
          <div>
            <h1 className={styles.title}>BEN ORACLE</h1>
            <span className={styles.sub}>SEES WHAT OTHERS MISS</span>
          </div>
        </div>

        {slate && (
          <div className={styles.meta}>
            <div className={styles.badge}>
              {slate.game_count} {slate.game_count === 1 ? "game" : "games"}
            </div>
            <div className={`${styles.badge} ${styles.badgeActive}`}>
              {slate.slate_type.replace("_", " ")}
            </div>
          </div>
        )}
      </div>
    </header>
  );
}
