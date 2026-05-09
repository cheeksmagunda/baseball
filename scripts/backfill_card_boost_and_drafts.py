"""
Backfill card_boost and drafts columns onto data/historical_players.csv.

Per CLAUDE.md the live runtime (`app/`) NEVER reads card_boost or drafts —
they remain forbidden inputs to env / trait / EV scoring, enforced by
scripts/audit_live_isolation.py.  This script lives in /scripts/ and writes
ONLY to /data/, so the audit gate stays clean.

Source: realsports.io stats payload, fetched directly from web.realapp.com
using the auth token from scraper/storage_state.json.  The browser-based
scraper (scrape_realsports_daily.py) only handles dates currently visible
on the date strip (~2 weeks); for the full 43-slate corpus we go direct
to the JSON API (date-strip-independent).  Auth headers extracted from a
live page request — see commit log for the discovery process.

We don't re-write any existing column — only add card_boost and drafts —
so the 22 backfilled columns (Statcast, at-slate stats, batting_order,
etc.) are preserved untouched.

The platform's stats endpoint exposes three leaderboard sections per slate
(highestBoostedValuePlayers, popularPlayers, mostCommon3xPlayers — total
~30-50 unique players), which is exactly the row set already present in
historical_players.csv.

TOKEN ROTATION & LOCKOUT SAFETY:
    The platform's `real-request-token` rotates every session.  A hardcoded
    token goes stale and produces 401s after the first session expires.

    This script handles it by running playwright ONCE at startup
    (_capture_live_headers) to grab a fresh token from a live page request,
    then immediately closing the browser.  All subsequent API calls (~86 for
    the full 43-slate corpus) are plain HTTPS GETs with that token — no
    further browser sessions, no UI interaction, no rate-limit risk.

    What NOT to do:
    - Don't re-launch playwright per date: that's 43 browser sessions and
      ~43 login events, which is the pattern most likely to trigger a
      rate-limit or temporary lockout.
    - Don't hardcode the token: it will expire mid-run and cause confusing
      mid-corpus failures.
    - If the API returns 401 mid-run the script exits with a clear error
      message rather than silently skipping dates or retrying with a stale
      token (which would just burn more requests).  Re-run after refreshing
      storage_state.json (python scrape_realsports_daily.py --refresh-auth).

Usage:
    .venv-scraper/bin/python scripts/backfill_card_boost_and_drafts.py
        # processes every date with at least one blank card_boost row
    .venv-scraper/bin/python scripts/backfill_card_boost_and_drafts.py --date 2026-05-07
        # single date (overwrites existing values)
    .venv-scraper/bin/python scripts/backfill_card_boost_and_drafts.py --date 2026-05-07 --force
        # overwrite an already-populated date
"""
import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PLAYERS_CSV = DATA_DIR / "historical_players.csv"


def _find_storage_state() -> Path:
    """Locate storage_state.json — present in the main repo but not git worktrees."""
    candidate = ROOT / "scraper" / "storage_state.json"
    if candidate.exists():
        return candidate
    for parent in ROOT.parents:
        c = parent / "scraper" / "storage_state.json"
        if c.exists():
            return c
    return candidate  # non-existent path → caller emits a clear error


STORAGE_STATE = _find_storage_state()

sys.path.insert(0, str(ROOT))
from scripts.scrape_realsports_daily import (  # noqa: E402
    _fetch_mlb_player_info,
    _name_for,
    _name_normalize,
    _team_key_normalize,
)

NEW_COLUMNS = ("card_boost", "drafts")

DEVICE_UUID = "569aead7-499e-46d9-870e-57806e88756e"  # from storage_state ls.realdeviceuuid
API_BASE = "https://web.realapp.com"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_boost_drafts")


