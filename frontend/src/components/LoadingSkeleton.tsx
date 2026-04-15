"use client";

import { PlayerCardSkeleton } from "./PlayerCardSkeleton";

function ColumnSkeleton() {
  return (
    <div className="space-y-3">
      {/* Header skeleton */}
      <div className="flex items-center justify-between pb-2">
        <div className="space-y-1">
          <div className="skeleton-shimmer h-6 w-28 rounded-md" />
          <div className="skeleton-shimmer h-3 w-40 rounded" />
        </div>
        <div className="space-y-1 text-right">
          <div className="skeleton-shimmer h-3 w-12 rounded" />
          <div className="skeleton-shimmer h-8 w-20 rounded-md" />
        </div>
      </div>

      {/* 5 card skeletons */}
      {Array.from({ length: 5 }).map((_, i) => (
        <PlayerCardSkeleton key={i} />
      ))}
    </div>
  );
}

export function LoadingSkeleton() {
  return (
    <div className="mx-auto w-full max-w-md px-4 py-4 lg:max-w-6xl lg:px-6">
      {/* Mobile: single column, Desktop: two columns */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {[false, true].map((hiddenOnMobile, i) => (
          <div key={i} className={hiddenOnMobile ? "hidden lg:block" : undefined}>
            <ColumnSkeleton />
          </div>
        ))}
      </div>
    </div>
  );
}
