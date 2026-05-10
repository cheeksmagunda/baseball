"""Invariance tests: mechanical defenses for the project's ABSOLUTE rules.

These tests exist to catch regressions that ruff and the static
`audit_live_isolation.py` cannot. Unlike unit tests that verify a specific
output, each test here pins down a *property* the system must always have:

1. Signal isolation — `card_boost`, `drafts`, and `popularity` must NEVER
   appear on FilteredCandidate (CLAUDE.md § "Signal Isolation: ABSOLUTE
   RULE").  Banned at the dataclass-signature level so EV logic is
   structurally incapable of reading them.
2. Schema contract — FilteredCandidate must not declare any of the banned
   historical-outcome OR in-draft dynamic fields.
3. Constant-perturbation rank stability — perturbing each env/trait
   threshold in `app/core/constants.py` by ±10% on a synthetic pool must not
   catastrophically reorder the ranking. Catches brittle cliff-thresholds.
   Note: the fixture is fully synthetic — no historical outcome is read, so
   this is SAFE under the "no automated calibration" rule.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from app.services.filter_strategy import (
    FilteredCandidate,
    _compute_base_ev,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BANNED_OUTCOME_FIELDS = {
    "real_score",
    "total_value",
    "is_highest_value",
    "is_most_popular",
    "is_most_drafted_3x",
    # In-draft dynamic signals — unknowable pre-game.
    "card_boost",
    "drafts",
    # V11.0: popularity removed from the optimizer entirely.
    "popularity",
    "sharp_score",
}


def _make_candidate(
    *,
    name: str = "Test Player",
    team: str = "NYY",
    is_pitcher: bool = False,
    total_score: float = 50.0,
    env_score: float = 0.6,
    batting_order: int | None = 3,
    env_unknown_count: int = 0,
    game_id: int | str | None = 1,
) -> FilteredCandidate:
    return FilteredCandidate(
        player_name=name,
        team=team,
        position="SP" if is_pitcher else "OF",
        total_score=total_score,
        env_score=env_score,
        env_unknown_count=env_unknown_count,
        game_id=game_id,
        is_pitcher=is_pitcher,
        batting_order=batting_order if not is_pitcher else None,
    )


def _kendall_tau(a: list[str], b: list[str]) -> float:
    """Compute Kendall's tau rank correlation between two orderings of the same items.

    Returns 1.0 for identical ordering, -1.0 for reverse ordering, 0.0 for
    random. Implemented without scipy so the test stays dependency-free.
    """
    if set(a) != set(b):
        raise ValueError("Kendall-tau requires the same item set in both orderings.")
    pos_b = {x: i for i, x in enumerate(b)}
    concordant = 0
    discordant = 0
    n = len(a)
    for i in range(n):
        for j in range(i + 1, n):
            order_a = a[i], a[j]
            order_b_pos = pos_b[order_a[0]], pos_b[order_a[1]]
            if order_b_pos[0] < order_b_pos[1]:
                concordant += 1
            else:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else 1.0


# ---------------------------------------------------------------------------
# 1. Signal isolation — banned fields must never accept a value
# ---------------------------------------------------------------------------


def test_banned_field_lists_consistent():
    """The banned-field set is enforced by two complementary tools:

      * tests/test_invariants.py (BANNED_OUTCOME_FIELDS) — runtime structural
        guard: FilteredCandidate's dataclass rejects these as constructor
        args, so they cannot enter EV.
      * scripts/audit_live_isolation.py (BANNED_FIELDS) — static grep guard:
        scans app/services + app/routers + app/core for any literal `.field`
        access on these names.  Strict-mode (May 2026) extends this to also
        flag dead-fallback constants (DEFAULT_*, UNKNOWN_SCORE_RATIO,
        DNP_*_PENALTY) that we never want reintroduced.

    Audit can be a SUPERSET of the dataclass-rejected set: every dataclass
    rejection must be audited, but the audit can flag additional symbols
    that aren't dataclass fields.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from scripts.audit_live_isolation import BANNED_FIELDS as audit_banned

    missing_from_audit = BANNED_OUTCOME_FIELDS - set(audit_banned)
    assert not missing_from_audit, (
        f"audit script is missing fields the dataclass rejects: "
        f"{missing_from_audit} — add them to BANNED_FIELDS in audit script"
    )


