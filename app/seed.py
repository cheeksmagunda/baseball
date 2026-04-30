"""Seed the database from existing CSV/JSON data files."""

import csv
import json
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db
from app.models.player import Player, PlayerGameLog, normalize_name
from app.models.slate import Slate, SlateGame, SlatePlayer
from app.models.draft import DraftLineup, DraftSlot

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _parse_float(val: str) -> float | None:
    if not val or val.strip() == "":
        return None
    try:
        return float(val.strip().lstrip("~"))
    except ValueError:
        return None


def _parse_int(val: str) -> int | None:
    if not val or val.strip() == "":
        return None
    try:
        return int(float(val.strip().lstrip("~")))
    except ValueError:
        return None


def _get_or_create_player(db: Session, name: str, team: str, position: str) -> Player:
    norm = normalize_name(name)
    player = db.query(Player).filter_by(name_normalized=norm, team=team).first()
    if not player:
        player = Player(name=name, name_normalized=norm, team=team, position=position)
        db.add(player)
        db.flush()
    return player


def seed_historical_players(db: Session):
    """Load historical_players.csv into players + slate_players."""
    path = DATA_DIR / "historical_players.csv"
    if not path.exists():
        return

    slates: dict[str, Slate] = {}

    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt_str = row["date"]
            if dt_str not in slates:
                dt = date.fromisoformat(dt_str)
                slate = db.query(Slate).filter_by(date=dt).first()
                if not slate:
                    slate = Slate(date=dt, status="completed")
                    db.add(slate)
                    db.flush()
                slates[dt_str] = slate

            slate = slates[dt_str]
            name = row["player_name"]
            team = row["team"]
            position = row["position"]
            player = _get_or_create_player(db, name, team, position)

            rs = _parse_float(row.get("real_score", ""))
            boost = _parse_float(row.get("card_boost", ""))
            drafts = _parse_int(row.get("drafts", ""))
            tv = _parse_float(row.get("total_value", ""))

            sp = SlatePlayer(
                slate_id=slate.id,
                player_id=player.id,
                card_boost=boost or 0.0,
                drafts=drafts,
                real_score=rs,
                total_value=tv,
                is_highest_value=row.get("is_highest_value", "0") == "1",
                is_most_popular=row.get("is_most_popular", "0") == "1",
                is_most_drafted_3x=row.get("is_most_drafted_3x", "0") == "1",
            )
            db.add(sp)

    db.commit()


def seed_winning_drafts(db: Session):
    """Load historical_winning_drafts.csv into draft_lineups + draft_slots."""
    path = DATA_DIR / "historical_winning_drafts.csv"
    if not path.exists():
        return

    lineups: dict[str, DraftLineup] = {}

    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt_str = row["date"]
            rank = row["winner_rank"]
            key = f"{dt_str}_{rank}"

            if key not in lineups:
                dt = date.fromisoformat(dt_str)
                slate = db.query(Slate).filter_by(date=dt).first()
                if not slate:
                    slate = Slate(date=dt, status="completed")
                    db.add(slate)
                    db.flush()

                lineup = DraftLineup(
                    slate_id=slate.id,
                    source=f"winner_rank_{rank}",
                )
                db.add(lineup)
                db.flush()
                lineups[key] = lineup

            lineup = lineups[key]
            slot = DraftSlot(
                lineup_id=lineup.id,
                slot_index=int(row["slot_index"]),
                player_name=row["player_name"],
                team=row.get("team", ""),
                position=row.get("position", ""),
                real_score=_parse_float(row.get("real_score", "")),
                slot_mult=_parse_float(row.get("slot_mult", "")) or 1.0,
                card_boost=_parse_float(row.get("card_boost", "")) or 0.0,
            )
            db.add(slot)

    db.commit()


def seed_slate_results(db: Session):
    """Load historical_slate_results.json into slates + slate_games."""
    path = DATA_DIR / "historical_slate_results.json"
    if not path.exists():
        return

    with open(path) as f:
        data = json.load(f)

    for entry in data:
        dt = date.fromisoformat(entry["date"])
        slate = db.query(Slate).filter_by(date=dt).first()
        if not slate:
            slate = Slate(date=dt, status="completed")
            db.add(slate)
            db.flush()

        slate.game_count = entry.get("game_count", 0)
        slate.season_stage = entry.get("season_stage", "regular")
        slate.notes = entry.get("notes", "")

        for game in entry.get("games", []):
            teams = game.get("teams", game.get("matchup", ""))
            if isinstance(teams, list) and len(teams) == 2:
                away, home = teams[0], teams[1]
            elif isinstance(teams, str) and " vs " in teams:
                away, home = teams.split(" vs ", 1)
            elif isinstance(teams, str) and " @ " in teams:
                away, home = teams.split(" @ ", 1)
            else:
                continue

            sg = SlateGame(
                slate_id=slate.id,
                away_team=away.strip(),
                home_team=home.strip(),
            )
            db.add(sg)

    db.commit()


def seed_hv_game_stats(db: Session):
    """Load hv_player_game_stats.csv into player_game_log."""
    path = DATA_DIR / "hv_player_game_stats.csv"
    if not path.exists():
        return

    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["player_name"]
            team = row.get("team_actual", row.get("team", ""))
            position = row.get("position", "DH")

            player = _get_or_create_player(db, name, team, position)
            game_date = date.fromisoformat(row["date"])

            existing = (
                db.query(PlayerGameLog)
                .filter_by(player_id=player.id, game_date=game_date)
                .first()
            )
            if existing:
                continue

            log = PlayerGameLog(
                player_id=player.id,
                game_date=game_date,
                opponent=row.get("game_result", ""),
                ab=_parse_int(row.get("ab", "")) or 0,
                runs=_parse_int(row.get("r", "")) or 0,
                hits=_parse_int(row.get("h", "")) or 0,
                hr=_parse_int(row.get("hr", "")) or 0,
                rbi=_parse_int(row.get("rbi", "")) or 0,
                bb=_parse_int(row.get("bb", "")) or 0,
                so=_parse_int(row.get("so", "")) or 0,
                ip=_parse_float(row.get("ip", "")) or 0.0,
                er=_parse_int(row.get("er", "")) or 0,
                k_pitching=_parse_int(row.get("k_pitching", "")) or 0,
                decision=row.get("decision", ""),
            )
            db.add(log)

    db.commit()


def run_seed(db: Session = None):
    """Seed reference data from CSV/JSON files.

    Player records are NOT seeded from CSV. They are created organically by
    populate_slate_players() during the T-65 pipeline run from live MLB API
    team rosters — ensuring the Player pool reflects only current-slate
    released rosters, not historical leaderboard participants.

    PlayerGameLog records are NOT seeded from CSV. Game logs come exclusively
    from fetch_player_season_stats() via the live MLB API (source='mlb_api').

    Re-ingestion workflow: the idempotency guard (DraftLineup count == 0)
    prevents double-seeding on normal restarts. To pick up freshly-appended
    CSV rows after a new slate is ingested, delete the database first:
        rm db/ben_oracle.db && python -m app.seed
    On Railway, a fresh DB seeds automatically via the FastAPI lifespan hook.
    """
    close_session = False
    if db is None:
        init_db()
        db = SessionLocal()
        close_session = True

    try:
        if db.query(DraftLineup).count() == 0:
            print("Seeding database from /data/ CSVs + JSON ...")
            seed_winning_drafts(db)
            seed_slate_results(db)
            print("  Seed complete.")
        else:
            print("Database already seeded.")
    finally:
        if close_session:
            db.close()


if __name__ == "__main__":
    run_seed()