def _capture_live_headers() -> dict:
    """Spin up playwright once to capture a fresh real-request-token + the
    real-auth-info header from a live page request.  The request-token
    rotates per-session, so we always grab it fresh at startup.

    Side-effect: re-saves scraper/storage_state.json with any session refresh.
    """
    from playwright.sync_api import sync_playwright

    log.info("Capturing fresh auth headers via playwright ...")
    captured: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 599, "height": 868},
            storage_state=str(STORAGE_STATE),
        )
        page = context.new_page()

        def on_request(req):
            if "/home/mlb/" in req.url and "next" in req.url and "headers" not in captured:
                captured["headers"] = dict(req.headers)

        page.on("request", on_request)
        page.goto("https://realsports.io/", wait_until="networkidle", timeout=30000)
        time.sleep(2)
        page.get_by_text("MLB", exact=True).first.click(timeout=15000)
        time.sleep(3)
        context.storage_state(path=str(STORAGE_STATE))  # persist any refresh
        browser.close()

    if "headers" not in captured:
        sys.exit("ERROR: failed to capture live request headers")
    h = captured["headers"]
    log.info(f"  captured request-token={h.get('real-request-token', '?')[:8]}...")
    return {
        "real-request-token": h["real-request-token"],
        "real-auth-info": h["real-auth-info"],
        "real-version": h.get("real-version", "31"),
        "real-device-type": "desktop_web",
        "real-device-name": h.get("real-device-name", "Mozilla/5.0"),
        "real-device-uuid": DEVICE_UUID,
        "user-agent": h.get("user-agent", "Mozilla/5.0"),
        "accept": "application/json",
        "content-type": "application/json",
        "referer": "https://realsports.io/",
        "origin": "https://realsports.io",
    }


def _http_get(url: str, headers: dict, attempts: int = 3) -> dict:
    """GET with simple retry on transient errors."""
    last_err = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code == 401:
                sys.exit("ERROR: 401 from realapp API — refresh scraper/storage_state.json")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            log.warning(f"  HTTP attempt {i+1}/{attempts} failed for {url}: {e}")
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"HTTP failed after {attempts} attempts: {last_err}")


def fetch_stats_payload_direct(target_date: str, headers: dict) -> dict:
    """
    Direct-API replacement for fetch_slate_payloads(): two HTTP calls, no
    browser.  Returns the same stats payload shape (with draftStats sections).
    """
    daily_url = f"{API_BASE}/home/mlb/day/next?cohort=0&day={target_date}"
    daily = _http_get(daily_url, headers)
    contests = (
        daily.get("content", {})
        .get("config", {})
        .get("dailyDraftInfo", {})
        .get("contests", [])
    )
    if not contests:
        raise RuntimeError(f"no contests in daily payload for {target_date}")
    contest_id = contests[0]["id"]
    log.info(f"  contestId={contest_id}")
    stats_url = f"{API_BASE}/games/playerratingcontest/{contest_id}/stats"
    stats = _http_get(stats_url, headers)
    return stats


def parse_drafts_value(raw) -> int:
    """
    'Drafts' field is either int (1, 311, 893) or 'k'-suffixed string
    ('1.1k', '14.6k').  Normalize to int draft count.
    """
    if isinstance(raw, int):
        return raw
    s = str(raw).strip().lower()
    if not s:
        raise ValueError("empty drafts value")
    if s.endswith("k"):
        return int(round(float(s[:-1]) * 1000))
    return int(s)


