"""Build the historical calibration corpus SQLite store from the legacy CSVs/JSON.

This is the one-shot ingestion script for Step 1 of the migration plan.  Reads:
  - data/historical_players.csv             (40 cols, 1645 rows, 43 dates)
  - data/historical_winning_drafts.csv      (10 cols, 2309 rows)
  - data/historical_slate_results.json      (43 envelopes, 551 game objects)
  - data/historical_player_game_logs.csv    (20 cols, 12,291 rows)
  - data/hv_player_game_stats.csv           (20 cols, 752 rows)

Writes to data/historical.db (5 tables per app/core/historical_db.py).

mlb_id resolution
-----------------
historical_players.csv and historical_winning_drafts.csv carry no mlb_id.
We resolve via:
  1. Index from historical_player_game_logs.csv: {(name_normalized, team_canonical) -> mlb_id}
  2. MLB Stats API people-search for unresolved names, with disambiguation by team.
  3. Targeted team-roster pulls for short-form names (e.g. "A. Ramirez").

The cache is persisted to scripts/output/.player_id_cache.json so re-runs do
not re-hit the API.

Usage:
    python scripts/build_historical_db.py --rebuild
    python scripts/build_historical_db.py --emit-parity-baseline scripts/output/migration_parity_report.txt
    python scripts/build_historical_db.py --db /tmp/scale.db --synthetic-multiplier 5 --rebuild
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Set BO_CURRENT_SEASON if not already set — needed to import app.core.constants.
os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "build-historical-db-stub")

from app.core import historical_db  # noqa: E402
from app.core.constants import canonicalize_team  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
HISTORICAL_PLAYERS_CSV = DATA_DIR / "historical_players.csv"
WINNING_DRAFTS_CSV = DATA_DIR / "historical_winning_drafts.csv"
SLATE_RESULTS_JSON = DATA_DIR / "historical_slate_results.json"
PLAYER_GAME_LOGS_CSV = DATA_DIR / "historical_player_game_logs.csv"
HV_STATS_CSV = DATA_DIR / "hv_player_game_stats.csv"
PLAYER_ID_CACHE = REPO_ROOT / "scripts" / "output" / ".player_id_cache.json"
UNRESOLVED_REPORT = REPO_ROOT / "scripts" / "output" / ".unresolved_player_ids.json"

# Synthetic mlb_id allocator for rows that cannot be resolved via game-logs,
# local cache, MLB people-search, or team-roster lookup.  These are typically
# OCR/typo errors from the V9.1-era manual ingest that landed on no leaderboard
# (all flags 0) and are not in any future calibration's signal set.  Using
# negative IDs preserves player_slate row count and keeps the rows queryable
# (by name + team) without colliding with any real MLB ID.
SYNTHETIC_ID_BASE = -1_000_000

MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 20

# Local team-alias extension on top of canonicalize_team.  WAS appears in
# historical_players.csv alongside WSH (mid-season writes used inconsistent
# abbreviations).  No app/ change — this is build-script-local.
LOCAL_TEAM_ALIASES = {"WAS": "WSH"}

# Outcome-label categories (audit Section F).
LABEL_FLAG_HIGHEST_VALUE = "highest_value"
LABEL_FLAG_MOST_POPULAR = "most_popular"
LABEL_FLAG_MOST_DRAFTED_3X = "most_drafted_3x"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("build_historical_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """NFKD-decompose, drop combining marks, lowercase, collapse whitespace.

    Matches app.core.popularity._normalize and the live runtime's
    name_normalize.  The build script uses this everywhere as the join key
    component for player identity.
    """
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_name.lower().split())


def cteam(team: str) -> str:
    """Canonical team abbreviation, with build-script-local alias extension."""
    canonical = canonicalize_team(team)
    return LOCAL_TEAM_ALIASES.get(canonical, canonical)


def parse_float_or_none(s):
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_int_or_none(s):
    if s is None or s == "":
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# mlb_id resolution
# ---------------------------------------------------------------------------
class PlayerIdResolver:
    """Resolve (name, canonical_team) -> mlb_id with caching.

    Lookup precedence:
        1. game-log index (built from historical_player_game_logs.csv)
        2. local cache file (scripts/output/.player_id_cache.json)
        3. MLB Stats API people-search
        4. MLB Stats API team-roster lookup (for short-name patterns)

    Persists the cache after every successful API resolution so partial runs
    are not lost.
    """

    SHORT_NAME_RE = re.compile(r"^([a-z])\.\s*(.+)$")

    def __init__(self, gamelog_path: Path, cache_path: Path):
        self.gamelog_index = self._build_gamelog_index(gamelog_path)
        log.info("mlb_id index from game_logs: %d entries", len(self.gamelog_index))
        self.cache_path = cache_path
        self.cache = self._load_cache()
        self._team_roster_cache: dict[str, list[dict]] = {}
        # synthetic-id state — assigned monotonically when the MLB API path
        # cannot resolve a (name, team) pair.  Persisted to the cache so the
        # same junk row gets the same synthetic id across re-runs.
        self._next_synth = SYNTHETIC_ID_BASE
        for v in self.cache.values():
            if isinstance(v, int) and v <= SYNTHETIC_ID_BASE:
                self._next_synth = min(self._next_synth, v - 1)
        self.unresolved: list[tuple[str, str]] = []

    @staticmethod
    def _build_gamelog_index(path: Path) -> dict[tuple[str, str], int]:
        idx: dict[tuple[str, str], int] = {}
        if not path.exists():
            return idx
        with path.open() as f:
            for r in csv.DictReader(f):
                try:
                    mlb_id = int(r["mlb_id"])
                except (KeyError, ValueError):
                    continue
                key = (normalize_name(r["player_name"]), cteam(r["team"]))
                idx[key] = mlb_id
        return idx

    def _load_cache(self) -> dict[str, int]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("cache file %s unreadable, ignoring", self.cache_path)
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=2, sort_keys=True))

    @staticmethod
    def _cache_key(name_norm: str, team: str) -> str:
        return f"{name_norm}|{team}"

    def resolve(self, player_name: str, team: str) -> int | None:
        name_norm = normalize_name(player_name)
        team_canon = cteam(team)
        key_tuple = (name_norm, team_canon)

        # 1. game-log index
        if key_tuple in self.gamelog_index:
            return self.gamelog_index[key_tuple]

        # 2. local cache
        cache_key = self._cache_key(name_norm, team_canon)
        if cache_key in self.cache:
            return self.cache[cache_key]

        # 3 + 4. MLB API.  Short-name pattern ("A. Ramirez") routes via
        # team roster.  Full-name pattern routes via people-search first.
        m = self.SHORT_NAME_RE.match(name_norm)
        if m:
            mlb_id = self._resolve_via_team_roster(m.group(1), m.group(2), team_canon)
        else:
            mlb_id = self._resolve_via_people_search(name_norm, team_canon)
            if mlb_id is None:
                # Fall back to team roster — handles cases where the player
                # name varies between MLB API records (e.g. "Mc"+"cullers"
                # case differences, ".jr" suffix mismatches).
                last_word = name_norm.split()[-1].rstrip(".")
                mlb_id = self._resolve_via_team_roster("", last_word, team_canon)

        if mlb_id is not None:
            self.cache[cache_key] = mlb_id
            self._save_cache()
            return mlb_id

        # Fallback: assign a sentinel negative id and record for traceability.
        synth = self._next_synth
        self._next_synth -= 1
        self.cache[cache_key] = synth
        self.unresolved.append((player_name, team_canon))
        self._save_cache()
        log.warning("mlb_id unresolved for %r / %r — assigned synthetic id %d",
                    player_name, team_canon, synth)
        return synth

    def _resolve_via_people_search(self, name_norm: str, team_canon: str) -> int | None:
        try:
            r = requests.get(
                f"{MLB_API}/people/search",
                params={"names": name_norm},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            log.warning("people-search failed for %r: %s", name_norm, e)
            return None
        people = r.json().get("people", [])
        candidates: list[tuple[int, str]] = []
        for p in people:
            pid = p.get("id")
            current_team_abbr = (p.get("currentTeam") or {}).get("abbreviation") or ""
            current_team_canon = cteam(current_team_abbr) if current_team_abbr else ""
            if pid is not None:
                candidates.append((pid, current_team_canon))
        # Prefer current team match.
        for pid, ct in candidates:
            if ct == team_canon:
                return pid
        # If a single result exists, accept it (mid-season trade resilience).
        if len(candidates) == 1:
            return candidates[0][0]
        return None

    def _resolve_via_team_roster(
        self, first_initial: str, last_name: str, team_canon: str,
    ) -> int | None:
        """Look up a player by short name on a specific team's roster."""
        roster = self._fetch_team_roster(team_canon)
        if not roster:
            return None
        last_norm = normalize_name(last_name)
        for p in roster:
            full = normalize_name(p.get("fullName") or "")
            tokens = full.split()
            if not tokens:
                continue
            roster_last = tokens[-1].rstrip(".")
            if roster_last != last_norm:
                # Allow last-word-without-suffix match (e.g. "mccullers jr." -> "mccullers")
                if len(tokens) >= 2 and tokens[-2].rstrip(".") == last_norm:
                    pass
                else:
                    continue
            if first_initial:
                first = tokens[0].lstrip(".") if tokens else ""
                if not first.startswith(first_initial):
                    continue
            return p.get("id")
        return None

    def _fetch_team_roster(self, team_canon: str) -> list[dict]:
        if team_canon in self._team_roster_cache:
            return self._team_roster_cache[team_canon]
        # Reverse-lookup TEAM_MLB_IDS from app/core/mlb_api
        from app.core.mlb_api import TEAM_MLB_IDS

        team_id = TEAM_MLB_IDS.get(team_canon)
        if team_id is None:
            log.warning("no MLB team_id for canonical team %r", team_canon)
            self._team_roster_cache[team_canon] = []
            return []
        try:
            r = requests.get(
                f"{MLB_API}/teams/{team_id}/roster",
                params={"rosterType": "fullRoster"},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            roster = r.json().get("roster", [])
            people = [{"id": e["person"]["id"], "fullName": e["person"]["fullName"]}
                      for e in roster if e.get("person")]
        except Exception as e:
            log.warning("roster fetch failed for %s (id=%s): %s", team_canon, team_id, e)
            people = []
        self._team_roster_cache[team_canon] = people
        return people


# ---------------------------------------------------------------------------
# Phase 1 — slate envelope + slate_game from historical_slate_results.json
# ---------------------------------------------------------------------------
def ingest_slate_envelope(conn, slate_results: list[dict]) -> int:
    """Insert one row per envelope into `slate`, plus per-game rows into
    `slate_game`.  Returns the number of game rows written.

    Doubleheaders share a single MLB game_pk in the source JSON; we assign
    incremental `game_number` (1, 2) to keep all rows distinct under the
    composite PK."""
    slate_count = 0
    game_count = 0
    for envelope in slate_results:
        slate_date = envelope["date"]
        historical_db.upsert_slate(conn, {
            "slate_date": slate_date,
            "game_count": envelope.get("game_count") or 0,
            "num_brawlers": envelope.get("num_brawlers"),
            "season_stage": envelope.get("season_stage") or "regular-season",
            "source": envelope.get("source") or envelope.get("previous_source") or "",
            "saved_at": envelope.get("saved_at") or "",
            "notes": envelope.get("notes") or "",
        })
        slate_count += 1
        seen_pks: dict[int, int] = {}  # game_pk -> next game_number to assign
        for g in envelope.get("games", []):
            game_pk = g.get("game_pk")
            if game_pk is None:
                continue
            game_pk = int(game_pk)
            game_number = seen_pks.get(game_pk, 1)
            seen_pks[game_pk] = game_number + 1
            row = {
                "slate_date": slate_date,
                "game_pk": game_pk,
                "game_number": game_number,
                "home_team": cteam(g.get("home", "")),
                "away_team": cteam(g.get("away", "")),
                "home_starter_id": parse_int_or_none(g.get("home_starter_id")),
                "home_starter_name": g.get("home_starter_name"),
                "home_starter_hand": g.get("home_starter_hand"),
                "home_starter_era": parse_float_or_none(g.get("home_starter_era")),
                "home_starter_whip": parse_float_or_none(g.get("home_starter_whip")),
                "home_starter_k_per_9": parse_float_or_none(g.get("home_starter_k_per_9")),
                "home_starter_x_era": parse_float_or_none(g.get("home_starter_x_era")),
                "home_starter_x_woba_against": parse_float_or_none(g.get("home_starter_x_woba_against")),
                "away_starter_id": parse_int_or_none(g.get("away_starter_id")),
                "away_starter_name": g.get("away_starter_name"),
                "away_starter_hand": g.get("away_starter_hand"),
                "away_starter_era": parse_float_or_none(g.get("away_starter_era")),
                "away_starter_whip": parse_float_or_none(g.get("away_starter_whip")),
                "away_starter_k_per_9": parse_float_or_none(g.get("away_starter_k_per_9")),
                "away_starter_x_era": parse_float_or_none(g.get("away_starter_x_era")),
                "away_starter_x_woba_against": parse_float_or_none(g.get("away_starter_x_woba_against")),
                "home_team_ops": parse_float_or_none(g.get("home_team_ops")),
                "home_team_k_pct": parse_float_or_none(g.get("home_team_k_pct")),
                "home_bullpen_era": parse_float_or_none(g.get("home_bullpen_era")),
                "home_team_framing_runs": parse_float_or_none(g.get("home_team_framing_runs")),
                "home_team_framing_pct": parse_float_or_none(g.get("home_team_framing_pct")),
                "away_team_ops": parse_float_or_none(g.get("away_team_ops")),
                "away_team_k_pct": parse_float_or_none(g.get("away_team_k_pct")),
                "away_bullpen_era": parse_float_or_none(g.get("away_bullpen_era")),
                "away_team_framing_runs": parse_float_or_none(g.get("away_team_framing_runs")),
                "away_team_framing_pct": parse_float_or_none(g.get("away_team_framing_pct")),
                "home_team_record_w": parse_int_or_none(g.get("home_team_record_w")),
                "home_team_record_l": parse_int_or_none(g.get("home_team_record_l")),
                "home_team_rest_days": parse_int_or_none(g.get("home_team_rest_days")),
                "away_team_record_w": parse_int_or_none(g.get("away_team_record_w")),
                "away_team_record_l": parse_int_or_none(g.get("away_team_record_l")),
                "away_team_rest_days": parse_int_or_none(g.get("away_team_rest_days")),
                "home_l10_wins": parse_int_or_none(g.get("home_l10_wins")),
                "home_series_wins": parse_int_or_none(g.get("home_series_wins")),
                "away_l10_wins": parse_int_or_none(g.get("away_l10_wins")),
                "away_series_wins": parse_int_or_none(g.get("away_series_wins")),
                "vegas_total": parse_float_or_none(g.get("vegas_total")),
                "home_moneyline": parse_int_or_none(g.get("home_moneyline")),
                "away_moneyline": parse_int_or_none(g.get("away_moneyline")),
                "park_team": g.get("park_team"),
                "park_hr_factor": parse_float_or_none(g.get("park_hr_factor")),
                "temperature_f": parse_float_or_none(g.get("temperature_f")),
                "wind_speed_mph": parse_float_or_none(g.get("wind_speed_mph")),
                "wind_direction": g.get("wind_direction"),
                "wind_direction_deg": parse_int_or_none(g.get("wind_direction_deg")),
                "datetime_utc": g.get("datetime_utc"),
                "home_score": parse_int_or_none(g.get("home_score")),
                "away_score": parse_int_or_none(g.get("away_score")),
                "winner": g.get("winner"),
                "loser": g.get("loser"),
                "winner_score": parse_int_or_none(g.get("winner_score")),
                "loser_score": parse_int_or_none(g.get("loser_score")),
            }
            historical_db.upsert_slate_game(conn, row)
            game_count += 1
    log.info("ingested %d slates, %d games", slate_count, game_count)
    return game_count


# ---------------------------------------------------------------------------
# Phase 2 — player_slate + label_event from historical_players.csv
# ---------------------------------------------------------------------------
# Numeric columns on player_slate (all REAL/INT — non-outcome inputs only).
PLAYER_SLATE_NUMERIC_COLS = (
    "ops_at_slate", "iso_at_slate",
    "era_at_slate", "whip_at_slate", "k9_at_slate",
    "ops_vs_lhp_at_slate", "ops_vs_rhp_at_slate",
    "x_woba", "x_ba", "x_slg",
    "avg_ev", "hard_hit_pct", "barrel_pct", "max_ev",
    "x_era", "x_woba_against",
    "fb_velo", "whiff_pct", "chase_pct",
    "fb_ivb", "fb_extension",
)


def ingest_players_csv(
    conn,
    resolver: PlayerIdResolver,
    csv_path: Path,
    observed_at: str,
) -> tuple[int, int]:
    """Ingest historical_players.csv.

    Each row produces:
      - One row in `player_slate` (identity + at-slate inputs).
      - 0..N rows in `label_event` (one per non-empty label column).

    Returns (player_slate_rows, label_event_rows).
    """
    player_rows = 0
    label_rows = 0

    with csv_path.open() as f:
        for r in csv.DictReader(f):
            slate_date = r["date"]
            name = r["player_name"]
            team_canon = cteam(r["team"])
            mlb_id = resolver.resolve(name, team_canon)
            # Resolver always returns an int (real or synthetic).  Synthetic
            # IDs preserve row count for junk rows from V9.1-era manual ingest.

            ps_row = {
                "slate_date": slate_date,
                "mlb_id": int(mlb_id),
                "player_name": name,
                "team": team_canon,
                "position": r.get("position") or "OF",
                "game_pk": None,  # set by env backfill if/when game_pk known
                "batting_order_at_slate": parse_int_or_none(r.get("batting_order_at_slate")),
            }
            for col in PLAYER_SLATE_NUMERIC_COLS:
                ps_row[col] = parse_float_or_none(r.get(col))
            historical_db.upsert_player_slate(conn, ps_row)
            player_rows += 1

            # ---- numeric scalar labels ----
            for col_name, label_type in [
                ("real_score", "real_score"),
                ("total_value", "total_value"),
                ("card_boost", "card_boost"),
                ("drafts", "drafts"),
                ("draft_count", "draft_count"),
                ("avg_draft_slot", "avg_draft_slot"),
                ("avg_draft_mult", "avg_draft_mult"),
                ("avg_draft_tv", "avg_draft_tv"),
                ("highest_draft_tv", "highest_draft_tv"),
            ]:
                v = parse_float_or_none(r.get(col_name))
                if v is None:
                    continue
                historical_db.upsert_label_event(
                    conn,
                    slate_date=slate_date, mlb_id=mlb_id, label_type=label_type,
                    label_value=v, label_text=None,
                    source=historical_db.SOURCE_REALSPORTS_STATS,
                    observed_at=observed_at,
                )
                label_rows += 1

            # ---- boolean leaderboard flags ----
            for col_name, label_type in [
                ("is_highest_value", LABEL_FLAG_HIGHEST_VALUE),
                ("is_most_popular", LABEL_FLAG_MOST_POPULAR),
                ("is_most_drafted_3x", LABEL_FLAG_MOST_DRAFTED_3X),
            ]:
                if r.get(col_name) == "1":
                    historical_db.upsert_label_event(
                        conn,
                        slate_date=slate_date, mlb_id=mlb_id, label_type=label_type,
                        label_value=1.0, label_text=None,
                        source=historical_db.SOURCE_REALSPORTS_STATS,
                        observed_at=observed_at,
                    )
                    label_rows += 1

            # ---- categorical labels ----
            mcs = r.get("most_common_slot")
            if mcs:
                historical_db.upsert_label_event(
                    conn,
                    slate_date=slate_date, mlb_id=mlb_id, label_type="most_common_slot",
                    label_value=parse_float_or_none(mcs), label_text=str(mcs),
                    source=historical_db.SOURCE_REALSPORTS_STATS,
                    observed_at=observed_at,
                )
                label_rows += 1
            inj = r.get("injury_status")
            if inj:
                historical_db.upsert_label_event(
                    conn,
                    slate_date=slate_date, mlb_id=mlb_id, label_type="injury_status",
                    label_value=None, label_text=inj,
                    source=historical_db.SOURCE_REALSPORTS_STATS,
                    observed_at=observed_at,
                )
                label_rows += 1

    log.info("ingested %d player_slate rows, %d label_event rows from historical_players.csv",
             player_rows, label_rows)
    return (player_rows, label_rows)


# ---------------------------------------------------------------------------
# Phase 3 — winning lineups → label_event(winning_lineup_slot)
# ---------------------------------------------------------------------------
def ingest_winning_drafts(
    conn,
    resolver: PlayerIdResolver,
    csv_path: Path,
    observed_at: str,
) -> int:
    """Each row of historical_winning_drafts.csv becomes a label_event row of
    type 'winning_lineup_slot'.  source = "rank=N" so re-running on the same
    date produces idempotent upserts but DIFFERENT rank rows for the same
    player do not collide on the PK.

    label_value = real_score; label_text = "rank={R}|slot={S}|mult={M}|cb={CB}|tm={TM}".
    """
    rows_written = 0

    with csv_path.open() as f:
        for csv_idx, r in enumerate(csv.DictReader(f)):
            slate_date = r["date"]
            name = r["player_name"]
            team_canon = cteam(r["team"])
            mlb_id = resolver.resolve(name, team_canon)

            try:
                rank = int(r["winner_rank"])
                slot_index = int(r["slot_index"])
                slot_mult = float(r["slot_mult"])
                rs = float(r["real_score"])
            except ValueError:
                continue

            cb_raw = r.get("card_boost", "") or ""
            tm_raw = r.get("total_mult", "") or ""
            # Preserve the row's identity columns verbatim — winning_drafts
            # captures don't always agree with the day's historical_players row
            # (mid-day trades, OCR variance), and the audit harness joins on
            # (date, name, team) so the export must reproduce the original
            # team string exactly even when player_slate has a different one.
            name_raw = r.get("player_name", "") or ""
            team_raw = r.get("team", "") or ""
            pos_raw = r.get("position", "") or ""
            # Use a JSON-encoded label_text so future fields land cleanly.
            label_text = json.dumps({
                "rank": rank,
                "slot": slot_index,
                "slot_mult": slot_mult,
                "card_boost": cb_raw,
                "total_mult": tm_raw,
                "name": name_raw,
                "team": team_raw,
                "position": pos_raw,
            }, sort_keys=True)
            # Source includes the CSV row index so EXACT-duplicate winning_drafts
            # rows (same date+rank+slot+player+team+score) coexist instead of
            # collapsing — preserves the original CSV's per-rank row count for
            # the audit_lineup_tv.py "matched == 5" filter.
            historical_db.upsert_label_event(
                conn,
                slate_date=slate_date,
                mlb_id=mlb_id,
                label_type="winning_lineup_slot",
                label_value=rs,
                label_text=label_text,
                source=f"row={csv_idx}|rank={rank}|slot={slot_index}",
                observed_at=observed_at,
            )
            rows_written += 1

    log.info("ingested %d winning_lineup_slot label_event rows", rows_written)
    return rows_written


# ---------------------------------------------------------------------------
# Phase 4 — player_game_log from historical_player_game_logs.csv
# ---------------------------------------------------------------------------
def ingest_player_game_logs(conn, csv_path: Path) -> int:
    rows = 0
    with csv_path.open() as f:
        for r in csv.DictReader(f):
            try:
                mlb_id = int(r["mlb_id"])
            except (KeyError, ValueError):
                continue
            row = {
                "slate_date": r["slate_date"],
                "mlb_id": mlb_id,
                "game_date": r["game_date"],
                "player_name": r.get("player_name"),
                "team": cteam(r.get("team", "")) if r.get("team") else None,
                "position": r.get("position"),
                "opponent": r.get("opponent"),
                "is_home": parse_int_or_none(r.get("is_home")),
                "ab": parse_int_or_none(r.get("ab")),
                "runs": parse_int_or_none(r.get("runs")),
                "hits": parse_int_or_none(r.get("hits")),
                "hr": parse_int_or_none(r.get("hr")),
                "rbi": parse_int_or_none(r.get("rbi")),
                "bb": parse_int_or_none(r.get("bb")),
                "so": parse_int_or_none(r.get("so")),
                "sb": parse_int_or_none(r.get("sb")),
                "ip": parse_float_or_none(r.get("ip")),
                "er": parse_int_or_none(r.get("er")),
                "k_pitching": parse_int_or_none(r.get("k_pitching")),
                "decision": r.get("decision"),
            }
            historical_db.upsert_player_game_log(conn, row)
            rows += 1
    log.info("ingested %d player_game_log rows", rows)
    return rows


# ---------------------------------------------------------------------------
# Phase 5 — HV box scores → label_event(box_score)
# ---------------------------------------------------------------------------
HV_BOX_SCORE_FIELDS = (
    "ab", "r", "h", "hr", "rbi", "bb", "so",
    "ip", "er", "k_pitching", "decision",
    "game_result", "notes",
)


def ingest_hv_player_stats(
    conn,
    resolver: PlayerIdResolver,
    csv_path: Path,
    observed_at: str,
) -> int:
    """Each HV box-score row becomes a label_event(box_score) row with all
    eleven box-score columns serialized as JSON in label_text.  This is the
    Section F.1 design: instead of pinning eleven columns on a side table that
    no calibration script reads, we keep the data inline as a typed-source
    label.
    """
    rows = 0
    with csv_path.open() as f:
        for r in csv.DictReader(f):
            slate_date = r["date"]
            name = r["player_name"]
            team = cteam(r["team_actual"])
            mlb_id = resolver.resolve(name, team)
            payload = {k: r.get(k, "") for k in HV_BOX_SCORE_FIELDS}
            real_score = parse_float_or_none(r.get("real_score"))
            historical_db.upsert_label_event(
                conn,
                slate_date=slate_date,
                mlb_id=mlb_id,
                label_type="box_score",
                label_value=real_score,
                label_text=json.dumps(payload),
                source=historical_db.SOURCE_MLB_BOXSCORE,
                observed_at=observed_at,
            )
            rows += 1
    log.info("ingested %d label_event(box_score) rows", rows)
    return rows


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def assert_row_counts(conn, expected: dict[str, int]) -> None:
    """Assert table row counts match expected.  Halts on miss."""
    actual = {}
    for table in ("slate", "slate_game", "player_slate", "player_game_log"):
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        actual[table] = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM label_event")
    actual["label_event"] = cur.fetchone()[0]

    log.info("Row counts: %s", actual)
    misses = []
    for table, want in expected.items():
        got = actual[table]
        if isinstance(want, tuple):  # (min, max)
            lo, hi = want
            if got < lo or got > hi:
                misses.append(f"{table}: {got} not in [{lo}, {hi}]")
        else:
            if got != want:
                misses.append(f"{table}: {got} != {want}")
    if misses:
        for m in misses:
            log.error(m)
        raise SystemExit("FAIL: row count assertion failed")


def assert_foreign_keys(conn) -> None:
    cur = conn.execute("PRAGMA foreign_key_check")
    rows = cur.fetchall()
    if rows:
        for r in rows:
            log.error("FK violation: %s", tuple(r))
        raise SystemExit(f"FAIL: {len(rows)} foreign-key violations")


def assert_label_event_coverage(conn) -> None:
    """Confirm at least one row exists for each label_type the build emits."""
    expected_types = {
        "real_score", "total_value", "card_boost", "drafts",
        "draft_count", "avg_draft_slot", "avg_draft_mult", "avg_draft_tv",
        "highest_draft_tv", "most_common_slot", "injury_status",
        "highest_value", "most_popular", "most_drafted_3x",
        "winning_lineup_slot", "box_score",
    }
    cur = conn.execute(
        "SELECT label_type, COUNT(*) FROM label_event GROUP BY label_type"
    )
    seen = {row[0]: row[1] for row in cur.fetchall()}
    log.info("label_event distribution: %s", dict(sorted(seen.items())))
    missing = expected_types - seen.keys()
    if missing:
        raise SystemExit(f"FAIL: label_event missing types: {sorted(missing)}")


# ---------------------------------------------------------------------------
# --emit-parity-baseline: run the 7 readers against existing CSVs/JSON
# ---------------------------------------------------------------------------
PARITY_READERS = [
    ("audit_hv_hit_rate.py", []),
    ("audit_lineup_tv.py", []),
    ("audit_slot1_quality.py", []),
    ("audit_tv_signals.py", []),
    ("calibrate_popularity_curve.py", []),
    ("calibrate_popularity_components.py", []),
    ("validate_ingest.py", ["--date", "2026-05-07"]),
]


def emit_parity_baseline(report_path: Path) -> None:
    """Run each reader against the EXISTING CSV/JSON paths and capture stdout.

    This is the Step-1 baseline.  Subsequent migration steps must preserve the
    numerical output across each reader (modulo deliberate calibration wins).
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("=" * 78)
    lines.append(f"Migration parity baseline — captured {now_iso()}")
    lines.append("Source: existing CSV/JSON paths (pre-migration).")
    lines.append("=" * 78)
    lines.append("")
    env = {**os.environ, "BO_CURRENT_SEASON": "2026", "BO_ODDS_API_KEY": "build-historical-db-stub"}
    for reader, extra_args in PARITY_READERS:
        reader_path = REPO_ROOT / "scripts" / reader
        cmd = [sys.executable, str(reader_path)] + list(extra_args)
        lines.append(f"--- $ {' '.join(cmd[1:])} ---")
        lines.append("")
        t0 = time.time()
        try:
            result = subprocess.run(
                cmd, cwd=str(REPO_ROOT), env=env,
                capture_output=True, text=True, timeout=900,
            )
            elapsed = time.time() - t0
            lines.append(result.stdout.rstrip())
            lines.append("")
            lines.append(f"[exit={result.returncode}; elapsed={elapsed:.1f}s]")
            if result.stderr:
                lines.append("[stderr]")
                lines.append(result.stderr.rstrip()[-2000:])
        except subprocess.TimeoutExpired:
            lines.append("[TIMEOUT after 900s]")
        except Exception as e:
            lines.append(f"[EXCEPTION: {e}]")
        lines.append("")
        lines.append("")
    report_path.write_text("\n".join(lines) + "\n")
    log.info("parity baseline written to %s", report_path)


# ---------------------------------------------------------------------------
# --synthetic-multiplier: scale corpus for Step-7 readiness check
# ---------------------------------------------------------------------------
def apply_synthetic_multiplier(conn, n: int) -> None:
    """Duplicate every row N-1 additional times under fake `slate_date` keys.

    Step 7 readiness probe: confirms the schema sustains 5×-10× the current
    corpus without size blowups or constraint violations.  Test-only path;
    never used in production.  Fake dates are derived by shifting each real
    date forward by (k × 1000) days (~3000 years in the future), guaranteed
    not to collide with any real slate.

    Implementation note: player_game_log carries an autoincrement rowid_seq
    PK; we exclude it from the SELECT so SQLite assigns fresh values per
    duplicated row.  The other tables use natural composite PKs and reuse
    the source columns directly.
    """
    if n <= 1:
        return
    from datetime import date as DateType, timedelta as _td
    real_dates = [r[0] for r in conn.execute("SELECT slate_date FROM slate").fetchall()]
    for k in range(1, n):
        offset_days = k * 1000
        for real in real_dates:
            fake = DateType.fromisoformat(real) + _td(days=offset_days)
            fake_str = fake.isoformat()
            for tbl in ("slate", "slate_game", "player_slate", "player_game_log", "label_event"):
                cols = [
                    c for c in historical_db._table_columns(conn, tbl)
                    if c not in ("slate_date", "rowid_seq")
                ]
                col_select = ", ".join(cols) if cols else ""
                col_insert = "slate_date" + (", " + col_select if cols else "")
                conn.execute(
                    f"INSERT INTO {tbl} ({col_insert}) "
                    f"SELECT ?{', ' + col_select if cols else ''} "
                    f"FROM {tbl} WHERE slate_date = ?",
                    (fake_str, real),
                )
    conn.commit()
    log.info("synthetic multiplier %d applied", n)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(historical_db.DEFAULT_DB_PATH),
                    help="Path to the SQLite DB (default: data/historical.db)")
    ap.add_argument("--rebuild", action="store_true",
                    help="Drop the existing DB before building (idempotent rebuild)")
    ap.add_argument("--emit-parity-baseline", default=None,
                    help="After build, run the 7 calibration readers against "
                         "existing CSVs/JSON and write their stdout here.")
    ap.add_argument("--synthetic-multiplier", type=int, default=1,
                    help="Step-7 use only: duplicate every row N-1 times under "
                         "fake date keys to scale-test the schema.")
    args = ap.parse_args()

    db_path = Path(args.db)
    if args.rebuild and db_path.exists():
        log.info("rebuild: removing existing %s", db_path)
        db_path.unlink()
        # WAL/shared-mem files
        for ext in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + ext)
            if sidecar.exists():
                sidecar.unlink()

    conn = historical_db.connect(db_path)
    historical_db.apply_schema(conn)

    # mlb_id resolver
    resolver = PlayerIdResolver(PLAYER_GAME_LOGS_CSV, PLAYER_ID_CACHE)

    observed_at = now_iso()

    # Phase 1: slate envelope
    log.info("Phase 1: ingesting historical_slate_results.json …")
    slate_results = json.loads(SLATE_RESULTS_JSON.read_text())
    games_written = ingest_slate_envelope(conn, slate_results)
    conn.commit()

    # Phase 2: player_slate + label_event from historical_players.csv
    log.info("Phase 2: ingesting historical_players.csv …")
    ps_rows, le_rows_p1 = ingest_players_csv(
        conn, resolver, HISTORICAL_PLAYERS_CSV, observed_at,
    )
    conn.commit()

    # Phase 3: winning lineups → label_event
    log.info("Phase 3: ingesting historical_winning_drafts.csv …")
    le_rows_p2 = ingest_winning_drafts(
        conn, resolver, WINNING_DRAFTS_CSV, observed_at,
    )
    conn.commit()

    # Phase 4: player_game_log
    log.info("Phase 4: ingesting historical_player_game_logs.csv …")
    pgl_rows = ingest_player_game_logs(conn, PLAYER_GAME_LOGS_CSV)
    conn.commit()

    # Phase 5: HV box scores → label_event(box_score)
    log.info("Phase 5: ingesting hv_player_game_stats.csv …")
    le_rows_p3 = ingest_hv_player_stats(
        conn, resolver, HV_STATS_CSV, observed_at,
    )
    conn.commit()

    # Synthetic scaling (Step 7 use)
    if args.synthetic_multiplier > 1:
        apply_synthetic_multiplier(conn, args.synthetic_multiplier)

    # Validation gate (only on real-corpus build).  Counts reflect the unique
    # tuples on each table's PK:
    #   - slate_game: 551 game objects in the JSON; doubleheader pairs sharing
    #     a single game_pk are disambiguated by `game_number`.
    #   - player_slate: 1644 data rows in historical_players.csv (1645 lines
    #     including header).
    #   - player_game_log: 12290 data rows; 63 duplicate
    #     (slate_date, mlb_id, game_date) collapse via INSERT OR REPLACE
    #     (data quality bug — same player+game appearing twice with
    #     conflicting box-score values; we keep the latest).
    if args.synthetic_multiplier == 1:
        assert_row_counts(conn, expected={
            "slate": 43,
            "slate_game": 551,
            "player_slate": 1644,
            "player_game_log": 12290,
            "label_event": (15000, 30000),
        })
        assert_foreign_keys(conn)
        assert_label_event_coverage(conn)

    log.info(
        "Build complete: %d games, %d player_slate rows, %d label_event rows total, %d game_logs",
        games_written, ps_rows, le_rows_p1 + le_rows_p2 + le_rows_p3, pgl_rows,
    )

    # Persist unresolved-id report for traceability.
    if resolver.unresolved:
        UNRESOLVED_REPORT.parent.mkdir(parents=True, exist_ok=True)
        UNRESOLVED_REPORT.write_text(json.dumps(
            [{"name": n, "team": t} for n, t in sorted(set(resolver.unresolved))],
            indent=2,
        ))
        log.warning("%d (name, team) pairs assigned synthetic IDs — see %s",
                    len(set(resolver.unresolved)), UNRESOLVED_REPORT)

    conn.close()

    if args.emit_parity_baseline:
        log.info("Emitting parity baseline …")
        emit_parity_baseline(Path(args.emit_parity_baseline))

    return 0


if __name__ == "__main__":
    sys.exit(main())
