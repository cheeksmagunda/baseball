"use client";

interface EnvFactorChipProps {
  factor: string;
}

export function EnvFactorChip({ factor }: EnvFactorChipProps) {
  return (
    <span className="inline-flex items-center rounded-md bg-surface-elevated px-2 py-0.5 text-fluid-xs text-text-secondary">
      {factor}
    </span>
  );
}
