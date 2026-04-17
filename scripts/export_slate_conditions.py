"""Export SlateGame env conditions for a completed slate to historical_conditions.csv.

Run after each slate alongside the manual player/draft data ingest:

    python scripts/export_slate_conditions.py             # today's date
    python scripts/export_slate_conditions.py 2026-04-14  # specific date

Appends one row per game to data/historical_conditions.csv.
Skips if that date is already present (idempotent).
Fails loudly if no SlateGame rows are found for the date.
"""

import csv
import sys
from datetime import date
from pathlib import Path

from app.database import SessionLocal
from app.models.slate import Slate, SlateGame

CONDITIONS_CSV = Path(__file__).resolve().parents[1] / "data" / "historical_conditions.csv"

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


def _existing_dates() -> set[str]:
    if not CONDITIONS_CSV.exists():
        return set()
    with CONDITIONS_CSV.open() as f:
        reader = csv.DictReader(f)
        return {row["date"] for row in reader}


def export(target_date: date) -> None:
    date_str = target_date.isoformat()

    existing = _existing_dates()
    if date_str in existing:
        print(f"Date {date_str} already in historical_conditions.csv — skipping.")
        return

    db = SessionLocal()
    try:
        try:
            slate = db.query(Slate).filter(Slate.date == target_date).first()
        except Exception as e:
            raise SystemExit(f"DB error — has the pipeline run yet? ({e})") from e

        if not slate:
            raise ValueError(f"No Slate found for {date_str}. Run the pipeline first.")

        games = db.query(SlateGame).filter(SlateGame.slate_id == slate.id).all()
        if not games:
            raise ValueError(f"No SlateGame rows found for {date_str}.")
    finally:
        db.close()

    rows = []
    for g in games:
        rows.append({
            "date": date_str,
            "home_team": g.home_team,
            "away_team": g.away_team,
            "vegas_total": g.vegas_total,
            "home_moneyline": g.home_moneyline,
            "away_moneyline": g.away_moneyline,
            "home_starter_era": g.home_starter_era,
            "away_starter_era": g.away_starter_era,
            "home_starter_k9": g.home_starter_k_per_9,
            "away_starter_k9": g.away_starter_k_per_9,
            "home_team_ops": g.home_team_ops,
            "away_team_ops": g.away_team_ops,
            "home_team_k_pct": g.home_team_k_pct,
            "away_team_k_pct": g.away_team_k_pct,
            "home_bullpen_era": g.home_bullpen_era,
            "away_bullpen_era": g.away_bullpen_era,
            "series_home_wins": g.series_home_wins,
            "series_away_wins": g.series_away_wins,
            "home_team_l10_wins": g.home_team_l10_wins,
            "away_team_l10_wins": g.away_team_l10_wins,
            "park_team": g.park_team,
            "wind_speed_mph": g.wind_speed_mph,
            "wind_direction": g.wind_direction,
            "temperature_f": g.temperature_f,
        })

    write_header = not CONDITIONS_CSV.exists() or CONDITIONS_CSV.stat().st_size == 0
    with CONDITIONS_CSV.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} games for {date_str} → {CONDITIONS_CSV.name}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
    else:
        target = date.today()
    export(target)
