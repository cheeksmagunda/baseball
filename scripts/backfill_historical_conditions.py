"""Backfill data/historical_conditions.csv for dates already in historical_slate_results.json.

For each date that has game results but no conditions row, this script:
  - Fetches weather from Open-Meteo archive (free, no API key)
  - Computes series context from the historical game results JSON
  - Fetches current-season team stats from MLB API (approximation — not as-of-date)
  - Fetches starting pitchers from MLB boxscores, then their current-season stats
  - Leaves Vegas lines NULL (not available from free historical sources)

Run once to backfill existing data:

    python scripts/backfill_historical_conditions.py

Then run scripts/export_slate_conditions.py after each new slate to stay current.

Limitations:
  - Team stats (OPS, K%, bullpen ERA) are current-season totals, not as-of-game-date.
    Early-season games (March/early April) will have stale stats.  These fields are
    primarily for calibrating the env scoring thresholds, not for live EV computation.
  - Vegas lines are always NULL for historical dates (free Odds API does not provide
    historical lines).
  - Starter ERA/K9 are current-season stats, not ERA at the time of the game.
"""

import asyncio
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.mlb_api import (
    _get,
    get_player_stats,
    get_team_pitching_stats,
    get_team_stats,
    TEAM_MLB_IDS,
)
from app.core.open_meteo import STADIUM_COORDINATES, get_game_weather

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CONDITIONS_CSV = DATA_DIR / "historical_conditions.csv"
SLATE_RESULTS = DATA_DIR / "historical_slate_results.json"

FIELDNAMES = [
    "date", "home_team", "away_team",
    "vegas_total", "home_moneyline", "away_moneyline",
    "home_starter_era", "away_starter_era", "home_starter_k9", "away_starter_k9",
    "home_team_ops", "away_team_ops", "home_team_k_pct", "away_team_k_pct",
    "home_bullpen_era", "away_bullpen_era",
    "series_home_wins", "series_away_wins", "home_team_l10_wins", "away_team_l10_wins",
    "park_team",
    "wind_speed_mph", "wind_direction", "temperature_f",
]

CURRENT_SEASON = 2026

# Default game start in UTC for evening games (7 PM EDT = 23 UTC during April DST)
DEFAULT_UTC_HOUR = 23


def _existing_dates() -> set[str]:
    if not CONDITIONS_CSV.exists():
        return set()
    with CONDITIONS_CSV.open() as f:
        return {row["date"] for row in csv.DictReader(f)}


def _load_all_game_results() -> dict[str, list[dict]]:
    """Load historical_slate_results.json → {date: [game_dict, ...]}."""
    with SLATE_RESULTS.open() as f:
        data = json.load(f)
    return {entry["date"]: entry.get("games", []) for entry in data}


def _compute_series_context(
    target_date: str,
    home: str,
    away: str,
    all_results: dict[str, list[dict]],
) -> tuple[int | None, int | None, int | None, int | None]:
    """
    Compute series wins and L10 wins for home/away teams from game history.

    Returns (series_home_wins, series_away_wins, home_l10, away_l10).
    Looks backward from target_date through all available historical dates.
    """
    sorted_dates = sorted(
        (d for d in all_results if d < target_date),
        reverse=True,
    )

    # L10 wins: last 10 completed games for each team (any opponent)
    def _l10(team: str) -> int | None:
        wins = 0
        count = 0
        for d in sorted_dates:
            for g in all_results[d]:
                if g.get("home") == team or g.get("away") == team:
                    winner = g.get("winner")
                    if winner is not None:
                        count += 1
                        if winner == team:
                            wins += 1
                    if count >= 10:
                        return wins
        return wins if count > 0 else None

    # Series wins: consecutive games between home and away immediately before target_date
    def _series(team: str, opp: str) -> int:
        wins = 0
        in_series = False
        for d in sorted_dates:
            found_series_game = False
            for g in all_results[d]:
                if (g.get("home") == team and g.get("away") == opp) or \
                   (g.get("home") == opp and g.get("away") == team):
                    found_series_game = True
                    in_series = True
                    if g.get("winner") == team:
                        wins += 1
            if in_series and not found_series_game:
                break  # Series ended — different opponent on this date
        return wins

    home_series = _series(home, away)
    away_series = _series(away, home)
    home_l10 = _l10(home)
    away_l10 = _l10(away)
    return home_series, away_series, home_l10, away_l10


