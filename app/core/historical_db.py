"""Historical calibration corpus — SQLite schema and connection helpers.

The historical store at `data/historical.db` is the canonical source of truth
for the calibration corpus (leaderboard outcomes, winning lineups, at-slate
inputs, prior game logs, slate-level env signals).  CSVs/JSON in /data/ are
byte-stable derived exports refreshed on every write — they remain
human-readable for ad-hoc inspection but the runtime/calibration paths read
from SQLite.

Five logical tables:
  - slate            — one row per slate envelope (date, game count, etc.)
  - slate_game       — one row per (slate_date, game_pk); env signals + post-game
  - player_slate     — one row per (slate_date, mlb_id); identity + at-slate inputs
  - player_game_log  — one row per (slate_date, mlb_id, game_date); prior-game
                       outcomes that feed recent_form / hot_streak calibration
  - label_event      — one row per (slate_date, mlb_id, label_type, source);
                       the typed/sourced outcome label store.  The presence or
                       absence of a row IS the signal — replaces empty-cell
                       semantics from the CSV era.

This module is in `app/core/` because both calibration scripts and the live
runtime (`app/core/popularity.py` after Step 5) need it.  The ONLY caller in
`app/` permitted to query outcome labels is `popularity.py`, and only for the
prior-slate `most_popular` flag — exactly the same carve-out the CSV era had.
The audit script `scripts/audit_live_isolation.py` enforces this.

The schema is deliberately permissive (no NOT NULL beyond PKs, no CHECKs, JSON
held as TEXT) so that:
  * Backfills can run incrementally without staging.
  * Synthetic / future-derived outcome labels can land as new label_type values
    without schema migrations.
  * A row in `player_slate` can exist without any matching `label_event` rows
    (the "did not appear on a leaderboard" case that the CSV could not express
    distinctly from "DNP").
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "historical.db"


def resolve_db_path(override: str | os.PathLike | None = None) -> Path:
    """Resolve the canonical DB path with override precedence.

    Order: explicit `override` arg → `HISTORICAL_DB` env var → `DEFAULT_DB_PATH`.
    The env var is the seam tests + the synthetic-multiplier scaling check
    (Step 7) use to point readers at a non-default DB without touching code.
    """
    if override is not None:
        return Path(override)
    env = os.environ.get("HISTORICAL_DB")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS slate (
    slate_date    TEXT PRIMARY KEY,
    game_count    INTEGER NOT NULL,
    num_brawlers  INTEGER,
    season_stage  TEXT,
    source        TEXT,
    saved_at      TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS slate_game (
    slate_date                  TEXT NOT NULL,
    game_pk                     INTEGER NOT NULL,
    game_number                 INTEGER NOT NULL DEFAULT 1,  -- 1/2 for doubleheaders sharing a game_pk
    home_team                   TEXT NOT NULL,
    away_team                   TEXT NOT NULL,
    -- starter env signals
    home_starter_id             INTEGER,
    home_starter_name           TEXT,
    home_starter_hand           TEXT,
    home_starter_era            REAL,
    home_starter_whip           REAL,
    home_starter_k_per_9        REAL,
    home_starter_x_era          REAL,
    home_starter_x_woba_against REAL,
    away_starter_id             INTEGER,
    away_starter_name           TEXT,
    away_starter_hand           TEXT,
    away_starter_era            REAL,
    away_starter_whip           REAL,
    away_starter_k_per_9        REAL,
    away_starter_x_era          REAL,
    away_starter_x_woba_against REAL,
    -- team season env signals
    home_team_ops               REAL,
    home_team_k_pct             REAL,
    home_bullpen_era            REAL,
    home_team_framing_runs      REAL,
    home_team_framing_pct       REAL,
    away_team_ops               REAL,
    away_team_k_pct             REAL,
    away_bullpen_era            REAL,
    away_team_framing_runs      REAL,
    away_team_framing_pct       REAL,
    home_team_rest_days         INTEGER,
    away_team_rest_days         INTEGER,
    home_l10_wins               INTEGER,
    home_series_wins            INTEGER,
    away_l10_wins               INTEGER,
    away_series_wins            INTEGER,
    -- Vegas (closing snapshot at T-65)
    vegas_total                 REAL,
    home_moneyline              INTEGER,
    away_moneyline              INTEGER,
    -- Tier 1 D4: Vegas line movement (open → close).  ML drift is a sharp-
    -- money signal distinct from the closing line.  Sourced from The Odds
    -- API /v4/historical/sports endpoint by backfill_vegas_line_movement.py.
    opening_total               REAL,
    opening_home_moneyline      INTEGER,
    opening_away_moneyline      INTEGER,
    line_open_at                TEXT,
    -- Tier 2 D7: bullpen cumulative pitch counts (rolling 2/3-day window
    -- per team).  Tired bullpens hand the late-inning advantage to the
    -- offense.  Derived from player_game_log by backfill_bullpen_rest.py.
    home_bullpen_2d_pitches     INTEGER,
    away_bullpen_2d_pitches     INTEGER,
    home_bullpen_3d_pitches     INTEGER,
    away_bullpen_3d_pitches     INTEGER,
    -- Phase C add: bullpen handedness usage in the trailing 2 days.
    -- LHP-heavy bullpen burned yesterday → batters get more RHP looks
    -- tonight (favorable for LHB).  Derived from player_game_log JOIN
    -- player_dim.pitch_hand by backfill_bullpen_handedness.py.
    home_bullpen_lhp_pitches_2d INTEGER,
    home_bullpen_rhp_pitches_2d INTEGER,
    away_bullpen_lhp_pitches_2d INTEGER,
    away_bullpen_rhp_pitches_2d INTEGER,
    -- Phase C add: lineup handedness composition (count of LHB / RHB /
    -- switch in batting_order ≤ 9).  Derived from player_dim JOIN
    -- player_slate per slate_date.
    home_lineup_lhb_count       INTEGER,
    home_lineup_rhb_count       INTEGER,
    home_lineup_switch_count    INTEGER,
    away_lineup_lhb_count       INTEGER,
    away_lineup_rhb_count       INTEGER,
    away_lineup_switch_count    INTEGER,
    -- Phase C add: Pythagorean expectation gap.  Pythag W% =
    -- runs_scored^2 / (runs_scored^2 + runs_allowed^2).  Gap = actual
    -- W% - pythag W%.  Negative gap → "unlucky" team likely to regress
    -- positively; positive gap → "lucky" team due for negative regression.
    -- Derived from standings runs_scored / runs_allowed / W-L.
    home_pythag_gap             REAL,
    away_pythag_gap             REAL,
    -- Phase C add: team defensive runs saved (DRS).  Suppresses BABIP
    -- behind a starter — independent signal from framing (which only
    -- moves K%).  Sourced from FanGraphs' team-defense leaderboard via
    -- pybaseball (cached).
    home_team_defense_drs       REAL,
    away_team_defense_drs       REAL,
    -- Phase E add: team L10 offensive momentum.  L10 wins ≠ runs scored;
    -- a team can be 7-3 winning 3-2 every night while the offense is dead.
    -- L10 wOBA + RPG capture run-environment trend orthogonal to wins.
    home_team_woba_l10          REAL,
    home_team_rpg_l10           REAL,
    away_team_woba_l10          REAL,
    away_team_rpg_l10           REAL,
    -- Phase E add: bullpen handedness ROSTER composition (count of distinct
    -- LHP / RHP relievers active in the trailing 14 days).  Distinct from
    -- bullpen_lhp_pitches_2d (usage burn) — a team with 4 LHP relievers
    -- structurally has more late-inning LHB-suppression options regardless
    -- of yesterday's usage.
    home_bullpen_lhp_count      INTEGER,
    home_bullpen_rhp_count      INTEGER,
    away_bullpen_lhp_count      INTEGER,
    away_bullpen_rhp_count      INTEGER,
    -- Phase E add: schedule context flags.  is_getaway_day = 1 when the
    -- team flies out to a different venue tonight (last game of a series).
    -- is_day_after_night_game = 1 when the team's previous game ended
    -- after 22:00 local time — both lightly suppress hitters via fatigue.
    is_getaway_day              INTEGER,
    is_day_after_night_game     INTEGER,
    -- park / weather (pre-game forecast, what the live pipeline reads at T-65)
    park_hr_factor              REAL,
    -- Phase C add: park HR factor split by batter handedness.  Coors plays
    -- different for LHB vs RHB; aggregated index masks the asymmetry.
    -- Sourced from Savant's statcast-park-factors leaderboard (3-year rolling
    -- window centred on the slate season).
    park_hr_factor_lhb          REAL,
    park_hr_factor_rhb          REAL,
    temperature_f               REAL,
    wind_speed_mph              REAL,
    wind_direction              TEXT,
    wind_direction_deg          INTEGER,
    -- Phase E add: humidity + barometric pressure + computed air-density
    -- ratio.  HR distance scales DIRECTLY with air density — Coors plays
    -- the way it does because of this; humid Florida nights play smaller
    -- than the temperature alone suggests.  Sourced from Open-Meteo
    -- Archive at first-pitch hour at venue lat/lon.  air_density_ratio is
    -- a multiplier on ball flight (>1.00 = ball carries less; <1.00 = ball
    -- carries farther — ratio defined as actual / sea-level-standard).
    humidity_pct                INTEGER,
    pressure_hpa                REAL,
    air_density_ratio           REAL,
    -- Phase E add: travel fatigue.  Derived from venue chain prior to
    -- slate_date.  miles_traveled_72h captures back-to-back-to-back road
    -- fatigue; zones_crossed_24h captures cross-country-flight body-clock
    -- disruption (the Brewers redeye-east is the canonical example).
    home_team_miles_traveled_72h INTEGER,
    home_team_zones_crossed_24h INTEGER,
    away_team_miles_traveled_72h INTEGER,
    away_team_zones_crossed_24h INTEGER,
    -- Phase E add: wind × handedness joint signal.  wind_to_rf_component is
    -- the wind speed projected onto the HP→RF axis (positive = blowing
    -- toward RF, helps LHB pull power); wind_to_lf_component projects onto
    -- HP→LF (helps RHB pull power).  Computed from wind_speed +
    -- wind_direction_deg + venue_dim.hp_to_cf_azimuth_deg.
    wind_to_rf_component        REAL,
    wind_to_lf_component        REAL,
    datetime_utc                TEXT,
    -- post-game outcomes (winner/loser/winner_score/loser_score derived on
    -- export from home_team/away_team/home_score/away_score — not stored).
    home_score                  INTEGER,
    away_score                  INTEGER,
    -- Step 9: venue + catcher identity.  Venue static dimensions
    -- (capacity/surface/roof/lat/lon/fence-distances) live in `venue_dim`
    -- as of the May 2026 Phase D cleanup — they're slowly-changing
    -- per-venue properties, not per-game facts.  venue_id is the join
    -- key.
    venue_id                    INTEGER,
    venue_name                  TEXT,
    -- Step 9: actual catcher IDs (the team-season framing aggregate is
    -- already on home/away_team_framing_runs; this lets calibration ask
    -- "did the team's elite framer actually catch this game?")
    home_catcher_id             INTEGER,
    away_catcher_id             INTEGER,
    -- Step 10: per-pitcher boxscore detail (post-game external).  IP is
    -- expressed as outs_recorded so partial-inning math is clean (3 outs
    -- per inning; "5.1 IP" → 16 outs).  Lets calibration ask "did this
    -- starter get pulled early because of pitch count or because of game
    -- script?" via the pitch_count vs outs_recorded ratio.
    home_starter_pitch_count    INTEGER,
    home_starter_outs_recorded  INTEGER,
    home_starter_hits_allowed   INTEGER,
    home_starter_runs_allowed   INTEGER,
    home_starter_er_allowed     INTEGER,
    home_starter_walks          INTEGER,
    home_starter_strikeouts     INTEGER,
    home_starter_hr_allowed     INTEGER,
    away_starter_pitch_count    INTEGER,
    away_starter_outs_recorded  INTEGER,
    away_starter_hits_allowed   INTEGER,
    away_starter_runs_allowed   INTEGER,
    away_starter_er_allowed     INTEGER,
    away_starter_walks          INTEGER,
    away_starter_strikeouts     INTEGER,
    away_starter_hr_allowed     INTEGER,
    -- Step 10: per-team bullpen usage this game (relievers only — starter
    -- excluded).  outs_recorded gives the workload; pitchers_used the
    -- arm count.
    home_bullpen_pitchers_used  INTEGER,
    home_bullpen_outs_recorded  INTEGER,
    home_bullpen_pitch_count    INTEGER,
    away_bullpen_pitchers_used  INTEGER,
    away_bullpen_outs_recorded  INTEGER,
    away_bullpen_pitch_count    INTEGER,
    -- (May 2026 Phase D cleanup dropped: actual_* weather (post-game
    -- forecast-validation, not a predictor), home/away_team_doubles /
    -- triples / walks / strikeouts / LOB / SB / errors (post-game team-box
    -- noise — HR / hits / runs are kept), home/away_team_streak
    -- (autocorrelated with l10_wins), home/away_team_division_rank /
    -- league_rank (slow-moving, weak DFS signal, derivable from games_back),
    -- home/away_team_record_w/_l (duplicate of home/away_record sums),
    -- attendance + day_night (low DFS signal-cost), park_team (synonym
    -- for home_team), ump_hp_id / ump_hp_name (2026 ABS Challenge System
    -- compresses the umpire signal too far for storage to be worth it).
    home_team_hits              INTEGER,
    home_team_hr                INTEGER,
    away_team_hits              INTEGER,
    away_team_hr                INTEGER,
    -- Step 16: as-of-slate-date team standings snapshot from MLB Stats API.
    home_team_games_back        REAL,    -- games behind division leader
    home_team_runs_scored       INTEGER, -- season-to-date
    home_team_runs_allowed      INTEGER,
    home_team_home_record       TEXT,    -- '12-8' (W-L when at home)
    home_team_away_record       TEXT,
    away_team_games_back        REAL,
    away_team_runs_scored       INTEGER,
    away_team_runs_allowed      INTEGER,
    away_team_home_record       TEXT,
    away_team_away_record       TEXT,
    PRIMARY KEY (slate_date, game_pk, game_number),
    FOREIGN KEY (slate_date) REFERENCES slate(slate_date)
);
CREATE INDEX IF NOT EXISTS idx_slate_game_pk ON slate_game(game_pk);

CREATE TABLE IF NOT EXISTS player_slate (
    slate_date              TEXT NOT NULL,
    mlb_id                  INTEGER NOT NULL,
    player_name             TEXT NOT NULL,
    team                    TEXT NOT NULL,
    position                TEXT NOT NULL,
    game_pk                 INTEGER,
    -- at-slate inputs (the live pipeline reads these pre-game)
    ops_at_slate            REAL,
    iso_at_slate            REAL,
    era_at_slate            REAL,
    whip_at_slate           REAL,
    k9_at_slate             REAL,
    ops_vs_lhp_at_slate     REAL,
    ops_vs_rhp_at_slate     REAL,
    batting_order_at_slate  INTEGER,
    -- Statcast snapshot at slate
    x_woba                  REAL,
    x_ba                    REAL,
    x_slg                   REAL,
    avg_ev                  REAL,
    hard_hit_pct            REAL,
    barrel_pct              REAL,
    max_ev                  REAL,
    x_era                   REAL,
    x_woba_against          REAL,
    fb_velo                 REAL,
    whiff_pct               REAL,
    chase_pct               REAL,
    fb_ivb                  REAL,
    fb_extension            REAL,
    -- (Step 11 per-player externals — bat_side / pitch_hand / birth_date /
    -- mlb_debut_date / height_in / weight_lb / birth_country /
    -- primary_position_code — moved to the player_dim table in Phase C of
    -- the May 2026 cleanup sweep.  These attributes are slowly-changing
    -- dimensions of a player_id, not per-slate facts; storing one row per
    -- (slate_date, mlb_id) was wasteful.  Joining via mlb_id gives the
    -- same data with O(N_players) instead of O(N_slates × N_players)
    -- storage.)
    -- Step 12: pitcher pitch-arsenal usage % from Savant.  Each column is
    -- the season-to-date frequency of that pitch type as a percentage of
    -- total pitches.  Pitch-type abbreviations: FF=4-seam, SI=sinker,
    -- FC=cutter, SL=slider, ST=sweeper, CU=curveball, KC=knuckle-curve,
    -- CH=changeup, FS=splitter, KN=knuckleball, SV=slurve.
    arsenal_ff_pct          REAL,
    arsenal_si_pct          REAL,
    arsenal_fc_pct          REAL,
    arsenal_sl_pct          REAL,
    arsenal_st_pct          REAL,
    arsenal_cu_pct          REAL,
    arsenal_kc_pct          REAL,
    arsenal_ch_pct          REAL,
    arsenal_fs_pct          REAL,
    arsenal_kn_pct          REAL,
    arsenal_sv_pct          REAL,
    arsenal_dominant_pitch  TEXT,        -- the most-thrown pitch type
    -- Step 15: per-batter sprint + defensive metrics from Savant.
    sprint_speed_fps        REAL,        -- feet per second; ML average ~27
    hp_to_first_sec         REAL,        -- home plate to first base time
    competitive_runs        INTEGER,     -- # of high-effort runs that count
    outs_above_avg          INTEGER,     -- season OAA, can be negative
    fielding_runs_prevented INTEGER,     -- runs saved relative to average
    -- Step 18: per-batter bat-tracking metrics from Savant 2024+ leaderboard.
    -- Bat tracking captures swing decisions + bat speed per swing — orthogonal
    -- to exit-velocity (a swing that misses still has a bat speed).
    avg_bat_speed_mph       REAL,        -- average bat speed across competitive swings
    hard_swing_rate         REAL,        -- % of swings >= 75 mph bat speed
    swing_length_ft         REAL,        -- average bat path length
    squared_up_per_swing    REAL,        -- "squared up" contact rate per swing
    blast_per_swing         REAL,        -- top-tier "blast" contact rate per swing
    swords_count            INTEGER,     -- swings + miss with bat-on-ball expected
    -- Tier 1 D2: per-catcher framing (replaces team-aggregate when the
    -- team's elite framer isn't catching tonight).  NULL for non-catchers.
    -- Sourced from Savant catcher-framing leaderboard by
    -- backfill_catcher_framing.py.
    framing_runs            REAL,
    framing_strike_rate     REAL,
    -- Tier 1 D3: pitcher rest days since last appearance with IP > 0.
    -- Derived from player_game_log by backfill_pitcher_rest.py.  NULL for
    -- batters and for true season-debut starters.
    pitcher_rest_days       INTEGER,
    -- Tier 2 D5: plate discipline metrics from FanGraphs / Savant.
    -- Orthogonal to xwOBA — discipline is FLOOR (will this hitter get on
    -- base when the matchup is hard) where xwOBA is CEILING (will the
    -- contact be quality).
    bb_pct                  REAL,
    k_pct                   REAL,
    o_swing_pct             REAL,        -- swings at pitches outside the zone
    z_contact_pct           REAL,        -- contact rate on pitches in the zone
    sw_str_pct              REAL,        -- swinging-strike rate
    -- Tier 2 D6: BABIP / HR-FB regression flags.  Tells the model "the
    -- surface stats lie" without retraining.
    babip_at_slate          REAL,
    hr_fb_at_slate          REAL,
    babip_regression_flag   INTEGER,     -- 1 if luckier than league norm
    hr_fb_regression_flag   INTEGER,
    -- Tier 2 D8: rolling-window per-handedness OPS splits (last 20 days).
    -- More responsive to hot-streak signal than the season-aggregate splits.
    ops_vs_lhp_last_20      REAL,
    ops_vs_rhp_last_20      REAL,
    -- Tier 2 D9: DFS-site projected ownership.  V14's leverage_factor is
    -- a rule-based predictor; vendor projections are a calibration target.
    dfs_projected_ownership_pct  REAL,
    dfs_projection_source        TEXT,
    -- Tier 3 D10: vendor projected fantasy points (FantasyPros / RotoBaller).
    -- Benchmark, NOT model input — measures whether our scoring engine
    -- agrees with consensus.
    vendor_projected_points       REAL,
    vendor_projection_source      TEXT,
    -- Phase C add (May 2026): pitcher batted-ball profile.  GB / FB / LD
    -- breakdown is the cleanest proxy for "what does the contact look
    -- like when this pitcher gets hit" — extreme GB% suppresses HR risk
    -- in HR-friendly parks, extreme FB% amplifies it.  IFFB% catches
    -- pop-up artists.  Derived from Statcast bb_type field.  NULL for
    -- batters.
    pitcher_gb_pct                REAL,        -- ground balls / batted balls
    pitcher_fb_pct                REAL,        -- fly balls / batted balls
    pitcher_ld_pct                REAL,        -- line drives / batted balls
    pitcher_iffb_pct              REAL,        -- pop-ups / (fly balls + pop-ups)
    -- Phase C add: pitcher recent velocity trend.  fb_velo above is the
    -- season-aggregate; this is (last-3-starts mean) − (season mean).
    -- Negative values flag declining velocity, often preceding IL stints
    -- or blow-up starts.  NULL when fewer than 3 prior starts.
    pitcher_velo_trend_3start     REAL,
    -- Phase E add (May 2026 batter sweep): Times-Through-Order pitcher
    -- splits.  TTO penalty is real and ~25-50 wOBA-points per pass; top-
    -- of-order batters disproportionately face the pitcher in the 1st/2nd
    -- TTO.  Derived from bulk Statcast at_bat_number bucketed (1–9 = TTO1,
    -- 10–18 = TTO2, 19+ = TTO3).
    pitcher_tto1_woba_against     REAL,
    pitcher_tto2_woba_against     REAL,
    pitcher_tto3_woba_against     REAL,
    -- Phase E add: pitcher inning-bucket wOBA-against splits.  Top-of-order
    -- batters face the pitcher in the 1st more than the 4th-6th; mid-order
    -- batters face him later.  Per-pitcher, inning ∈ {1, 2-3, 4-6, 7+}.
    pitcher_woba_inning_1         REAL,
    pitcher_woba_inning_2_3       REAL,
    pitcher_woba_inning_4_6       REAL,
    pitcher_woba_inning_7plus     REAL,
    -- Phase E add: player rest / consecutive starts.  Derived from
    -- player_game_log.  Pitchers and batters both populated.
    player_consecutive_starts     INTEGER,
    player_days_since_rest        INTEGER,
    -- Phase E add: career BvP wOBA with Bayesian shrinkage toward league
    -- rate.  bvp_pa_count_vs_starter is the raw PA sample size for the
    -- batter vs his game's starter prior to slate_date; bvp_woba_vs_starter
    -- is the shrunken estimate.  NULL for pitcher rows.
    bvp_woba_vs_starter           REAL,
    bvp_pa_count_vs_starter       INTEGER,
    -- Phase E add: is_fresh_off_il — 1 when an IL-return transaction
    -- (typeCode='RTN', 'STA' returning to active) landed within 7 days
    -- prior to slate_date.  Derived from label_event(label_type='transaction').
    is_fresh_off_il               INTEGER,
    -- Phase E add: rolling 30-day BABIP minus season-aggregate BABIP.
    -- Captures luck regression independently of the absolute BABIP.
    babip_delta_30day             REAL,
    PRIMARY KEY (slate_date, mlb_id)
    -- game_pk is informational only; player_slate cannot foreign-key to
    -- slate_game because the latter's PK includes game_number (doubleheader
    -- support) which player_slate has no way to disambiguate.
);
CREATE INDEX IF NOT EXISTS idx_player_slate_mlb_id ON player_slate(mlb_id);
CREATE INDEX IF NOT EXISTS idx_player_slate_team ON player_slate(slate_date, team);

CREATE TABLE IF NOT EXISTS player_game_log (
    -- Implicit `rowid` is the primary key.  We do NOT add a (slate_date,
    -- mlb_id, game_date) UNIQUE constraint because the historical CSV had
    -- 63 duplicate rows for that triple — 35 of them with identical values
    -- (harmless), 28 with conflicting box-score values (data-quality bug
    -- from a backfill that ran twice for some games).  Preserving the dups
    -- keeps calibration byte-identical with the CSV-era audit harness; the
    -- inflight harness already handles the conflict by "last-row-wins" via
    -- dict insertion order, and our export reproduces that order via
    -- ingest sequence.
    rowid_seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    slate_date    TEXT NOT NULL,
    mlb_id        INTEGER NOT NULL,
    game_date     TEXT NOT NULL,
    player_name   TEXT,
    team          TEXT,
    position      TEXT,
    opponent      TEXT,
    is_home       INTEGER,
    ab            INTEGER,
    runs          INTEGER,
    hits          INTEGER,
    hr            INTEGER,
    rbi           INTEGER,
    bb            INTEGER,
    so            INTEGER,
    sb            INTEGER,
    ip            REAL,
    er            INTEGER,
    k_pitching    INTEGER,
    decision      TEXT
);
CREATE INDEX IF NOT EXISTS idx_player_game_log_key
    ON player_game_log(slate_date, mlb_id, game_date);
CREATE INDEX IF NOT EXISTS idx_player_game_log_game_date ON player_game_log(game_date);
CREATE INDEX IF NOT EXISTS idx_player_game_log_mlb_id  ON player_game_log(mlb_id);

CREATE TABLE IF NOT EXISTS label_event (
    slate_date   TEXT NOT NULL,
    mlb_id       INTEGER NOT NULL,
    label_type   TEXT NOT NULL,
    label_value  REAL,
    label_text   TEXT,
    source       TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    PRIMARY KEY (slate_date, mlb_id, label_type, source)
);
CREATE INDEX IF NOT EXISTS idx_label_event_type   ON label_event(label_type);
CREATE INDEX IF NOT EXISTS idx_label_event_player ON label_event(slate_date, mlb_id);
CREATE INDEX IF NOT EXISTS idx_label_event_date_type ON label_event(slate_date, label_type);

-- Side table: per-player slowly-changing dimensions.  One row per mlb_id.
-- Populated by scripts/backfill_player_externals.py from the MLB Stats API
-- /people endpoint.  Replaces the 8 per-slate snapshot columns that lived
-- on player_slate before the May 2026 cleanup sweep — same data, ~40×
-- less storage.  first_observed_date / last_observed_date track the slate-
-- date range we've seen the row covering; if a value drifts (rare — trade
-- changes primary_position_code occasionally; mid-season weight refresh
-- updates weight_lb), the latest backfill run wins.
CREATE TABLE IF NOT EXISTS player_dim (
    mlb_id                 INTEGER PRIMARY KEY,
    bat_side               TEXT,         -- 'R' / 'L' / 'S' (switch)
    pitch_hand             TEXT,         -- 'R' / 'L' (pitchers only)
    birth_date             TEXT,         -- ISO date — fixed once known
    birth_country          TEXT,         -- fixed once known
    mlb_debut_date         TEXT,         -- ISO date — fixed once set
    height_in              INTEGER,      -- inches — slow drift on offseason refresh
    weight_lb              INTEGER,      -- pounds — slow drift on offseason refresh
    primary_position_code  TEXT,         -- '1B' / 'C' / 'SS' / 'OF' / 'SP' / etc.
    first_observed_date    TEXT,         -- earliest slate_date this player appeared
    last_observed_date     TEXT,         -- latest slate_date this player appeared
    observed_at            TEXT NOT NULL -- ISO timestamp of the latest backfill
);
CREATE INDEX IF NOT EXISTS idx_player_dim_position
    ON player_dim(primary_position_code);

-- May 2026 Phase D: per-venue slowly-changing dimensions.  Lifted off
-- slate_game where they were duplicated 80+ times per venue per season.
-- venue_id is the join key.  Capacity / surface / roof / dimensions
-- update once or twice per offseason; mid-season changes (Athletics
-- moving to Sacramento) get the new row on next backfill_game_externals
-- run.
CREATE TABLE IF NOT EXISTS venue_dim (
    venue_id            INTEGER PRIMARY KEY,
    venue_name          TEXT,
    venue_capacity      INTEGER,
    venue_surface       TEXT,        -- 'Grass' / 'Turf'
    venue_roof_type     TEXT,        -- 'Open' / 'Dome' / 'Retractable Roof'
    venue_elevation_ft  INTEGER,
    venue_latitude      REAL,
    venue_longitude     REAL,
    venue_timezone      TEXT,
    venue_lf_line_ft    INTEGER,
    venue_lf_ft         INTEGER,
    venue_lcf_ft        INTEGER,
    venue_cf_ft         INTEGER,
    venue_rcf_ft        INTEGER,
    venue_rf_ft         INTEGER,
    venue_rf_line_ft    INTEGER,
    -- Phase E add: stadium orientation as the compass azimuth from home
    -- plate looking toward center field.  0° = N, 90° = E, 180° = S, 270° = W.
    -- Used to project wind direction onto the HP→pull-field axis for the
    -- per-handedness wind-assist features on slate_game.
    hp_to_cf_azimuth_deg REAL,
    observed_at         TEXT NOT NULL
);

-- Tier 3 D11: Win-Probability-Added (WPA) per HV player game.  Separates
-- high-leverage HV (1-run game in the 9th, repeatable) from volume HV
-- (blowout in the 3rd, luck-driven).  Stored as label_event(label_type='wpa').
-- (No new table — uses the existing label_event store.)

-- Tier 3 D12: per-batted-ball Statcast for HV games.  Lets calibration
-- ask "did this HV pop come from quality of contact (sustainable) or
-- BABIP luck (one-off bloop)?".  HV-only by default to keep size sane;
-- backfill_statcast_pa.py can run with --all-games for a fuller corpus.
CREATE TABLE IF NOT EXISTS statcast_pa (
    slate_date              TEXT NOT NULL,
    mlb_id                  INTEGER NOT NULL,
    game_date               TEXT NOT NULL,
    pa_index                INTEGER NOT NULL,
    exit_velocity_mph       REAL,
    launch_angle_deg        REAL,
    hit_distance_ft         REAL,
    x_woba                  REAL,
    pitch_type              TEXT,
    result                  TEXT,
    observed_at             TEXT NOT NULL,
    PRIMARY KEY (slate_date, mlb_id, game_date, pa_index)
);
CREATE INDEX IF NOT EXISTS idx_statcast_pa_mlb_id
    ON statcast_pa(mlb_id);

-- Tier 3 D13: pitcher pitch-arsenal × batter pitch-type wOBA crosstab.
-- Replaces V10.8's "simplified xwOBA-against single number" approach in
-- score_batter_matchup with a per-pitch-type weighted blend.  Sourced
-- from Savant per-batter pitch-type splits by
-- backfill_batter_pitch_type_splits.py.
CREATE TABLE IF NOT EXISTS batter_pitch_type_woba (
    slate_date    TEXT NOT NULL,
    mlb_id        INTEGER NOT NULL,
    pitch_type    TEXT NOT NULL,
    pa_count      INTEGER,
    woba          REAL,
    observed_at   TEXT NOT NULL,
    PRIMARY KEY (slate_date, mlb_id, pitch_type)
);
CREATE INDEX IF NOT EXISTS idx_bptwoba_mlb_id
    ON batter_pitch_type_woba(mlb_id);

-- Side table: alias rows used to recover identity for HV box-score players
-- whose canonical name does not match historical_player_game_logs.csv.  Empty
-- by default; populated only when the build script encounters a name that
-- needs an mlb_id alias.
CREATE TABLE IF NOT EXISTS player_alias (
    name_normalized TEXT NOT NULL,
    team            TEXT NOT NULL,
    mlb_id          INTEGER NOT NULL,
    source          TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (name_normalized, team)
);
"""


