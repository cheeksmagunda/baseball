export interface TraitBreakdown {
  trait_name: string;
  score: number;
  max_score: number;
  raw_value?: string | null;
}

export interface FilterSlotOut {
  slot_index: number;
  slot_mult: number;
  player_name: string;
  team: string;
  position: string;
  total_score: number;
  env_score: number;
  env_factors: string[];
  filter_ev: number;
  expected_slot_value: number;
  game_id?: number | string | null;
  is_two_way_pitcher?: boolean;
  breakdowns: TraitBreakdown[];
}

export interface FilterLineupOut {
  lineup: FilterSlotOut[];
  total_expected_value: number;
  strategy: string;
  composition: Record<string, number>;
  warnings: string[];
}

export interface SlateClassificationOut {
  slate_type: string;
  game_count: number;
  quality_sp_matchups: number;
  high_total_games: number;
  reason: string;
}

export interface FilterOptimizeResponse {
  slate_classification: SlateClassificationOut;
  lineup: FilterLineupOut;
  all_candidates: unknown[];
}

export interface LivePlayerStats {
  player_name: string;
  team: string;
  position: string;
  game_status: string | null;
  // Batter
  ab: number | null;
  h: number | null;
  hr: number | null;
  rbi: number | null;
  bb: number | null;
  k: number | null;
  // Pitcher
  ip: string | null;
  er: number | null;
  k_p: number | null;
}

export interface LiveStatsResponse {
  players: LivePlayerStats[];
}

export interface WaitInfo {
  phase: "before_lock" | "generating" | "initializing";
  first_pitch_utc: string | null;
  lock_time_utc: string | null;
  minutes_until_lock: number | null;
}

export interface OptimizeStatus {
  ready: boolean;
  phase: "no_slate" | "before_lock" | "generating" | "ready" | "failed";
  first_pitch_utc: string | null;
  lock_time_utc: string | null;
  minutes_until_lock: number | null;
  // Populated when phase === "failed": short summary of why the T-65 pipeline
  // crashed. Surfaced in the ErrorState UI so the user knows picks aren't
  // coming and can refresh after a redeploy.
  error: string | null;
}
