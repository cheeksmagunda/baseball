"use client";

export function EmptyState() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center px-4 text-center">
      <div className="mb-4 flex h-20 w-20 items-center justify-center rounded-full bg-surface-elevated">
        <span className="text-4xl">&#9918;</span>
      </div>
      <h2 className="text-fluid-xl font-bold text-text-primary">No Games Today</h2>
      <p className="mt-2 max-w-xs text-fluid-sm text-text-muted">
        There are no slates available right now. Check back when games are scheduled.
      </p>
    </div>
  );
}
