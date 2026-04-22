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
  card_boost: number;
  total_score: number;
  env_score: number;
  env_factors: string[];
  popularity: "FADE" | "TARGET" | "NEUTRAL";
  filter_ev: number;
  expected_slot_value: number;
  game_id?: number | string | null;
  drafts?: number | null;
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
  starting_5: FilterLineupOut;
  moonshot: FilterLineupOut;
  all_candidates: unknown[];
}

export type LineupTab = "starting5" | "moonshot";

export interface WaitInfo {
  phase: "before_lock" | "generating" | "initializing";
  first_pitch_utc: string | null;
  lock_time_utc: string | null;
  minutes_until_lock: number | null;
}