async def _fetch_team_batting(team: str) -> dict:
    """Return {ops, k_pct} for the team (current-season totals)."""
    team_id = TEAM_MLB_IDS.get(team)
    if not team_id:
        return {}
    data = await get_team_stats(team_id, CURRENT_SEASON)
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    ops_str = s.get("ops", "")
    pa = s.get("plateAppearances", 0)
    so = s.get("strikeOuts", 0)
    return {
        "ops": float(ops_str) if ops_str else None,
        "k_pct": (so / pa) if pa > 0 else None,
    }


async def _fetch_team_bullpen_era(team: str) -> float | None:
    """Return current-season team pitching ERA as bullpen ERA proxy."""
    team_id = TEAM_MLB_IDS.get(team)
    if not team_id:
        return None
    data = await get_team_pitching_stats(team_id, CURRENT_SEASON)
    splits = (data.get("stats") or [{}])[0].get("splits", [])
    if not splits:
        return None
    s = splits[0].get("stat", {})
    era_str = s.get("era", "")
    return float(era_str) if era_str else None


async def _fetch_starter_stats(
    game_date: str,
    home_team: str,
    away_team: str,
) -> tuple[float | None, float | None, float | None, float | None]:
    """
    Fetch home and away starting pitchers from the MLB schedule/boxscore,
    then retrieve their current-season ERA and K/9.

    Returns (home_era, home_k9, away_era, away_k9).
    """
    data = await _get("/schedule", {
        "date": game_date,
        "sportId": 1,
        "teamId": ",".join(str(tid) for tid in TEAM_MLB_IDS.values() if tid),
        "hydrate": "probablePitcher",
    })

    # Build lookup from team abbreviation → pitcher mlb_id
    starter_ids: dict[str, int] = {}
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            teams = g.get("teams", {})
            home_abbr = teams.get("home", {}).get("team", {}).get("abbreviation", "")
            away_abbr = teams.get("away", {}).get("team", {}).get("abbreviation", "")
            # Normalize
            from app.core.constants import canonicalize_team
            home_abbr = canonicalize_team(home_abbr)
            away_abbr = canonicalize_team(away_abbr)

            if home_abbr not in {home_team, away_team} and away_abbr not in {home_team, away_team}:
                continue

            for side, abbr in [("home", home_abbr), ("away", away_abbr)]:
                pitcher = teams.get(side, {}).get("probablePitcher") or {}
                if pitcher.get("id"):
                    starter_ids[abbr] = pitcher["id"]

    async def _pitcher_stats(mlb_id: int) -> tuple[float | None, float | None]:
        stats_data = await get_player_stats(mlb_id, CURRENT_SEASON)
        for person in stats_data.get("people", []):
            for group in person.get("stats", []):
                if group.get("group", {}).get("displayName") == "pitching":
                    splits = group.get("splits", [])
                    if splits:
                        s = splits[0].get("stat", {})
                        era_str = s.get("era", "")
                        ip = float(s.get("inningsPitched", "0") or 0)
                        so = s.get("strikeOuts", 0)
                        era = float(era_str) if era_str else None
                        k9 = round(so / ip * 9, 2) if ip > 0 else None
                        return era, k9
        return None, None

    home_id = starter_ids.get(home_team)
    away_id = starter_ids.get(away_team)

    home_era, home_k9 = await _pitcher_stats(home_id) if home_id else (None, None)
    away_era, away_k9 = await _pitcher_stats(away_id) if away_id else (None, None)
    return home_era, home_k9, away_era, away_k9


async def _build_row(
    target_date: str,
    home: str,
    away: str,
    all_results: dict[str, list[dict]],
    team_batting: dict[str, dict],
    team_pitching: dict[str, float | None],
    starter_stats: dict[str, tuple],
    game_weather: dict | None,
) -> dict:
    series_hw, series_aw, home_l10, away_l10 = _compute_series_context(
        target_date, home, away, all_results
    )

    home_bat = team_batting.get(home, {})
    away_bat = team_batting.get(away, {})
    home_era_p, home_k9_p, away_era_p, away_k9_p = starter_stats.get(
        (home, away), (None, None, None, None)
    )

    return {
        "date": target_date,
        "home_team": home,
        "away_team": away,
        "vegas_total": None,
        "home_moneyline": None,
        "away_moneyline": None,
        "home_starter_era": home_era_p,
        "away_starter_era": away_era_p,
        "home_starter_k9": home_k9_p,
        "away_starter_k9": away_k9_p,
        "home_team_ops": home_bat.get("ops"),
        "away_team_ops": away_bat.get("ops"),
        "home_team_k_pct": home_bat.get("k_pct"),
        "away_team_k_pct": away_bat.get("k_pct"),
        "home_bullpen_era": team_pitching.get(home),
        "away_bullpen_era": team_pitching.get(away),
        "series_home_wins": series_hw,
        "series_away_wins": series_aw,
        "home_team_l10_wins": home_l10,
        "away_team_l10_wins": away_l10,
        "park_team": home,  # home team = park host
        "wind_speed_mph": game_weather["wind_speed_mph"] if game_weather else None,
        "wind_direction": game_weather["wind_direction"] if game_weather else None,
        "temperature_f": game_weather["temperature_f"] if game_weather else None,
    }