class TestSignalIsolation:
    """V11.0: card_boost, drafts, popularity, sharp_score do not exist on
    FilteredCandidate at all.  Passing any of these to the dataclass
    constructor raises TypeError, making it structurally impossible for EV
    logic to read them.
    """

    def test_constructor_rejects_card_boost(self):
        with pytest.raises(TypeError):
            FilteredCandidate(
                player_name="x", team="NYY", position="OF",
                total_score=50.0, env_score=0.5, card_boost=1.5,  # type: ignore[call-arg]
            )

    def test_constructor_rejects_drafts(self):
        with pytest.raises(TypeError):
            FilteredCandidate(
                player_name="x", team="NYY", position="OF",
                total_score=50.0, env_score=0.5, drafts=500,  # type: ignore[call-arg]
            )

    def test_constructor_rejects_popularity(self):
        with pytest.raises(TypeError):
            FilteredCandidate(
                player_name="x", team="NYY", position="OF",
                total_score=50.0, env_score=0.5, popularity="FADE",  # type: ignore[call-arg]
            )

    def test_constructor_rejects_sharp_score(self):
        with pytest.raises(TypeError):
            FilteredCandidate(
                player_name="x", team="NYY", position="OF",
                total_score=50.0, env_score=0.5, sharp_score=80.0,  # type: ignore[call-arg]
            )

    def test_predicted_ownership_score_is_allowed(self):
        """V15: predicted_ownership_score is a continuous float in [0, 10]
        produced from public pre-game observables (team market, fame,
        batting order, season stats).  It is explicitly NOT card_boost,
        NOT a raw drafts count, and NOT an outcome label.
        STRATEGY_AUDIT_2026-05.md carves it out as analogous to using
        prior-season ERA — backward-looking aggregate of publicly visible
        facts, not leakage of the current slate's outcome.
        """
        c = FilteredCandidate(
            player_name="x", team="NYY", position="OF",
            total_score=50.0, env_score=0.5,
            predicted_ownership_score=0.5,
        )
        assert c.predicted_ownership_score == 0.5
        # Confirm it's a numeric score, never a count or outcome label.
        assert isinstance(c.predicted_ownership_score, float)


# ---------------------------------------------------------------------------
# 2. Schema contract — FilteredCandidate must not declare banned fields
# ---------------------------------------------------------------------------

class TestSchemaContract:
    """The static audit in scripts/audit_live_isolation.py catches runtime
    attribute reads. This contract test catches the upstream case: someone
    adding a banned field to the FilteredCandidate dataclass itself.
    """

    def test_filtered_candidate_excludes_banned_outcome_fields(self):
        fields = {f.name for f in dataclasses.fields(FilteredCandidate)}
        leaked = fields & BANNED_OUTCOME_FIELDS
        assert not leaked, (
            f"FilteredCandidate declares banned fields: {leaked}. "
            "Historical outcome data, in-draft dynamic signals "
            "(card_boost, drafts), and popularity (V11.0) are never "
            "allowed in the live EV path."
        )

    def test_slate_player_does_not_have_team_column(self):
        """SlatePlayer.team must not exist — team lives on the related Player.

        Regression: pipeline code that wants the team for a SlatePlayer must
        go through `sp.player.team`, not `sp.team`. A `team` column on
        SlatePlayer would mask AttributeError at the call site (which
        previously fired in the rookie-track warning path) and let stale
        denormalised values drift from Player.team. Keep the relationship as
        the single source of truth.
        """
        from sqlalchemy import inspect as sa_inspect
        from app.models.slate import SlatePlayer
        cols = {c.name for c in sa_inspect(SlatePlayer).columns}
        assert "team" not in cols, (
            "SlatePlayer.team must not exist as a column — code that needs "
            "the team should resolve via `sp.player.team` (or join Player "
            "explicitly)."
        )


# ---------------------------------------------------------------------------
# Step 6: pin the historical-corpus SQLite schema and the derived-CSV column
# contracts.  After Step 2, data/historical.db is the canonical store and the
# CSVs in /data/ are byte-stable derived exports.  These tests catch:
#   * accidental schema drift on rebuild (e.g. a backfill adds a column to a
#     new table without updating SCHEMA_DDL)
#   * accidental export-column drift (e.g. the union-merge logic in
#     scripts/export_historical_csvs.py loses a column)
# ---------------------------------------------------------------------------

