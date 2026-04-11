"use client";

import { useState, useCallback } from "react";
import { motion, useMotionValue, useTransform, type PanInfo } from "framer-motion";
import type { LineupTab, FilterOptimizeResponse } from "@/lib/types";
import { useLineupData } from "@/hooks/useLineupData";
import { useSlatePolling } from "@/hooks/useSlatePolling";
import { useReducedMotion } from "@/hooks/useReducedMotion";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { StickyHeader } from "@/components/StickyHeader";
import { TabBar } from "@/components/TabBar";
import { LineupStack } from "@/components/LineupStack";
import { LoadingSkeleton } from "@/components/LoadingSkeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { WaitState } from "@/components/WaitState";

const SWIPE_THRESHOLD = 50;

interface ClientHomeProps {
  initialData: FilterOptimizeResponse | null;
}

export function ClientHome({ initialData }: ClientHomeProps) {
  const { data, loading, error, waitInfo, refetch } = useLineupData(initialData);
  const [activeTab, setActiveTab] = useState<LineupTab>("starting5");
  const [direction, setDirection] = useState(0);
  const reduced = useReducedMotion();
  const isDesktop = useMediaQuery("(min-width: 1024px)");

  const dragX = useMotionValue(0);
  const dragOpacity = useTransform(dragX, [-150, 0, 150], [0.5, 1, 0.5]);

  const switchTab = useCallback(
    (tab: LineupTab) => {
      setDirection(tab === "moonshot" ? 1 : -1);
      setActiveTab(tab);
    },
    [],
  );

  useSlatePolling(refetch);

  function handleDragEnd(_: unknown, info: PanInfo) {
    if (reduced || isDesktop) return;
    const { offset, velocity } = info;
    const swipe = Math.abs(offset.x) * velocity.x;

    if (offset.x < -SWIPE_THRESHOLD || swipe < -1000) {
      if (activeTab === "starting5") switchTab("moonshot");
    } else if (offset.x > SWIPE_THRESHOLD || swipe > 1000) {
      if (activeTab === "moonshot") switchTab("starting5");
    }
  }

  if (loading) {
    return (
      <div className="flex min-h-screen flex-col">
        <StickyHeader />
        <TabBar activeTab={activeTab} onTabChange={switchTab} />
        <main className="flex-1 py-4">
          <LoadingSkeleton />
        </main>
      </div>
    );
  }

  if (waitInfo) {
    return (
      <div className="flex min-h-screen flex-col">
        <StickyHeader />
        <main className="flex-1">
          <WaitState waitInfo={waitInfo} onReady={refetch} />
        </main>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex min-h-screen flex-col">
        <StickyHeader />
        <main className="flex-1">
          <ErrorState status={error.status} message={error.message} onRetry={refetch} />
        </main>
      </div>
    );
  }

  if (!data || (!data.starting_5.lineup.length && !data.moonshot.lineup.length)) {
    return (
      <div className="flex min-h-screen flex-col">
        <StickyHeader />
        <main className="flex-1">
          <EmptyState />
        </main>
      </div>
    );
  }

  const currentLineup = activeTab === "starting5" ? data.starting_5 : data.moonshot;

  /* ── Desktop: side-by-side lineups ── */
  if (isDesktop) {
    return (
      <div className="flex min-h-screen flex-col">
        <StickyHeader slate={data.slate_classification} />

        <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-6">
          <div className="grid grid-cols-2 gap-6">
            <LineupStack lineup={data.starting_5} tab="starting5" direction={0} />
            <LineupStack lineup={data.moonshot} tab="moonshot" direction={0} />
          </div>
        </main>
      </div>
    );
  }

  /* ── Mobile: tabbed + swipeable ── */
  return (
    <div className="flex min-h-screen flex-col">
      <StickyHeader slate={data.slate_classification} />
      <TabBar activeTab={activeTab} onTabChange={switchTab} />

      <motion.main
        className="flex-1 py-4"
        drag={reduced ? false : "x"}
        dragConstraints={{ left: 0, right: 0 }}
        dragElastic={0.15}
        onDragEnd={handleDragEnd}
        style={{ x: dragX, opacity: dragOpacity }}
      >
        <LineupStack lineup={currentLineup} tab={activeTab} direction={direction} />
      </motion.main>
    </div>
  );
}
