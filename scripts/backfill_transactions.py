"""Backfill recent MLB transactions (IL moves, call-ups) into label_event.

Phase C add (May 2026).  Source: MLB Stats API
`/api/v1/transactions?startDate=&endDate=`.  Free, no key.

We capture, per (slate_date, mlb_id) where the transaction's effective
date is < slate_date AND >= slate_date - 14 days:
  label_type='transaction'
  label_text=<JSON> with:
    - typeCode (e.g. 'SU'=selected, 'OPT'=optioned, 'IL15'=IL stint,
      'IL60', 'TR'=trade, 'AS'=assigned, 'REL'=released, 'STA'=status,
      'RTN'=returned, 'NUM'=number change, 'CON'=contract, 'OUT'=outright)
    - description
    - effective_date

Storage in label_event (one row per transaction) keeps the schema
flat — calibration scripts can query "show me all transactions for
mlb_id=123 between date_a and date_b" with a single SELECT.

Usage:
    python scripts/backfill_transactions.py
    python scripts/backfill_transactions.py --season 2026
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date as DateType, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-transactions-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".transactions_cache"
MLB_API = "https://statsapi.mlb.com/api/v1/transactions"
HTTP_TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Type codes we deliberately skip:
#   NUM — jersey number changes (no predictive signal; ~75% of transactions)
#   CON — minor-league contract assignments (not predictive at MLB level)
SKIP_TYPE_CODES = {"NUM", "CON"}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_transactions")


def _fetch(start_date: str, end_date: str) -> list[dict]:
    cache_file = CACHE_DIR / f"{start_date}_{end_date}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    try:
        r = requests.get(
            MLB_API,
            params={"startDate": start_date, "endDate": end_date},
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        log.warning("transactions fetch failed: %s", e)
        return []
    if r.status_code != 200:
        log.warning("transactions fetch returned %s", r.status_code)
        return []
    try:
        data = r.json()
    except Exception:
        return []
    txs = data.get("transactions") or []
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(txs))
    return txs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # Pull transactions for the entire season (one request — cheap)
    season_start = f"{args.season}-03-01"
    season_end = f"{args.season}-11-15"
    txs = _fetch(season_start, season_end)
    log.info("loaded %d transactions for %d", len(txs), args.season)

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        if args.force:
            conn.execute("DELETE FROM label_event WHERE label_type='transaction'")
            conn.commit()

        # For each transaction, find every slate_date within [tx_date, tx_date+14d]
        # where the player appears in player_slate, and write a label_event row.
        cur = conn.execute("SELECT slate_date, mlb_id FROM player_slate")
        player_slates: dict[int, list[str]] = {}
        for r in cur.fetchall():
            player_slates.setdefault(r["mlb_id"], []).append(r["slate_date"])

        observed_at = datetime.now(timezone.utc).isoformat()
        upserts = 0
        for tx in txs:
            mid = (tx.get("person") or {}).get("id")
            if not mid:
                continue
            mid = int(mid)
            tx_date = tx.get("effectiveDate") or tx.get("date")
            if not tx_date:
                continue
            try:
                tx_d = DateType.fromisoformat(tx_date[:10])
            except (ValueError, TypeError):
                continue
            type_code = tx.get("typeCode")
            if type_code in SKIP_TYPE_CODES:
                continue
            description = tx.get("description")

            relevant_slates = []
            for sd in player_slates.get(mid, []):
                try:
                    sd_dt = DateType.fromisoformat(sd)
                except (ValueError, TypeError):
                    continue
                days_since = (sd_dt - tx_d).days
                if 0 <= days_since <= 14:
                    relevant_slates.append(sd)
            for sd in relevant_slates:
                payload = json.dumps({
                    "type_code": type_code,
                    "description": description,
                    "effective_date": tx_date[:10],
                })
                historical_db.upsert_label_event(
                    conn,
                    slate_date=sd, mlb_id=mid, label_type="transaction",
                    label_value=None, label_text=payload,
                    source=f"mlb_transactions:{type_code}:{tx_date[:10]}",
                    observed_at=observed_at,
                )
                upserts += 1
        conn.commit()
        log.info("UPSERT label_event(transaction): %d", upserts)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        if not os.environ.get("HISTORICAL_DB"):
            from scripts.export_historical_csvs import export_all
            export_all()
    sys.exit(rc)