class TestHistoricalSqliteSchemaContract:
    """Pin the schema of data/historical.db.  Any deliberate addition of a
    column requires updating EXPECTED_COLUMNS — a forcing function that the
    DDL change has been thought through."""

    EXPECTED_TABLES = {
        "slate", "slate_game", "player_slate", "player_game_log",
        "label_event", "player_alias",
    }

    EXPECTED_COLUMNS = {
        "slate": {
            "slate_date", "game_count", "num_brawlers",
            "season_stage", "source", "saved_at", "notes",
        },
        "slate_game": {
            "slate_date", "game_pk", "game_number", "home_team", "away_team",
            "home_starter_id", "home_starter_name", "home_starter_hand",
            "home_starter_era", "home_starter_whip", "home_starter_k_per_9",
            "home_starter_x_era", "home_starter_x_woba_against",
            "away_starter_id", "away_starter_name", "away_starter_hand",
            "away_starter_era", "away_starter_whip", "away_starter_k_per_9",
            "away_starter_x_era", "away_starter_x_woba_against",
            "home_team_ops", "home_team_k_pct", "home_bullpen_era",
            "home_team_framing_runs", "home_team_framing_pct",
            "away_team_ops", "away_team_k_pct", "away_bullpen_era",
            "away_team_framing_runs", "away_team_framing_pct",
            "home_team_record_w", "home_team_record_l", "home_team_rest_days",
            "away_team_record_w", "away_team_record_l", "away_team_rest_days",
            "home_l10_wins", "home_series_wins",
            "away_l10_wins", "away_series_wins",
            "vegas_total", "home_moneyline", "away_moneyline",
            "park_team", "park_hr_factor",
            "temperature_f", "wind_speed_mph",
            "wind_direction", "wind_direction_deg", "datetime_utc",
            "home_score", "away_score", "winner", "loser",
            "winner_score", "loser_score",
            # Step 9: external game-info statics from MLB Stats API live feed
            "attendance", "game_duration_minutes", "day_night", "weather_condition",
            "venue_id", "venue_name", "venue_capacity", "venue_surface",
            "venue_roof_type", "venue_elevation_ft", "venue_latitude",
            "venue_longitude", "venue_timezone",
            "venue_lf_line_ft", "venue_lf_ft", "venue_lcf_ft", "venue_cf_ft",
            "venue_rcf_ft", "venue_rf_ft", "venue_rf_line_ft",
            "ump_hp_id", "ump_hp_name", "ump_1b_id", "ump_2b_id", "ump_3b_id",
            "home_catcher_id", "away_catcher_id",
            # Step 10: per-pitcher boxscore detail (post-game external)
            "home_starter_pitch_count", "home_starter_outs_recorded",
            "home_starter_hits_allowed", "home_starter_runs_allowed",
            "home_starter_er_allowed", "home_starter_walks",
            "home_starter_strikeouts", "home_starter_hr_allowed",
            "away_starter_pitch_count", "away_starter_outs_recorded",
            "away_starter_hits_allowed", "away_starter_runs_allowed",
            "away_starter_er_allowed", "away_starter_walks",
            "away_starter_strikeouts", "away_starter_hr_allowed",
            "home_bullpen_pitchers_used", "home_bullpen_outs_recorded",
            "home_bullpen_pitch_count",
            "away_bullpen_pitchers_used", "away_bullpen_outs_recorded",
            "away_bullpen_pitch_count",
            # Step 13: actual weather at first pitch from Open-Meteo Archive
            "actual_temperature_f", "actual_wind_speed_mph",
            "actual_wind_direction_deg", "actual_precipitation_mm",
            "actual_humidity_pct", "actual_pressure_hpa",
            "actual_cloud_cover_pct",
            # Step 14: per-team post-game box-score totals
            "innings_played",
            "home_team_hits", "home_team_runs", "home_team_doubles",
            "home_team_triples", "home_team_hr",
            "home_team_walks", "home_team_strikeouts",
            "home_team_left_on_base", "home_team_stolen_bases",
            "home_team_errors",
            "away_team_hits", "away_team_runs", "away_team_doubles",
            "away_team_triples", "away_team_hr",
            "away_team_walks", "away_team_strikeouts",
            "away_team_left_on_base", "away_team_stolen_bases",
            "away_team_errors",
            # Step 16: as-of-slate-date team standings snapshot
            "home_team_games_back", "home_team_runs_scored",
            "home_team_runs_allowed", "home_team_run_differential",
            "home_team_streak", "home_team_division_rank",
            "home_team_league_rank", "home_team_home_record",
            "home_team_away_record", "home_team_winning_pct",
            "away_team_games_back", "away_team_runs_scored",
            "away_team_runs_allowed", "away_team_run_differential",
            "away_team_streak", "away_team_division_rank",
            "away_team_league_rank", "away_team_home_record",
            "away_team_away_record", "away_team_winning_pct",
            # Step 17: per-team mound visits + ABS challenges
            "home_mound_visits_used", "away_mound_visits_used",
            "home_abs_challenges_used", "home_abs_challenges_won",
            "away_abs_challenges_used", "away_abs_challenges_won",
        },
        "player_slate": {
            "slate_date", "mlb_id", "player_name", "team", "position",
            "game_pk",
            "ops_at_slate", "iso_at_slate",
            "era_at_slate", "whip_at_slate", "k9_at_slate",
            "ops_vs_lhp_at_slate", "ops_vs_rhp_at_slate",
            "batting_order_at_slate",
            "x_woba", "x_ba", "x_slg",
            "avg_ev", "hard_hit_pct", "barrel_pct", "max_ev",
            "x_era", "x_woba_against",
            "fb_velo", "whiff_pct", "chase_pct",
            "fb_ivb", "fb_extension",
            # Step 11: per-player externals from MLB people endpoint
            "bat_side", "pitch_hand", "birth_date", "mlb_debut_date",
            "height_in", "weight_lb", "birth_country",
            "primary_position_code", "jersey_number",
            # Step 12: pitcher pitch-arsenal usage % from Savant
            "arsenal_ff_pct", "arsenal_si_pct", "arsenal_fc_pct",
            "arsenal_sl_pct", "arsenal_st_pct", "arsenal_cu_pct",
            "arsenal_kc_pct", "arsenal_ch_pct", "arsenal_fs_pct",
            "arsenal_kn_pct", "arsenal_sv_pct", "arsenal_dominant_pitch",
            # Step 15: per-batter sprint + defensive metrics from Savant
            "sprint_speed_fps", "hp_to_first_sec", "competitive_runs",
            "outs_above_avg", "fielding_runs_prevented",
            # Step 18: per-batter bat-tracking metrics from Savant
            "avg_bat_speed_mph", "hard_swing_rate", "swing_length_ft",
            "squared_up_per_swing", "blast_per_swing", "swords_count",
        },
        "player_game_log": {
            "rowid_seq", "slate_date", "mlb_id", "game_date",
            "player_name", "team", "position", "opponent", "is_home",
            "ab", "runs", "hits", "hr", "rbi", "bb", "so", "sb",
            "ip", "er", "k_pitching", "decision",
        },
        "label_event": {
            "slate_date", "mlb_id", "label_type",
            "label_value", "label_text", "source", "observed_at",
        },
        "player_alias": {
            "name_normalized", "team", "mlb_id", "source", "observed_at",
        },
    }

    def _connect(self):
        from app.core import historical_db
        return historical_db.connect_readonly()

    def test_all_expected_tables_exist(self):
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
            actual = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        missing = self.EXPECTED_TABLES - actual
        unexpected = actual - self.EXPECTED_TABLES
        assert not missing, f"missing tables: {missing}"
        assert not unexpected, (
            f"unexpected new tables: {unexpected}.  Update EXPECTED_TABLES "
            "if intentional."
        )

    def test_table_columns_match_expected(self):
        conn = self._connect()
        try:
            for table, expected in self.EXPECTED_COLUMNS.items():
                cur = conn.execute(f"PRAGMA table_info({table})")
                actual = {r[1] for r in cur.fetchall()}
                missing = expected - actual
                unexpected = actual - expected
                assert not missing, f"{table}: missing columns {missing}"
                assert not unexpected, (
                    f"{table}: unexpected columns {unexpected}.  Update "
                    "EXPECTED_COLUMNS in test_invariants.py if intentional."
                )
        finally:
            conn.close()

    def test_foreign_key_check_passes(self):
        conn = self._connect()
        try:
            cur = conn.execute("PRAGMA foreign_key_check")
            violations = cur.fetchall()
        finally:
            conn.close()
        assert not violations, f"FK violations: {violations}"

    def test_label_event_vocabulary_populated(self):
        """Every label_type the build script emits is present in label_event."""
        from app.core import historical_db
        expected = (
            historical_db.LABEL_TYPES_NUMERIC
            + historical_db.LABEL_TYPES_FLAG
            + historical_db.LABEL_TYPES_CATEGORICAL
        )
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT label_type, COUNT(*) FROM label_event GROUP BY label_type"
            )
            seen = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        missing = set(expected) - seen
        assert not missing, (
            f"label_types declared in historical_db but missing from corpus: "
            f"{missing}"
        )