# ---------------------------------------------------------------------------
# Label-type vocabulary (audit Section F)
# ---------------------------------------------------------------------------
# Numeric scalar labels — label_value populated, label_text null.
# total_value, avg_draft_slot, avg_draft_mult, avg_draft_tv, highest_draft_tv
# dropped — all derivable from real_score × (2 + card_boost) and the per-lineup
# `winning_lineup_slot` rows respectively.  Aggregates are recomputed on export.
LABEL_TYPES_NUMERIC = (
    "real_score",
    "card_boost",
    "drafts",
    "draft_count",
    # Note: `total_mult` is not a standalone label_type — it's encoded
    # inside winning_lineup_slot.label_text JSON alongside rank/slot/etc.
    # Note: `wpa` is also a valid label_type emitted by scripts/backfill_wpa.py
    # (Tier 3 D11) but is not in this tuple because it isn't part of the
    # CSV-ingest path; the corpus may have zero `wpa` rows until the backfill
    # runs.  The label_event PK accepts any label_type string.
)

# Boolean-flag leaderboard memberships — label_value=1.0 when the player landed
# on the leaderboard for that slate; absence of a row means "not on it".
LABEL_TYPES_FLAG = (
    "highest_value",
    "most_popular",
    "most_drafted_3x",
)

# Categorical / ordinal — label_text populated, label_value optionally too.
# `most_common_slot` dropped — derivable from `winning_lineup_slot` rows.
LABEL_TYPES_CATEGORICAL = (
    "injury_status",
    "winning_lineup_slot",
    "box_score",
)