def build_boost_drafts_lookup(stats_payload: dict, player_info: dict) -> dict:
    """
    Build (player_name, team) -> {'card_boost': float, 'drafts': int} from
    the three leaderboard sections.  Skips the unnamed 'My draft' section.

    Boost is per-(player,date) and consistent across sections.  Drafts varies:
    HV/3X show subset counts (drafters at a specific boost level), MP shows
    TOTAL drafters — we want the total → take max across sections.

    Each player is registered under BOTH name forms (MLB-API full name AND
    platform display name) because the historical CSV is a mix of both.
    Skips players with team=None (rare, happens for in-flight trades).
    """
    LEADERBOARD_SECTIONS = {
        "highestBoostedValuePlayers",
        "popularPlayers",
        "mostCommon3xPlayers",
    }
    lookup: dict[tuple[str, str], dict] = {}

    for sec in stats_payload.get("draftStats", []):
        if sec.get("sectionName") not in LEADERBOARD_SECTIONS:
            continue
        for p in sec["players"]:
            pl = p.get("player") or {}
            tm = p.get("team") or {}
            if not tm.get("key"):
                # Trade/orphan case: payload has player but no team association.
                # Can't match against CSV (which always has a team) — skip.
                log.warning(f"    skipping team=None player: "
                            f"{pl.get('displayName', '?')} (id={pl.get('id')})")
                continue
            team = _team_key_normalize(tm["key"])
            full_name = _name_for(pl["id"], player_info, pl["displayName"])
            display_name = _name_normalize(pl.get("displayName") or "")

            boost = float(p["multiplierBonus"])
            drafts_raw = next(
                (x["value"] for x in p.get("displayStats", []) if x.get("label") == "Drafts"),
                None,
            )
            if drafts_raw is None:
                log.warning(f"    no Drafts displayStat for {full_name} ({team})")
                continue
            drafts = parse_drafts_value(drafts_raw)

            value = {"card_boost": boost, "drafts": drafts}
            for key in {(full_name, team), (display_name, team)}:
                if key in lookup:
                    prior = lookup[key]
                    if abs(prior["card_boost"] - boost) > 0.01:
                        log.warning(f"    {full_name} ({team}): boost drift "
                                    f"{prior['card_boost']} vs {boost}")
                    lookup[key]["drafts"] = max(prior["drafts"], drafts)
                else:
                    lookup[key] = dict(value)
    return lookup


def _collect_player_ids_from_stats(stats_payload: dict) -> list[int]:
    """All unique playerIds we'll need MLB-API names for."""
    ids: set[int] = set()
    for sec in stats_payload.get("draftStats", []):
        for p in sec.get("players", []):
            pid = p.get("player", {}).get("id")
            if pid is not None:
                ids.add(pid)
    return list(ids)


def load_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def date_already_populated(rows: list[dict], target_date: str) -> bool:
    """A date is 'already populated' if every row for that date has a
    non-blank card_boost AND non-blank drafts."""
    date_rows = [r for r in rows if r.get("date") == target_date]
    if not date_rows:
        return False
    return all(r.get("card_boost") not in (None, "") and r.get("drafts") not in (None, "")
               for r in date_rows)