class TestExportColumnContract:
    """Pin the column header of every CSV exported by
    scripts.export_historical_csvs.  Catches accidental column-order changes
    or column drops in the byte-stable export contract."""

    EXPECTED_HEADERS = {
        "historical_players.csv": (
            "date,player_name,team,position,real_score,total_value,"
            "is_highest_value,is_most_popular,is_most_drafted_3x,"
            "ops_at_slate,iso_at_slate,era_at_slate,whip_at_slate,"
            "k9_at_slate,x_woba,x_ba,x_slg,avg_ev,hard_hit_pct,barrel_pct,"
            "max_ev,x_era,x_woba_against,fb_velo,whiff_pct,chase_pct,"
            "fb_ivb,fb_extension,ops_vs_lhp_at_slate,ops_vs_rhp_at_slate,"
            "batting_order_at_slate,card_boost,drafts,draft_count,"
            "avg_draft_slot,most_common_slot,avg_draft_mult,avg_draft_tv,"
            "highest_draft_tv,injury_status"
        ),
        "historical_winning_drafts.csv": (
            "date,winner_rank,slot_index,player_name,team,position,"
            "real_score,slot_mult,card_boost,total_mult"
        ),
        "hv_player_game_stats.csv": (
            "date,player_name,team_actual,position,real_score,game_result,"
            "ab,r,h,hr,rbi,bb,so,ip,er,k_pitching,decision,notes,"
            "ops_at_slate,iso_at_slate"
        ),
        "historical_player_game_logs.csv": (
            "slate_date,player_name,team,mlb_id,position,game_date,opponent,"
            "is_home,ab,runs,hits,hr,rbi,bb,so,sb,ip,er,k_pitching,decision"
        ),
    }

    def test_export_produces_expected_headers(self, tmp_path):
        """Run the export into tmp_path and assert each CSV's header matches."""
        from scripts.export_historical_csvs import export_all
        export_all(out_dir=tmp_path)
        for filename, expected in self.EXPECTED_HEADERS.items():
            actual = (tmp_path / filename).read_text().splitlines()[0]
            assert actual == expected, (
                f"{filename} header drift:\n"
                f"  expected: {expected}\n"
                f"  actual:   {actual}"
            )


