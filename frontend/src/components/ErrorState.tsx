"use client";

interface ErrorStateProps {
  status: number;
  message: string;
  onRetry: () => void;
}

export function ErrorState({ status, message, onRetry }: ErrorStateProps) {
  return (
    <div role="alert" className="flex min-h-[60vh] flex-col items-center justify-center px-4 text-center">
      <div className="mb-4 flex h-20 w-20 items-center justify-center rounded-full bg-brand-danger/15">
        <span className="font-stats text-fluid-2xl font-black text-brand-danger">
          {status || "!"}
        </span>
      </div>
      <h2 className="text-fluid-xl font-bold text-text-primary">Something Went Wrong</h2>
      <p className="mt-2 max-w-xs text-fluid-sm text-text-muted">{message}</p>
      <button
        onClick={onRetry}
        className="mt-6 rounded-xl bg-brand-primary px-6 py-2.5 text-fluid-sm font-semibold text-white transition-colors hover:bg-brand-hover focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:ring-offset-2 focus-visible:ring-offset-surface-base focus-visible:outline-none"
      >
        Retry
      </button>
    </div>
  );
}