LABEL_TYPES_ALL = LABEL_TYPES_NUMERIC + LABEL_TYPES_FLAG + LABEL_TYPES_CATEGORICAL

# Sources we currently emit — used by the auditor + the export step to know
# which (label_type, source) tuples produce CSV columns.
SOURCE_REALSPORTS_STATS = "realsports_stats"
SOURCE_REALSPORTS_ENTRIES = "realsports_entries"
SOURCE_MLB_BOXSCORE = "mlb_boxscore"
SOURCE_BACKFILL_RICH = "backfill_rich_stats"
SOURCE_BACKFILL_CARD_BOOST = "backfill_card_boost_and_drafts"
SOURCE_INITIAL_BUILD = "initial_csv_ingest"


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open a read-write connection to the historical store.

    Caller is responsible for `commit()` / `close()`.  WAL is enabled for
    concurrent-reader safety even when calibration scripts run alongside the
    daily writer.
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_readonly(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open a read-only connection.  Used by audit / calibration / runtime
    paths that must never mutate the corpus.

    Note: SQLite requires the file to already exist for `mode=ro`.  Tests that
    construct fresh DBs should use `connect()` instead.
    """
    path = resolve_db_path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"historical_db.connect_readonly: {path} does not exist.  "
            "Run scripts/build_historical_db.py to build the corpus."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Idempotently install the full schema.  Safe to call against an existing
    DB — every CREATE uses IF NOT EXISTS."""
    conn.executescript(SCHEMA_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# Idempotent upsert helpers — used by the daily writer (Step 2), the
# backfill scripts (Step 3), and the audit/calibration readers (Step 4).
# ---------------------------------------------------------------------------
def upsert_slate(conn: sqlite3.Connection, row: dict) -> None:
    cols = ("slate_date", "game_count", "num_brawlers", "season_stage",
            "source", "saved_at", "notes")
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO slate ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def upsert_slate_game(conn: sqlite3.Connection, row: dict) -> None:
    """INSERT OR REPLACE on (slate_date, game_pk).  Caller passes a dict with
    any subset of the slate_game columns; missing columns become NULL on
    insert.  For partial backfills, prefer `update_slate_game_columns`."""
    cols = list(_table_columns(conn, "slate_game"))
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO slate_game ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def update_slate_game_columns(
    conn: sqlite3.Connection,
    slate_date: str,
    game_pk: int,
    updates: dict,
) -> None:
    """Surgical update of a subset of slate_game columns.  Used by the backfills
    that enrich existing rows (env conditions, handedness, V10.8 signals)
    without disturbing fields populated by other backfills."""
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = tuple(updates.values()) + (slate_date, game_pk)
    conn.execute(
        f"UPDATE slate_game SET {set_clause} WHERE slate_date = ? AND game_pk = ?",
        params,
    )


def upsert_player_slate(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(_table_columns(conn, "player_slate"))
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO player_slate ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def update_player_slate_columns(
    conn: sqlite3.Connection,
    slate_date: str,
    mlb_id: int,
    updates: dict,
) -> None:
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = tuple(updates.values()) + (slate_date, mlb_id)
    conn.execute(
        f"UPDATE player_slate SET {set_clause} WHERE slate_date = ? AND mlb_id = ?",
        params,
    )


def upsert_player_game_log(conn: sqlite3.Connection, row: dict) -> None:
    """Append a player_game_log row.  The table has no uniqueness constraint
    on (slate_date, mlb_id, game_date) — historical duplicates are preserved
    so calibration outputs remain byte-identical with the CSV era.  Callers
    that want idempotent re-runs should DELETE rows first by their composite
    key, or use `replace_player_game_log_by_key()` (Step 3 helper)."""
    cols = [c for c in _table_columns(conn, "player_game_log") if c != "rowid_seq"]
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT INTO player_game_log ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(row.get(c) for c in cols),
    )


def replace_player_game_log_for_slate(
    conn: sqlite3.Connection, slate_date: str
) -> None:
    """Delete every player_game_log row for `slate_date`.  Backfills call
    this before re-inserting to maintain idempotency without the (slate_date,
    mlb_id, game_date) PK.  Cheap (indexed delete)."""
    conn.execute("DELETE FROM player_game_log WHERE slate_date = ?", (slate_date,))


def upsert_label_event(
    conn: sqlite3.Connection,
    *,
    slate_date: str,
    mlb_id: int,
    label_type: str,
    label_value: float | None = None,
    label_text: str | None = None,
    source: str,
    observed_at: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO label_event "
        "(slate_date, mlb_id, label_type, label_value, label_text, source, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (slate_date, mlb_id, label_type, label_value, label_text, source, observed_at),
    )


def upsert_player_dim(
    conn: sqlite3.Connection,
    *,
    mlb_id: int,
    bat_side: str | None = None,
    pitch_hand: str | None = None,
    birth_date: str | None = None,
    birth_country: str | None = None,
    mlb_debut_date: str | None = None,
    height_in: int | None = None,
    weight_lb: int | None = None,
    primary_position_code: str | None = None,
    first_observed_date: str | None = None,
    last_observed_date: str | None = None,
    observed_at: str,
) -> None:
    """Upsert a `player_dim` row.

    On conflict, COALESCE preserves any non-NULL value already in place
    (no_op replacement of an existing value with NULL) but always advances
    `observed_at` and `last_observed_date`, and lowers `first_observed_date`
    if we've seen the player on an earlier slate than the existing row.
    """
    conn.execute(
        """
        INSERT INTO player_dim (
            mlb_id, bat_side, pitch_hand, birth_date, birth_country,
            mlb_debut_date, height_in, weight_lb, primary_position_code,
            first_observed_date, last_observed_date, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mlb_id) DO UPDATE SET
            bat_side             = COALESCE(excluded.bat_side, bat_side),
            pitch_hand           = COALESCE(excluded.pitch_hand, pitch_hand),
            birth_date           = COALESCE(excluded.birth_date, birth_date),
            birth_country        = COALESCE(excluded.birth_country, birth_country),
            mlb_debut_date       = COALESCE(excluded.mlb_debut_date, mlb_debut_date),
            height_in            = COALESCE(excluded.height_in, height_in),
            weight_lb            = COALESCE(excluded.weight_lb, weight_lb),
            primary_position_code = COALESCE(excluded.primary_position_code, primary_position_code),
            first_observed_date  = MIN(COALESCE(excluded.first_observed_date, first_observed_date),
                                        COALESCE(first_observed_date, excluded.first_observed_date)),
            last_observed_date   = MAX(COALESCE(excluded.last_observed_date, last_observed_date),
                                        COALESCE(last_observed_date, excluded.last_observed_date)),
            observed_at          = excluded.observed_at
        """,
        (
            mlb_id, bat_side, pitch_hand, birth_date, birth_country,
            mlb_debut_date, height_in, weight_lb, primary_position_code,
            first_observed_date, last_observed_date, observed_at,
        ),
    )


def upsert_player_alias(
    conn: sqlite3.Connection,
    *,
    name_normalized: str,
    team: str,
    mlb_id: int,
    source: str,
    observed_at: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO player_alias "
        "(name_normalized, team, mlb_id, source, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name_normalized, team, mlb_id, source, observed_at),
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Read helpers — thin wrappers; keep query strings centralised.
# ---------------------------------------------------------------------------
def fetch_player_slate_rows(
    conn: sqlite3.Connection,
    slate_date: str | None = None,
) -> list[sqlite3.Row]:
    """Return all player_slate rows, optionally filtered to one slate_date."""
    if slate_date is None:
        cur = conn.execute(
            "SELECT * FROM player_slate ORDER BY slate_date, mlb_id"
        )
    else:
        cur = conn.execute(
            "SELECT * FROM player_slate WHERE slate_date = ? ORDER BY mlb_id",
            (slate_date,),
        )
    return cur.fetchall()


def fetch_label_value(
    conn: sqlite3.Connection,
    slate_date: str,
    mlb_id: int,
    label_type: str,
) -> tuple[float | None, str | None] | None:
    """Return (label_value, label_text) for the matching row, or None.

    When multiple sources have written the same label_type for the same
    (slate_date, mlb_id), the most-recently observed wins.
    """
    cur = conn.execute(
        "SELECT label_value, label_text FROM label_event "
        "WHERE slate_date = ? AND mlb_id = ? AND label_type = ? "
        "ORDER BY observed_at DESC LIMIT 1",
        (slate_date, mlb_id, label_type),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def has_label(
    conn: sqlite3.Connection,
    slate_date: str,
    mlb_id: int,
    label_type: str,
) -> bool:
    """True if any label of the given type exists for (slate_date, mlb_id)."""
    cur = conn.execute(
        "SELECT 1 FROM label_event "
        "WHERE slate_date = ? AND mlb_id = ? AND label_type = ? LIMIT 1",
        (slate_date, mlb_id, label_type),
    )
    return cur.fetchone() is not None


def rebuild_from_csvs_and_export(
    *, db_path: str | os.PathLike | None = None,
    out_dir: str | os.PathLike | None = None,
) -> None:
    """Re-ingest the canonical store from the on-disk CSV/JSON files and
    refresh the derived exports.  Called at the end of every Step-3 backfill
    script so the DB and CSVs stay in sync without each backfill needing
    bespoke per-table SQLite writes.

    Pre-condition: the backfill has just written its updated CSV/JSON to /data/.
    Post-condition: data/historical.db is rebuilt from those CSVs, and the
    CSVs are re-exported in canonical form (column order, sort order,
    formatting) so subsequent backfills see byte-stable inputs.
    """
    import subprocess
    import sys
    repo_root = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        str(repo_root / "scripts" / "build_historical_db.py"),
        "--rebuild",
    ]
    if db_path is not None:
        cmd += ["--db", str(db_path)]
    env = {**os.environ}
    env.setdefault("BO_CURRENT_SEASON", "2026")
    env.setdefault("BO_ODDS_API_KEY", "backfill-rebuild-stub")
    subprocess.run(cmd, check=True, env=env, cwd=str(repo_root))

    cmd = [
        sys.executable,
        str(repo_root / "scripts" / "export_historical_csvs.py"),
    ]
    if db_path is not None:
        cmd += ["--db", str(db_path)]
    if out_dir is not None:
        cmd += ["--out-dir", str(out_dir)]
    subprocess.run(cmd, check=True, env=env, cwd=str(repo_root))


def fetch_most_popular_index(
    conn: sqlite3.Connection,
    *,
    cutoff_inclusive: str,
    as_of_exclusive: str,
) -> list[sqlite3.Row]:
    """Read the rolling most_popular fame index used by app/core/popularity.py
    after Step 5.  Returns one row per (slate_date, mlb_id) appearance with
    the player's name, team, and a 0/1 most_popular flag.

    Window: cutoff_inclusive <= slate_date < as_of_exclusive.

    Semantics match the CSV-era _load_fame_rate_index byte-for-byte: the
    denominator counts every player_slate row within the window (each row
    is one leaderboard appearance — the CSV had no rows for non-leaderboard
    players); the numerator counts the subset with a `most_popular`
    label_event row.

    Returns sqlite3.Row objects with columns: slate_date, mlb_id,
    player_name, team, is_most_popular.
    """
    cur = conn.execute(
        """
        SELECT
            ps.slate_date,
            ps.mlb_id,
            ps.player_name,
            ps.team,
            CASE WHEN mp.mlb_id IS NOT NULL THEN 1 ELSE 0 END AS is_most_popular
        FROM player_slate ps
        LEFT JOIN (
            SELECT DISTINCT slate_date, mlb_id
            FROM label_event
            WHERE label_type = 'most_popular'
              AND slate_date >= ?
              AND slate_date < ?
        ) AS mp
          ON mp.slate_date = ps.slate_date AND mp.mlb_id = ps.mlb_id
        WHERE ps.slate_date >= ?
          AND ps.slate_date < ?
        ORDER BY ps.slate_date, ps.mlb_id
        """,
        (cutoff_inclusive, as_of_exclusive, cutoff_inclusive, as_of_exclusive),
    )
    return cur.fetchall()
