"use client";

import type { SlateClassificationOut } from "@/lib/types";
import styles from "./StickyHeader.module.css";

interface StickyHeaderProps {
  slate?: SlateClassificationOut | null;
}

function InlineBaseball() {
  return (
    <svg
      className={styles.inlineBaseball}
      viewBox="0 0 100 100"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        <radialGradient id="ballFill" cx="40%" cy="35%" r="55%">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="60%" stopColor="#e8e8e8" />
          <stop offset="100%" stopColor="#c0c0c0" />
        </radialGradient>
        <radialGradient id="ballGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#ff6b35" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#ff6b35" stopOpacity="0" />
        </radialGradient>
      </defs>
      {/* Orange glow behind the ball */}
      <circle cx="50" cy="50" r="48" fill="url(#ballGlow)" />
      {/* The baseball */}
      <circle cx="50" cy="50" r="36" fill="url(#ballFill)" />
      {/* Left seam */}
      <path
        d="M 34 22 Q 24 38 30 52 Q 36 66 32 80"
        fill="none"
        stroke="#cc0000"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      {/* Right seam */}
      <path
        d="M 66 20 Q 76 36 70 50 Q 64 64 68 80"
        fill="none"
        stroke="#cc0000"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      {/* Left stitch marks */}
      <g stroke="#cc0000" strokeWidth="1.8" strokeLinecap="round">
        <line x1="30" y1="28" x2="37" y2="26" />
        <line x1="27" y1="38" x2="34" y2="35" />
        <line x1="27" y1="48" x2="34" y2="47" />
        <line x1="29" y1="58" x2="36" y2="58" />
        <line x1="32" y1="68" x2="39" y2="69" />
        <line x1="31" y1="76" x2="38" y2="78" />
      </g>
      {/* Right stitch marks */}
      <g stroke="#cc0000" strokeWidth="1.8" strokeLinecap="round">
        <line x1="70" y1="26" x2="63" y2="25" />
        <line x1="73" y1="36" x2="66" y2="34" />
        <line x1="73" y1="46" x2="66" y2="46" />
        <line x1="71" y1="56" x2="64" y2="57" />
        <line x1="68" y1="66" x2="61" y2="68" />
        <line x1="69" y1="76" x2="62" y2="78" />
      </g>
    </svg>
  );
}

export function StickyHeader({ slate }: StickyHeaderProps) {
  return (
    <header className={styles.header}>
      <div className={styles.glowBackdrop} />

      <div className={styles.content}>
        <div className={styles.left}>
          <div className={styles.titleRow}>
            <h1 className={styles.title}>
              <span className={styles.gradientText}>
                BEN&nbsp;<InlineBaseball />RACLE
              </span>
            </h1>
            <span className={styles.bombBadge}>ABSOLUTE BOMB</span>
          </div>
          <span className={styles.sub}>SEES WHAT OTHERS MISS</span>
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

      <div className={styles.bottomFade} />
    </header>
  );
}
