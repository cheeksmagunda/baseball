"use client";

export function PlayerCardSkeleton() {
  return (
    <div
      aria-busy="true"
      className="overflow-hidden rounded-xl border border-border-subtle bg-surface-card"
    >
      <div className="p-4 pl-5">
        {/* Top row */}
        <div className="flex items-center justify-between">
          <div className="skeleton-shimmer h-5 w-24 rounded-md" />
          <div className="skeleton-shimmer h-5 w-14 rounded-full" />
        </div>

        {/* Name row */}
        <div className="mt-3 flex items-center gap-2">
          <div className="skeleton-shimmer h-6 w-40 rounded-md" />
          <div className="skeleton-shimmer h-5 w-10 rounded-md" />
          <div className="skeleton-shimmer h-4 w-6 rounded-md" />
        </div>

        {/* Score row */}
        <div className="mt-4 grid grid-cols-3 gap-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="space-y-1.5">
              <div className="skeleton-shimmer h-3 w-12 rounded" />
              <div className="skeleton-shimmer h-5 w-16 rounded" />
            </div>
          ))}
        </div>

        {/* Badges row */}
        <div className="mt-3 flex gap-1.5">
          <div className="skeleton-shimmer h-5 w-16 rounded-full" />
          <div className="skeleton-shimmer h-5 w-28 rounded-md" />
          <div className="skeleton-shimmer h-5 w-24 rounded-md" />
        </div>
      </div>
    </div>
  );
}
