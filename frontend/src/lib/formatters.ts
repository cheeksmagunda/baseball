export function formatScore(value: number): string {
  return value.toFixed(1);
}

export function formatEV(value: number): string {
  return value.toFixed(2);
}

export function formatBoost(boost: number): string {
  if (boost === 0) return "+0.0x";
  return `+${boost.toFixed(1)}x`;
}

export function formatSlotMult(mult: number): string {
  return `${mult.toFixed(1)}x`;
}

export function formatPercent(value: number): string {
  return `${(value * 100).toFixed(0)}%`;
}

export function positionLabel(position: string): string {
  const map: Record<string, string> = {
    SP: "Starting Pitcher",
    RP: "Relief Pitcher",
    C: "Catcher",
    "1B": "First Base",
    "2B": "Second Base",
    "3B": "Third Base",
    SS: "Shortstop",
    LF: "Left Field",
    CF: "Center Field",
    RF: "Right Field",
    DH: "Designated Hitter",
    OF: "Outfield",
  };
  return map[position] ?? position;
}

export function traitDisplayName(traitName: string): string {
  return traitName
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
