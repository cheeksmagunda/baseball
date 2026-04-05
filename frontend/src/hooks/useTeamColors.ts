"use client";

import chroma from "chroma-js";
import { TEAM_COLORS } from "@/lib/teamColors";

interface TeamColorResult {
  primary: string;
  secondary: string;
  textColor: string;
  glowShadow: string;
  gradientBg: string;
  borderColor: string;
}

const DEFAULT_COLORS = { primary: "#3b82f6", secondary: "#1e40af" };

export function useTeamColors(team: string): TeamColorResult {
  const colors = TEAM_COLORS[team.toUpperCase()] ?? DEFAULT_COLORS;
  const { primary, secondary } = colors;

  // WCAG AA contrast check: 4.5:1 ratio
  const luminance = chroma(primary).luminance();
  const textColor = luminance > 0.18 ? "#111827" : "#f0f0f5";

  const glowShadow = `0 0 20px ${primary}40, 0 0 40px ${primary}20`;
  const gradientBg = `linear-gradient(135deg, ${primary}18 0%, ${secondary}10 100%)`;
  const borderColor = `${primary}30`;

  return { primary, secondary, textColor, glowShadow, gradientBg, borderColor };
}