# ---------------------------------------------------------------------------
# 3. Constant-perturbation rank stability
# ---------------------------------------------------------------------------

class TestConstantRankStability:
    """Perturb each constant in the filter_strategy namespace by ±10% on a
    synthetic 20-candidate pool and assert Kendall-tau ≥ 0.78 vs baseline.

    What this catches: a cliff-threshold whose small move causes a large
    fraction of the pool to flip ranks — a sign that the constant is brittle
    and likely overfit to a narrow range of conditions.

    What this does NOT do: calibrate the constants against historical
    outcomes. The pool is synthetic (random env_score, total_score values
    seeded deterministically). No real_score or HV flag is ever read.
    """

    PERTURBATION_TARGETS = [
        # Env modifier bounds — PRIMARY signal
        "ENV_MODIFIER_FLOOR",
        "ENV_MODIFIER_CEILING",
        # Trait modifier bounds — SECONDARY signal
        "TRAIT_MODIFIER_FLOOR",
        "TRAIT_MODIFIER_CEILING",
        # Context multiplier (DNP penalties were removed in strict-mode pass)
        "STACK_BONUS",
        # V13.3 — rookie env cap
        "ROOKIE_ENV_MODIFIER_CEILING",
    ]

    # V15 — continuous popularity-curve constants live on a different
    # import path (app.core.constants) but participate in EV.  Keep them
    # in a separate parametrize so the monkeypatch lookup is correct.
    POPULARITY_PERTURBATION_TARGETS = [
        "POPULARITY_NEUTRAL_SCORE",
        "POPULARITY_SLOPE",
        "POPULARITY_MULT_FLOOR",
        "POPULARITY_MULT_CEILING",
    ]

    TAU_FLOOR = 0.78

    @staticmethod
    def _build_pool(seed: int = 42, size: int = 20) -> list[FilteredCandidate]:
        rng = random.Random(seed)
        pool = []
        for i in range(size):
            pool.append(
                _make_candidate(
                    name=f"P{i}",
                    team=f"T{i % 10}",
                    is_pitcher=(i < size // 5),
                    total_score=rng.uniform(20.0, 90.0),
                    env_score=rng.uniform(0.2, 0.95),
                    # Strict-mode: every batter has a projected batting order
                    # (DNP filter excludes None upstream).  No env_unknown_count
                    # field anymore — every signal is required.
                    batting_order=rng.choice([1, 3, 5, 7, 9]),
                )
            )
        return pool

    @staticmethod
    def _rank(pool: list[FilteredCandidate]) -> list[str]:
        # Stable ordering: sort by EV desc, break ties by name for determinism.
        return [
            c.player_name
            for c in sorted(pool, key=lambda c: (-_compute_base_ev(c), c.player_name))
        ]

    @pytest.mark.parametrize("constant_name", PERTURBATION_TARGETS)
    @pytest.mark.parametrize("delta", [-0.10, +0.10])
    def test_rank_is_stable_under_constant_perturbation(
        self, constant_name, delta, monkeypatch
    ):
        from app.services import filter_strategy as fs

        original = getattr(fs, constant_name)
        baseline_pool = self._build_pool()
        baseline_ranking = self._rank(baseline_pool)

        monkeypatch.setattr(fs, constant_name, original * (1.0 + delta))
        perturbed_pool = self._build_pool()  # rebuild with same seed
        perturbed_ranking = self._rank(perturbed_pool)

        tau = _kendall_tau(baseline_ranking, perturbed_ranking)
        assert tau >= self.TAU_FLOOR, (
            f"Ranking collapsed under {delta:+.0%} perturbation of {constant_name}: "
            f"Kendall-tau={tau:.3f} < floor={self.TAU_FLOOR}. "
            "This constant is cliff-like; consider widening its effective range."
        )

    @pytest.mark.parametrize("constant_name", POPULARITY_PERTURBATION_TARGETS)
    @pytest.mark.parametrize("delta", [-0.10, +0.10])
    def test_popularity_curve_perturbation_does_not_collapse_rank(
        self, constant_name, delta, monkeypatch
    ):
        """V15: perturbing each popularity-curve constant by ±10% must not
        catastrophically reorder the ranking.  Pool seeded with assorted
        popularity scores so the leverage signal is actually exercised.
        """
        import random as _r
        from app.core import constants as cc
        from app.core import popularity as pop

        rng = _r.Random(42)
        baseline_pool = self._build_pool()
        # Seed each candidate with a popularity score in [0, 8] — covers
        # the empirical pool's working range so the curve is exercised.
        for c in baseline_pool:
            c.predicted_ownership_score = rng.uniform(0.0, 8.0)
        baseline_ranking = self._rank(baseline_pool)

        original = getattr(cc, constant_name)
        monkeypatch.setattr(cc, constant_name, original * (1.0 + delta))
        # popularity.py imports the constants at module-load time, so
        # monkeypatch the module-level binding too.
        monkeypatch.setattr(pop, constant_name, original * (1.0 + delta))
        perturbed_pool = self._build_pool()
        rng2 = _r.Random(42)  # same seed → same scores
        for c in perturbed_pool:
            c.predicted_ownership_score = rng2.uniform(0.0, 8.0)
        perturbed_ranking = self._rank(perturbed_pool)

        tau = _kendall_tau(baseline_ranking, perturbed_ranking)
        assert tau >= self.TAU_FLOOR, (
            f"Popularity curve rank collapsed under {delta:+.0%} of "
            f"{constant_name}: tau={tau:.3f} < floor={self.TAU_FLOOR}"
        )


# ---------------------------------------------------------------------------
# 4. Kendall-tau helper self-test (keeps the helper honest)
# ---------------------------------------------------------------------------

def test_kendall_tau_self_test():
    assert _kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert _kendall_tau(["a", "b", "c"], ["c", "b", "a"]) == -1.0
    # Swapping one adjacent pair out of 3 comparisons → (2 - 1)/3 = 1/3
    assert abs(_kendall_tau(["a", "b", "c"], ["b", "a", "c"]) - (1 / 3)) < 1e-9