async def backfill() -> None:
    if not SLATE_RESULTS.exists():
        print(f"Missing {SLATE_RESULTS} — nothing to backfill.")
        return

    all_results = _load_all_game_results()
    existing = _existing_dates()
    missing_dates = sorted(d for d in all_results if d not in existing)

    if not missing_dates:
        print("historical_conditions.csv already has all dates — nothing to do.")
        return

    print(f"Backfilling {len(missing_dates)} date(s): {missing_dates[0]} → {missing_dates[-1]}")

    # --- Fetch team stats (current-season, shared across all dates) ---
    all_teams: set[str] = set()
    for d in missing_dates:
        for g in all_results[d]:
            all_teams.add(g["home"])
            all_teams.add(g["away"])

    print(f"Fetching batting stats for {len(all_teams)} teams...")
    batting_results = await asyncio.gather(*[_fetch_team_batting(t) for t in all_teams])
    team_batting = dict(zip(all_teams, batting_results))

    print("Fetching bullpen ERA for teams...")
    pitching_results = await asyncio.gather(*[_fetch_team_bullpen_era(t) for t in all_teams])
    team_pitching = dict(zip(all_teams, pitching_results))

    new_rows: list[dict] = []

    for target_date in missing_dates:
        print(f"  Processing {target_date}...")
        games = all_results[target_date]
        if not games:
            print(f"    No games recorded — skipping.")
            continue

        # --- Starter stats per game ---
        print(f"    Fetching starter stats for {len(games)} game(s)...")
        starter_coros = [
            _fetch_starter_stats(target_date, g["home"], g["away"])
            for g in games
        ]
        starter_results = await asyncio.gather(*starter_coros, return_exceptions=True)
        starter_stats: dict[tuple, tuple] = {}
        for g, res in zip(games, starter_results):
            if isinstance(res, Exception):
                print(f"    Starter fetch failed for {g['home']} vs {g['away']}: {res}")
                starter_stats[(g["home"], g["away"])] = (None, None, None, None)
            else:
                starter_stats[(g["home"], g["away"])] = res

        # --- Weather per game ---
        print(f"    Fetching weather for {len(games)} game(s) (archive)...")
        game_date_obj = date.fromisoformat(target_date)
        weather_coros = []
        for g in games:
            park = g["home"]
            coords = STADIUM_COORDINATES.get(park)
            if coords is None:
                weather_coros.append(asyncio.sleep(0))  # placeholder
            else:
                lat, lon = coords
                weather_coros.append(get_game_weather(
                    lat=lat, lon=lon,
                    game_date=game_date_obj,
                    game_utc_hour=DEFAULT_UTC_HOUR,
                    park_team=park,
                    use_archive=True,
                ))
        weather_results = await asyncio.gather(*weather_coros, return_exceptions=True)

        for g, wx in zip(games, weather_results):
            if isinstance(wx, Exception) or STADIUM_COORDINATES.get(g["home"]) is None:
                wx = None
            row = await _build_row(
                target_date, g["home"], g["away"],
                all_results, team_batting, team_pitching, starter_stats, wx,
            )
            new_rows.append(row)

    if not new_rows:
        print("No rows to write.")
        return

    write_header = not CONDITIONS_CSV.exists() or CONDITIONS_CSV.stat().st_size < 50
    with CONDITIONS_CSV.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)

    print(f"\nWrote {len(new_rows)} rows to {CONDITIONS_CSV.name}")
    print("Note: team stats are current-season totals, not as-of-game-date.")
    print("      Vegas lines are NULL — not available from free historical sources.")
    print("Run scripts/calibrate_env_scoring.py to analyse the backfilled data.")


if __name__ == "__main__":
    asyncio.run(backfill())
