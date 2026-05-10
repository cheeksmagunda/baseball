"""Backfill per-player external attributes onto player_slate from the
MLB Stats API `/api/v1/people?personIds=...` batch endpoint.

External (no derivations):
  bat_side, pitch_hand, birth_date, mlb_debut_date, height_in,
  weight_lb, birth_country, primary_position_code, jersey_number

These are slowly-changing dimensions; we still snapshot per-slate so the
corpus captures the as-of-date value (e.g. weight changes mid-season,
position shifts).  For synthetic-mlb_id rows (the 5 V9.1-era junk rows
with negative IDs) we leave columns NULL.

Cache: scripts/output/.player_externals_cache/<mlb_id>.json (one file
per unique mlb_id).  Re-runs are cheap.

Usage:
    python scripts/backfill_player_externals.py
    python scripts/backfill_player_externals.py --force
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-player-externals-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".player_externals_cache"
MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 30
BATCH_SIZE = 50      # MLB API supports many at once; 50 keeps URL shorter
MAX_WORKERS = 8

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_player_externals")


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _height_to_inches(height_str: str | None) -> int | None:
    """MLB API returns 'feet inches' format like "6' 2\\"".  Parse to inches."""
    if not height_str:
        return None
    try:
        s = height_str.replace('"', "").replace("'", " ").strip()
        parts = s.split()
        if len(parts) == 1:
            return int(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 12 + int(parts[1])
    except (TypeError, ValueError):
        pass
    return None


def fetch_batch(mlb_ids: list[int]) -> dict[int, dict]:
    """Fetch a batch of person records, returning {mlb_id: person_dict}."""
    out: dict[int, dict] = {}
    # Disk-cache check first — only fetch IDs not in cache
    to_fetch = []
    for mid in mlb_ids:
        cache_file = CACHE_DIR / f"{mid}.json"
        if cache_file.exists():
            try:
                out[mid] = json.loads(cache_file.read_text())
                continue
            except json.JSONDecodeError:
                pass
        to_fetch.append(mid)

    if not to_fetch:
        return out

    ids_str = ",".join(str(x) for x in to_fetch)
    try:
        r = requests.get(
            f"{MLB_API}/people",
            params={"personIds": ids_str},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        people = r.json().get("people", [])
    except Exception as e:
        log.warning("batch fetch failed for %s: %s", to_fetch[:5], e)
        return out

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for p in people:
        pid = _safe_int(p.get("id"))
        if pid is None:
            continue
        out[pid] = p
        (CACHE_DIR / f"{pid}.json").write_text(json.dumps(p))
    return out


def extract_externals(person: dict) -> dict:
    if not person:
        return {}
    primary_pos = (person.get("primaryPosition") or {})
    return {
        "bat_side": (person.get("batSide") or {}).get("code"),
        "pitch_hand": (person.get("pitchHand") or {}).get("code"),
        "birth_date": person.get("birthDate"),
        "mlb_debut_date": person.get("mlbDebutDate"),
        "height_in": _height_to_inches(person.get("height")),
        "weight_lb": _safe_int(person.get("weight")),
        "birth_country": person.get("birthCountry"),
        "primary_position_code": primary_pos.get("abbreviation"),
        "jersey_number": person.get("primaryNumber"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        if args.force:
            where = "WHERE mlb_id > 0"
        else:
            where = "WHERE mlb_id > 0 AND birth_date IS NULL"
        cur = conn.execute(
            f"SELECT DISTINCT mlb_id FROM player_slate {where} ORDER BY mlb_id"
        )
        unique_ids = [r["mlb_id"] for r in cur.fetchall()]
        log.info("unique mlb_ids needing external data: %d", len(unique_ids))
        if not unique_ids:
            log.info("nothing to backfill")
            return 0

        # Batch fetch
        batches = [
            unique_ids[i:i + BATCH_SIZE]
            for i in range(0, len(unique_ids), BATCH_SIZE)
        ]
        log.info("issuing %d batched calls (%d ids/batch)", len(batches), BATCH_SIZE)

        person_by_id: dict[int, dict] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(fetch_batch, b) for b in batches]
            for fut in as_completed(futures):
                person_by_id.update(fut.result())
        log.info("fetched %d / %d persons in %.1fs",
                 len(person_by_id), len(unique_ids), time.time() - t0)

        if args.dry_run:
            sample_id = next(iter(person_by_id), None)
            if sample_id:
                log.info("sample person %d: %s",
                         sample_id, json.dumps(extract_externals(person_by_id[sample_id]), indent=2))
            return 0

        # Apply per-(slate_date, mlb_id) row in player_slate.
        cur = conn.execute(
            "SELECT slate_date, mlb_id FROM player_slate WHERE mlb_id > 0 "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        updates = 0
        missing = 0
        for t in targets:
            person = person_by_id.get(t["mlb_id"])
            if not person:
                missing += 1
                continue
            ext = extract_externals(person)
            if not any(v is not None for v in ext.values()):
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], ext,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (missing person record: %d)", updates, missing)
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
