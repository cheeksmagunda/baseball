"use client";

import type { FilterOptimizeResponse, OptimizeStatus } from "@/lib/types";
import { useLineupData } from "@/hooks/useLineupData";
import { useSlatePolling } from "@/hooks/useSlatePolling";
import { useLiveStats } from "@/hooks/useLiveStats";
import { StickyHeader } from "@/components/StickyHeader";
import { LineupStack } from "@/components/LineupStack";
import { LoadingSkeleton } from "@/components/LoadingSkeleton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { WaitState } from "@/components/WaitState";

interface ClientHomeProps {
  initialData: FilterOptimizeResponse | null;
  initialStatus: OptimizeStatus | null;
}

export function ClientHome({ initialData, initialStatus }: ClientHomeProps) {
  const { data, loading, error, waitInfo, refetch } = useLineupData(initialData, initialStatus);
  const liveStats = useLiveStats(!!data && !loading && !error && !waitInfo);

  useSlatePolling(refetch);

  if (loading) {
    return (
      <div className="flex min-h-screen flex-col">
        <StickyHeader />
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

  if (!data || !data.lineup.lineup.length) {
    return (
      <div className="flex min-h-screen flex-col">
        <StickyHeader />
        <main className="flex-1">
          <EmptyState />
        </main>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col">
      <StickyHeader slate={data.slate_classification} />
      <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-6 sm:px-6">
        <LineupStack lineup={data.lineup} liveStats={liveStats} />
      </main>
    </div>
  );
}
