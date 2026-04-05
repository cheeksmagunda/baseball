"use client";

import { useEffect, useRef, useState } from "react";
import { useReducedMotion } from "@/hooks/useReducedMotion";

interface NumberTickerProps {
  value: number;
  decimals?: number;
  duration?: number;
  className?: string;
}

export function NumberTicker({ value, decimals = 2, duration = 800, className = "" }: NumberTickerProps) {
  const [display, setDisplay] = useState(0);
  const reduced = useReducedMotion();
  const frameRef = useRef<number>(0);

  useEffect(() => {
    if (reduced) {
      setDisplay(value);
      return;
    }

    const start = performance.now();
    const from = 0;

    function tick(now: number) {
      const elapsed = now - start;
      const progress = Math.min(elapsed / duration, 1);
      // Ease out cubic
      const eased = 1 - Math.pow(1 - progress, 3);
      setDisplay(from + (value - from) * eased);

      if (progress < 1) {
        frameRef.current = requestAnimationFrame(tick);
      }
    }

    frameRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameRef.current);
  }, [value, duration, reduced]);

  return <span className={`font-stats ${className}`}>{display.toFixed(decimals)}</span>;
}
