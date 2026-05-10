"""Backfill per-pitcher post-game boxscore detail onto slate_game.

Reads the same MLB Stats API gumbo feed that backfill_game_externals.py
caches at scripts/output/.game_externals_cache/<game_pk>.json — no
duplicate API calls — and extracts:

  - home/away starter: pitch_count, outs_recorded, hits/runs/er/bb/k/hr_allowed
  - home/away bullpen aggregate: pitchers_used, outs_recorded, pitch_count

These complement the at-T-65 starter ERA / WHIP / K/9 columns already in
slate_game, which are season-aggregate snapshots; the new columns are
the actual outcome of the game's start.

Pure post-game externals — no derivations beyond simple sums of
already-external boxscore values (e.g. bullpen_outs_recorded is the
sum of every relief pitcher's outs).

Usage:
    python scripts/backfill_pitcher_boxscore.py
    python scripts/backfill_pitcher_boxscore.py --force
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-pitcher-boxscore-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".game_externals_cache"
MLB_API = "https://statsapi.mlb.com/api/v1.1"
HTTP_TIMEOUT = 20
MAX_WORKERS = 12

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_pitcher_boxscore")


def fetch_game(game_pk: int) -> dict | None:
    cache_file = CACHE_DIR / f"{game_pk}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    try:
        r = requests.get(f"{MLB_API}/game/{game_pk}/feed/live", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.warning("game_pk=%s fetch failed: %s", game_pk, e)
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(r.text)
    return r.json()


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ip_to_outs(ip_value) -> int | None:
    """MLB API returns IP as a string like '5.2' meaning 5⅔ innings.  The
    decimal is OUTS not tenths — '.0' = 0 outs, '.1' = 1, '.2' = 2."""
    if ip_value is None or ip_value == "":
        return None
    try:
        s = str(ip_value)
        if "." in s:
            full, frac = s.split(".", 1)
            return int(full) * 3 + int(frac[0]) if frac else int(full) * 3
        return int(s) * 3
    except (TypeError, ValueError):
        return None


def _extract_team_pitching(team_box: dict) -> dict:
    """Walk a team's pitchers from boxscore.teams.{home|away}, splitting
    starter (first pitcher in the array) from bullpen (the rest)."""
    pitchers = team_box.get("pitchers", []) or []
    players = team_box.get("players", {}) or {}

    out = {
        "starter_pitch_count": None,
        "starter_outs_recorded": None,
        "starter_hits_allowed": None,
        "starter_runs_allowed": None,
        "starter_er_allowed": None,
        "starter_walks": None,
        "starter_strikeouts": None,
        "starter_hr_allowed": None,
        "bullpen_pitchers_used": 0,
        "bullpen_outs_recorded": 0,
        "bullpen_pitch_count": 0,
    }

    if not pitchers:
        return out

    bullpen_outs = 0
    bullpen_pitches = 0
    bullpen_count = 0
    for idx, pid in enumerate(pitchers):
        p = players.get(f"ID{pid}", {}) or {}
        stats = (p.get("stats") or {}).get("pitching") or {}
        outs = _ip_to_outs(stats.get("inningsPitched"))
        pitches = _safe_int(stats.get("numberOfPitches"))
        if idx == 0:
            out["starter_pitch_count"] = pitches
            out["starter_outs_recorded"] = outs
            out["starter_hits_allowed"] = _safe_int(stats.get("hits"))
            out["starter_runs_allowed"] = _safe_int(stats.get("runs"))
            out["starter_er_allowed"] = _safe_int(stats.get("earnedRuns"))
            out["starter_walks"] = _safe_int(stats.get("baseOnBalls"))
            out["starter_strikeouts"] = _safe_int(stats.get("strikeOuts"))
            out["starter_hr_allowed"] = _safe_int(stats.get("homeRuns"))
        else:
            bullpen_count += 1
            if outs is not None:
                bullpen_outs += outs
            if pitches is not None:
                bullpen_pitches += pitches

    out["bullpen_pitchers_used"] = bullpen_count
    out["bullpen_outs_recorded"] = bullpen_outs if bullpen_count else None
    out["bullpen_pitch_count"] = bullpen_pitches if bullpen_count else None
    return out


def extract_pitcher_boxscore(payload: dict) -> dict:
    if not payload:
        return {}
    bx = (payload.get("liveData") or {}).get("boxscore") or {}
    teams = bx.get("teams") or {}
    home = _extract_team_pitching(teams.get("home", {}) or {})
    away = _extract_team_pitching(teams.get("away", {}) or {})
    out: dict = {}
    for k, v in home.items():
        out[f"home_{k}"] = v
    for k, v in away.items():
        out[f"away_{k}"] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even when fields are already populated")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        if args.force:
            where = "WHERE game_pk IS NOT NULL"
        else:
            where = (
                "WHERE game_pk IS NOT NULL AND home_starter_pitch_count IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, game_pk, game_number FROM slate_game "
            f"{where} ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        log.info("targets: %d", len(targets))
        if not targets:
            log.info("nothing to backfill — re-run with --force to refresh")
            return 0

        unique_pks = sorted({t["game_pk"] for t in targets})
        log.info("unique game_pks: %d", len(unique_pks))

        payloads: dict[int, dict] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(fetch_game, pk): pk for pk in unique_pks}
            for fut in as_completed(futures):
                pk = futures[fut]
                payloads[pk] = fut.result() or {}
        log.info("fetched %d / %d in %.1fs",
                 len(payloads), len(unique_pks), time.time() - t0)

        if args.dry_run:
            sample = next(iter(payloads.values()), None)
            if sample:
                log.info("sample: %s", json.dumps(extract_pitcher_boxscore(sample), indent=2))
            return 0

        updates = 0
        for t in targets:
            payload = payloads.get(t["game_pk"])
            if not payload:
                continue
            ext = extract_pitcher_boxscore(payload)
            if not ext:
                continue
            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"], ext,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d", updates)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        import sys as _sys
        from pathlib import Path as _Path
        _repo = _Path(__file__).resolve().parents[1]
        if str(_repo) not in _sys.path:
            _sys.path.insert(0, str(_repo))
        from scripts.export_historical_csvs import export_all
        export_all()
    sys.exit(rc)