def backfill_one_date(target_date: str, rows: list[dict], headers: dict) -> tuple[int, int]:
    log.info(f"--- {target_date} ---")
    stats = fetch_stats_payload_direct(target_date, headers)

    player_ids = _collect_player_ids_from_stats(stats)
    player_info = _fetch_mlb_player_info(player_ids)

    boost_drafts = build_boost_drafts_lookup(stats, player_info)
    log.info(f"  payload: {len(boost_drafts)} unique leaderboard players")

    # Name-only payload index for fallback (handles CSV team drift /
    # in-flight trade where the payload reports the player's CURRENT team
    # but the CSV captured the team-as-of-slate).  Only used when the
    # strict (name, team) match fails.
    payload_by_name: dict[str, dict] = {}
    name_collisions: set[str] = set()
    for (name, _team), val in boost_drafts.items():
        if name in payload_by_name:
            # Different teams under the same name → ambiguous; refuse fallback.
            if payload_by_name[name] != val:
                name_collisions.add(name)
        else:
            payload_by_name[name] = val

    # Clear existing values for this date so a rejected fallback leaves the
    # row blank rather than retaining a stale (potentially wrong) value
    # from a prior run.
    for row in rows:
        if row.get("date") == target_date:
            row["card_boost"] = ""
            row["drafts"] = ""

    matched = 0
    matched_via_fallback = 0
    missed = 0
    missed_examples: list[tuple[str, str]] = []
    payload_keys = set(boost_drafts.keys())
    csv_keys = set()
    for row in rows:
        if row.get("date") != target_date:
            continue
        normalized_name = _name_normalize(row["player_name"])
        key = (normalized_name, row["team"])
        csv_keys.add(key)
        if key in boost_drafts:
            row["card_boost"] = boost_drafts[key]["card_boost"]
            row["drafts"] = boost_drafts[key]["drafts"]
            matched += 1
        elif normalized_name in payload_by_name and normalized_name not in name_collisions:
            val = payload_by_name[normalized_name]
            # Verify the fallback isn't a wrong-attribution: CSV total_value
            # must reconcile against real_score × (2 + boost).  If drift > 1.0,
            # we've matched a same-name DIFFERENT player (the CSV's team field
            # is wrong AND the player happens to share a name with someone
            # currently in the API).  Reject the match — boost-during-slate
            # changes are tiny (max ~0.05 drift); >1.0 means wrong player.
            try:
                tv = float(row.get("total_value", "") or 0)
                rs = float(row.get("real_score", "") or 0)
                recon = rs * (2.0 + val["card_boost"])
                if tv and abs(tv - recon) > 1.0:
                    log.warning(f"    rejecting fallback for {row['player_name']} "
                                f"({row['team']}): tv={tv} vs recon={recon:.2f}")
                    missed += 1
                    if len(missed_examples) < 5:
                        missed_examples.append((row["player_name"], row["team"]))
                    continue
            except (ValueError, TypeError):
                pass
            row["card_boost"] = val["card_boost"]
            row["drafts"] = val["drafts"]
            matched += 1
            matched_via_fallback += 1
        else:
            missed += 1
            if len(missed_examples) < 5:
                missed_examples.append((row["player_name"], row["team"]))

    unmatched = sorted(payload_keys - csv_keys)
    log.info(f"  CSV rows for {target_date}: {len(csv_keys)} "
             f"(matched {matched} [{matched_via_fallback} via name-only fallback], "
             f"missed {missed})")
    if missed_examples:
        log.info(f"  missed CSV examples: {missed_examples}")
    if unmatched:
        log.info(f"  payload had {len(unmatched)} players not in CSV (e.g. {unmatched[:3]})")
    return matched, missed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD; default = every date with blank card_boost")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even for already-populated dates")
    args = ap.parse_args()

    if not PLAYERS_CSV.exists():
        sys.exit(f"ERROR: {PLAYERS_CSV} not found")
    if not STORAGE_STATE.exists():
        sys.exit(f"ERROR: {STORAGE_STATE} not found")

    headers = _capture_live_headers()

    fieldnames, rows = load_csv(PLAYERS_CSV)
    log.info(f"Loaded {len(rows)} rows from {PLAYERS_CSV.name}")

    schema_changed = False
    for col in NEW_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
            schema_changed = True
    if schema_changed:
        log.info(f"Schema: added columns {NEW_COLUMNS} (existing rows get blanks until matched)")
        for row in rows:
            for col in NEW_COLUMNS:
                row.setdefault(col, "")

    all_dates = sorted({r["date"] for r in rows})
    if args.date:
        if args.date not in all_dates:
            sys.exit(f"ERROR: {args.date} not present in CSV")
        target_dates = [args.date]
    else:
        target_dates = [d for d in all_dates
                        if args.force or not date_already_populated(rows, d)]
    log.info(f"Will process {len(target_dates)} of {len(all_dates)} dates")

    if not target_dates:
        log.info("Nothing to do.")
        return

    total_matched = 0
    total_missed = 0
    failed_dates: list[str] = []

    for i, d in enumerate(target_dates, 1):
        log.info(f"\n[{i}/{len(target_dates)}] {d}")
        try:
            matched, missed = backfill_one_date(d, rows, headers)
            total_matched += matched
            total_missed += missed
            write_csv(PLAYERS_CSV, fieldnames, rows)
        except Exception as e:
            log.error(f"  FAILED on {d}: {e!r}")
            failed_dates.append(d)
            continue

    log.info(f"\n=== Summary ===")
    log.info(f"Processed: {len(target_dates) - len(failed_dates)} / {len(target_dates)}")
    log.info(f"Matched rows: {total_matched}")
    log.info(f"Missed rows: {total_missed}")
    if failed_dates:
        log.info(f"Failed dates ({len(failed_dates)}): {failed_dates}")
        sys.exit(1)


if __name__ == "__main__":
    main()
    # Step 3 hook: re-ingest CSVs into data/historical.db so downstream
    # readers (Step 4) see the new values.  Cheap (~1s) and idempotent.
    import sys as _sys
    from pathlib import Path as _Path
    _repo = _Path(__file__).resolve().parents[1]
    if str(_repo) not in _sys.path:
        _sys.path.insert(0, str(_repo))
    from app.core import historical_db
    historical_db.rebuild_from_csvs_and_export()
