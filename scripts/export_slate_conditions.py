"""Export SlateGame env conditions for a completed slate into historical_slate_results.json.

These are the pre-game context signals the T-65 pipeline consumed live —
Vegas lines, starter ERA/K9/WHIP/hand, team OPS/K%, bullpen ERA, series context, weather.
They are patched directly into the game objects of the existing JSON entry for that date.

Capturing them here enables calibration: cross-referencing with player outcomes
(real_score, HV flags in historical_players.csv) by (date, team) shows whether the
env scoring thresholds in constants.py are actually predictive.

Run after each slate alongside the manual player/draft data ingest:

    python scripts/export_slate_conditions.py             # today's date
    python scripts/export_slate_conditions.py 2026-04-14  # specific date

Idempotent — skips if the date's games already have env fields populated.
Fails loudly if the date entry is missing from the JSON or no SlateGame rows exist.
"""

import json
import sys
from datetime import date
from pathlib import Path

from app.database import SessionLocal
from app.models.slate import Slate, SlateGame

RESULTS_JSON = Path(__file__).resolve().parents[1] / "data" / "historical_slate_results.json"


def export(target_date: date) -> None:
    date_str = target_date.isoformat()

    data = json.loads(RESULTS_JSON.read_text())
    entry = next((e for e in data if e["date"] == date_str), None)
    if not entry:
        raise ValueError(
            f"No entry for {date_str} in historical_slate_results.json. "
            "Add the slate envelope first."
        )

    games = entry.get("games") or []
    if games and games[0].get("vegas_total") is not None:
        print(f"Date {date_str} already has env fields — skipping.")
        return

    db = SessionLocal()
    try:
        slate = db.query(Slate).filter(Slate.date == target_date).first()
        if not slate:
            raise ValueError(f"No Slate found for {date_str}. Run the pipeline first.")
        slate_games = db.query(SlateGame).filter(SlateGame.slate_id == slate.id).all()
        if not slate_games:
            raise ValueError(f"No SlateGame rows found for {date_str}.")
    finally:
        db.close()

    lookup = {(g.home_team, g.away_team): g for g in slate_games}

    updated = 0
    for game_obj in games:
        key = (game_obj["home"], game_obj["away"])
        g = lookup.get(key)
        if not g:
            print(f"  Warning: no SlateGame for {key[1]} @ {key[0]}")
            continue
        game_obj.update({
            "vegas_total":        g.vegas_total,
            "home_moneyline":     g.home_moneyline,
            "away_moneyline":     g.away_moneyline,
            "home_starter_era":   g.home_starter_era,
            "away_starter_era":   g.away_starter_era,
            "home_starter_whip":  g.home_starter_whip,
            "away_starter_whip":  g.away_starter_whip,
            "home_starter_k9":    g.home_starter_k_per_9,
            "away_starter_k9":    g.away_starter_k_per_9,
            "home_starter_hand":  g.home_starter_hand,
            "away_starter_hand":  g.away_starter_hand,
            "home_team_ops":      g.home_team_ops,
            "away_team_ops":      g.away_team_ops,
            "home_team_k_pct":    g.home_team_k_pct,
            "away_team_k_pct":    g.away_team_k_pct,
            "home_bullpen_era":   g.home_bullpen_era,
            "away_bullpen_era":   g.away_bullpen_era,
            "series_home_wins":   g.series_home_wins,
            "series_away_wins":   g.series_away_wins,
            "home_team_l10_wins": g.home_team_l10_wins,
            "away_team_l10_wins": g.away_team_l10_wins,
            "wind_speed_mph":     g.wind_speed_mph,
            "wind_direction":     g.wind_direction,
            "temperature_f":      g.temperature_f,
        })
        updated += 1

    RESULTS_JSON.write_text(json.dumps(data, indent=2))
    print(f"Patched {updated}/{len(games)} games for {date_str} in {RESULTS_JSON.name}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
    else:
        target = date.today()
    export(target)
