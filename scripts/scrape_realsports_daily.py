"""
Daily realsports.io scraper for MLB.

Scrapes one day's slate data from https://realsports.io and appends rows to:
  - data/historical_players.csv          (HV/MP/3X player leaderboards)
  - data/historical_winning_drafts.csv   (top 20 lineups, slot-by-slot)
  - data/historical_slate_results.json   (game results envelope)
  - data/hv_player_game_stats.csv        (one row per HV player; box stats blank)

No new variables beyond what the existing files track.

Usage:
    # Default: scrape yesterday in EST
    python scripts/scrape_realsports_daily.py

    # Specific date (must be a past date with completed games)
    python scripts/scrape_realsports_daily.py --date 2026-04-25

    # Re-scrape (overwrite existing rows for that date)
    python scripts/scrape_realsports_daily.py --date 2026-04-25 --force

    # Refresh auth state interactively (only if storage_state.json is stale)
    python scripts/scrape_realsports_daily.py --refresh-auth

Dependencies: playwright (and the project's existing dependencies).

Auth: requires scraper/storage_state.json (created via --refresh-auth or
login_save_state.py). The token in there has indefinite-ish lifetime; if you
get 401s, run with --refresh-auth.
"""
import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

# --- Config ---------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SCRAPER_DIR = ROOT / "scraper"
SCRAPER_DIR.mkdir(exist_ok=True)
STATE_FILE = SCRAPER_DIR / "storage_state.json"

PLAYERS_CSV = DATA_DIR / "historical_players.csv"
DRAFTS_CSV = DATA_DIR / "historical_winning_drafts.csv"
RESULTS_JSON = DATA_DIR / "historical_slate_results.json"
HV_STATS_CSV = DATA_DIR / "hv_player_game_stats.csv"

# Slot multipliers per Real Sports MLB DFS (5 slots, position 0 = pitcher anchor)
SLOT_MULTIPLIERS = [2.0, 1.8, 1.6, 1.4, 1.2]
BASE_SLOT_MULT = 2.0  # CLAUDE.md: total_value = real_score * (2 + card_boost)

# Username/password only used by --refresh-auth.  Read from env to keep
# secrets out of source.
USERNAME = os.environ.get("BO_REALSPORTS_USERNAME", "cheeksmagunda")
PASSWORD = os.environ.get("BO_REALSPORTS_PASSWORD", "")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("scraper")


# --- Auth ----------------------------------------------------------------

def refresh_auth_state() -> None:
    """Login fresh and save storage_state.json. Use sparingly to avoid rate limits."""
    if not PASSWORD:
        sys.exit("ERROR: BO_REALSPORTS_PASSWORD env var is required for --refresh-auth")
    log.info("Refreshing auth state (logging in to realsports.io) ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto("https://realsports.io/", wait_until="networkidle", timeout=30000)
        time.sleep(2)
        ins = [i for i in page.query_selector_all("input") if i.is_visible()]
        if len(ins) < 2:
            sys.exit(f"ERROR: expected 2 visible inputs at login, got {len(ins)}")
        ins[0].fill(USERNAME)
        ins[1].fill(PASSWORD)
        ins[1].press("Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        time.sleep(4)
        body = page.evaluate("() => document.body.innerText")[:300]
        if "Attempts exceeded" in body or "Forgot password" in body[:100]:
            sys.exit(f"ERROR: login rate-limited.  Body: {body!r}")
        if "MLB" not in body and "Home" not in body:
            sys.exit(f"ERROR: login appeared to fail.  Body: {body!r}")
        context.storage_state(path=str(STATE_FILE))
        log.info(f"  saved auth state to {STATE_FILE}")
        browser.close()


# --- Scraping ------------------------------------------------------------

def _humanize_url(url: str) -> str:
    base = url.split("?")[0]
    qs = ("?" + url.split("?", 1)[1][:60]) if "?" in url else ""
    return base + qs


def fetch_slate_payloads(target_date: str) -> dict[str, Any]:
    """
    Open the Real Sports app, navigate to MLB → target_date, click stats and
    comments icons. Return the three captured JSON payloads we need:
      - daily   = /home/mlb/day/next?day=...
      - stats   = /games/playerratingcontest/{contestId}/stats   (HV/MP/3X)
      - entries = /games/playerratingcontest/{contestId}/entries (top 20 lineups)
    """
    if not STATE_FILE.exists():
        sys.exit(f"ERROR: {STATE_FILE} not found. Run with --refresh-auth first.")

    captured: dict[str, dict] = {}  # url_pattern -> body json

    def on_response(resp):
        # Only catch the daily payload here -- stats and entries use expect_response
        # which is race-free.
        url = resp.url
        if "realapp.com" not in url:
            return
        if "/home/mlb/day/next" in url and target_date in url:
            captured["daily"] = json.loads(resp.text())
            log.info(f"  captured daily payload ({len(resp.text())}b)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 599, "height": 868},  # mobile viewport — easier nav
            storage_state=str(STATE_FILE),
        )
        page = context.new_page()
        page.on("response", on_response)

        log.info("Opening realsports.io ...")
        page.goto("https://realsports.io/", wait_until="networkidle", timeout=30000)
        time.sleep(2)

        log.info("Clicking MLB tab ...")
        page.get_by_text("MLB", exact=True).first.click(timeout=15000)
        time.sleep(2)
        page.wait_for_load_state("networkidle", timeout=10000)

        # Date label format on the strip: "Apr 25"
        date_obj = datetime.strptime(target_date, "%Y-%m-%d")
        date_label = date_obj.strftime("%b %-d")  # "Apr 25"
        log.info(f"Clicking date {date_label!r} ...")
        # Be robust: there may be multiple matches (date strip + headers).
        # Use the date strip — it's in the row right under the sport tabs.
        date_locator = page.get_by_text(date_label, exact=True)
        count = date_locator.count()
        log.info(f"  found {count} elements matching {date_label!r}")
        # Click the first match; if more matches exist, may need .first or nth
        try:
            date_locator.first.click(timeout=15000)
        except Exception as e:
            log.error(f"  date click failed: {e}")
            page.screenshot(path="/tmp/scraper_date_fail.png", full_page=True)
            raise
        time.sleep(3)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        body = page.evaluate("() => document.body.innerText")
        log.info(f"  body sample after date click: {body[:200]!r}")

        if "daily" not in captured:
            sys.exit(f"ERROR: failed to capture daily payload for {target_date}")

        # The 'View results' button on the daily page navigates AWAY to a
        # leaderboard view. The chart icon (right of View results) opens the
        # HV/MP/3X stats modal in-place. So order matters:
        #   1. Click chart → /stats (modal pops, dismiss with Cancel)
        #   2. Click View results → /entries (navigates away, doesn't matter -- we're done)
        log.info("Locating chart icon next to View results ...")
        vr = page.get_by_text("View results", exact=True).first
        vr_box = vr.bounding_box()
        if not vr_box:
            sys.exit("ERROR: 'View results' button not found on page")

        svgs = page.evaluate(f"""
            () => {{
                const yMin = {vr_box['y']} - 20;
                const yMax = {vr_box['y'] + vr_box['height']} + 20;
                return [...document.querySelectorAll('svg')]
                    .filter(s => {{
                        const r = s.getBoundingClientRect();
                        return r.width > 0 && r.y >= yMin && r.y <= yMax
                            && r.x > {vr_box['x'] + vr_box['width']};
                    }})
                    .map(s => {{
                        const r = s.getBoundingClientRect();
                        return {{x: r.x, y: r.y, w: r.width, h: r.height}};
                    }})
                    .sort((a, b) => a.x - b.x);
            }}
        """)
        if not svgs:
            sys.exit("ERROR: no SVG icons found right of View results")

        chart_svg = svgs[0]
        log.info(f"  chart icon at ({chart_svg['x']:.0f}, {chart_svg['y']:.0f})")

        # Step A: Click chart icon → /stats
        log.info("Clicking chart icon (HV/MP/3X stats) ...")
        cx = chart_svg["x"] + chart_svg["w"]/2
        cy = chart_svg["y"] + chart_svg["h"]/2
        with page.expect_response(
            lambda r: "/games/playerratingcontest/" in r.url and r.url.split("?")[0].endswith("/stats"),
            timeout=15000,
        ) as ev:
            page.mouse.move(cx, cy)
            time.sleep(0.2)
            page.mouse.down()
            time.sleep(0.05)
            page.mouse.up()
        stats_resp = ev.value
        captured["stats"] = json.loads(stats_resp.text())
        log.info(f"  captured stats payload ({len(stats_resp.text())}b)")

        # Close the stats modal so View results is clickable again
        try:
            page.get_by_text("Cancel", exact=True).first.click(timeout=3000)
            time.sleep(1.5)
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            log.warning("  Cancel button not found - continuing anyway")

        # Step B: Click 'View results' → /entries
        log.info("Clicking 'View results' (top-20 lineups) ...")
        vr2 = page.get_by_text("View results", exact=True).first
        with page.expect_response(
            lambda r: "/games/playerratingcontest/" in r.url and r.url.split("?")[0].endswith("/entries"),
            timeout=15000,
        ) as ev:
            vr2.click(timeout=8000)
        entries_resp = ev.value
        captured["entries"] = json.loads(entries_resp.text())
        log.info(f"  captured entries payload ({len(entries_resp.text())}b)")

        browser.close()

    return captured


# --- Parsers -------------------------------------------------------------

def _name_normalize(s: str) -> str:
    """ASCII-fold so 'J. Rodríguez' becomes 'J. Rodriguez' for stable joins."""
    import unicodedata
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def _fetch_mlb_positions(player_ids: list[int]) -> dict[int, str]:
    """
    Batch-query the MLB Stats API for primary positions of the given player IDs.
    Real Sports player IDs ARE MLB player IDs.

    Returns {mlb_id: 'P'} for pitchers, {mlb_id: 'OF'} for everyone else
    (including Two-Way Players like Ohtani — they hit more often in DFS).

    Uses /people?personIds=... batch endpoint. Splits into chunks of 50 to
    keep URLs short.
    """
    import requests
    lookup: dict[int, str] = {}
    unique_ids = sorted(set(player_ids))
    if not unique_ids:
        return lookup
    CHUNK = 50
    for i in range(0, len(unique_ids), CHUNK):
        chunk = unique_ids[i:i + CHUNK]
        ids_str = ",".join(str(x) for x in chunk)
        try:
            r = requests.get(
                "https://statsapi.mlb.com/api/v1/people",
                params={"personIds": ids_str},
                timeout=15,
            )
            r.raise_for_status()
            for p in r.json().get("people", []):
                pid = p.get("id")
                pp_type = p.get("primaryPosition", {}).get("type", "")
                if pid is not None:
                    lookup[pid] = "P" if pp_type == "Pitcher" else "OF"
        except Exception as e:
            log.warning(f"  MLB API position lookup failed for chunk {i}-{i+CHUNK}: {e}")
    log.info(f"  fetched MLB positions for {len(lookup)}/{len(unique_ids)} players "
             f"(P={sum(1 for v in lookup.values() if v=='P')}, "
             f"OF={sum(1 for v in lookup.values() if v=='OF')})")
    return lookup


def _build_team_lookup(daily_payload: dict, stats_payload: dict) -> dict[int, str]:
    """
    Build teamId → 3-letter key (e.g. 113 → 'CIN') from the games array (which
    has full home/awayTeam objects) plus the stats payload's player.team objects
    (which cover any teams not in today's games).
    """
    lookup: dict[int, str] = {}
    for g in daily_payload.get("content", {}).get("games", []):
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side) or {}
            if t.get("id") and t.get("key"):
                lookup[t["id"]] = t["key"]
    for sec in stats_payload.get("draftStats", []):
        for p in sec.get("players", []):
            t = p.get("team") or {}
            if t.get("id") and t.get("key"):
                lookup[t["id"]] = t["key"]
    return lookup


def _collect_all_player_ids(stats_payload: dict, entries: list[dict]) -> list[int]:
    """All unique playerIds we'll need positions for."""
    ids: set[int] = set()
    for sec in stats_payload.get("draftStats", []):
        for p in sec.get("players", []):
            pid = p.get("player", {}).get("id")
            if pid is not None:
                ids.add(pid)
    for e in entries:
        for player in e.get("additionalInfo", {}).get("lineup", []):
            pid = player.get("playerId")
            if pid is not None:
                ids.add(pid)
    return list(ids)


def _position_for(player_id: int, position_lookup: dict[int, str]) -> str:
    """Default to 'OF' if MLB API didn't have data (e.g. very recent call-up)."""
    return position_lookup.get(player_id, "OF")


def parse_players(stats_payload: dict, position_lookup: dict[int, str],
                  target_date: str) -> list[dict]:
    """
    historical_players.csv columns:
      date, player_name, team, position, real_score, card_boost, drafts,
      total_value, is_highest_value, is_most_popular, is_most_drafted_3x

    Dedup by (player_name, team), merging flags from HV/MP/3X sections.
    """
    by_key: dict[tuple[str, str], dict] = {}

    for sec in stats_payload.get("draftStats", []):
        section_name = sec["sectionName"]  # 'highestBoostedValuePlayers' / 'popularPlayers' / 'mostCommon3xPlayers'
        flag_field = {
            "highestBoostedValuePlayers": "is_highest_value",
            "popularPlayers": "is_most_popular",
            "mostCommon3xPlayers": "is_most_drafted_3x",
        }.get(section_name)
        if flag_field is None:
            continue

        for p in sec["players"]:
            pl = p["player"]
            tm = p["team"]
            name = _name_normalize(pl["displayName"])
            team = tm["key"]
            key = (name, team)

            real_score = float(p["value"])
            boost = float(p["multiplierBonus"])
            drafts = int(p["count"])
            total_value = round(real_score * (BASE_SLOT_MULT + boost), 4)
            position = _position_for(pl["id"], position_lookup)

            if key not in by_key:
                by_key[key] = {
                    "date": target_date,
                    "player_name": name,
                    "team": team,
                    "position": position,
                    "real_score": real_score,
                    "card_boost": boost,
                    "drafts": drafts,
                    "total_value": total_value,
                    "is_highest_value": 0,
                    "is_most_popular": 0,
                    "is_most_drafted_3x": 0,
                }
            else:
                # Same player, take max drafts and most-trustworthy values
                row = by_key[key]
                row["drafts"] = max(row["drafts"], drafts)
                # Real score should be identical across sections for same player
                # (sanity: warn if drift)
                if abs(row["real_score"] - real_score) > 0.001:
                    log.warning(f"    {name} ({team}): real_score drift "
                                f"{row['real_score']} vs {real_score}")

            by_key[key][flag_field] = 1

    return list(by_key.values())


def parse_winning_drafts(entries: list[dict], position_lookup: dict[int, str],
                         team_lookup: dict[int, str], target_date: str) -> list[dict]:
    """
    historical_winning_drafts.csv columns:
      date, winner_rank, slot_index, player_name, team, position,
      real_score, slot_mult, card_boost
    """
    rows = []
    missing_teams: set[int] = set()
    for entry in entries:
        rank = entry["rank"]
        for player in entry.get("additionalInfo", {}).get("lineup", []):
            # Some lineups have ghost / unfilled slots (no value, no name).
            if "value" not in player or "displayName" not in player:
                log.warning(f"  rank {rank} order {player.get('order','?')}: stub player, skipping")
                continue
            order = player["order"]
            slot_index = order + 1
            slot_mult = SLOT_MULTIPLIERS[order]
            pid = player.get("playerId")
            tid = player.get("teamId")
            team_key = team_lookup.get(tid, "")
            if not team_key and tid is not None:
                missing_teams.add(tid)
            rows.append({
                "date": target_date,
                "winner_rank": rank,
                "slot_index": slot_index,
                "player_name": _name_normalize(player["displayName"]),
                "team": team_key,
                "position": _position_for(pid, position_lookup),
                "real_score": float(player["value"]),
                "slot_mult": slot_mult,
                "card_boost": float(player.get("multiplierBonus", 0)),
            })
    if missing_teams:
        log.warning(f"  no team_key for teamIds: {sorted(missing_teams)}")
    return rows


def parse_games(daily_payload: dict, target_date: str) -> dict:
    """
    historical_slate_results.json envelope:
      {date, game_count, games[], season_stage, source, saved_at, notes}
    """
    content = daily_payload["content"]
    games_in = content.get("games", [])
    games_out = []
    for g in games_in:
        ht, at = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        hk, ak = ht.get("key", ""), at.get("key", "")
        hs = g.get("homeTeamScore")
        a_s = g.get("awayTeamScore")
        if hs is None or a_s is None:
            # game not finalized; skip env field
            continue
        winner_key = hk if hs > a_s else ak
        loser_key = ak if hs > a_s else hk
        winner_score = max(hs, a_s)
        loser_score = min(hs, a_s)
        games_out.append({
            "home": hk, "away": ak,
            "home_score": hs, "away_score": a_s,
            "winner": winner_key, "loser": loser_key,
            "winner_score": winner_score, "loser_score": loser_score,
        })

    return {
        "date": target_date,
        "game_count": len(games_in),
        "games": games_out,
        "season_stage": "regular-season",
        "source": "realsports_scraper",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "notes": f"Scraped via realsports.io on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
    }


def parse_hv_stats_blank(stats_payload: dict, position_lookup: dict[int, str],
                         target_date: str, games_envelope: dict) -> list[dict]:
    """
    hv_player_game_stats.csv columns:
      date, player_name, team_actual, position, real_score, card_boost,
      game_result, ab, r, h, hr, rbi, bb, so, ip, er, k_pitching, decision, notes

    We populate identifying fields + game_result (from the games envelope)
    and leave per-player batting/pitching stats blank — those are backfilled
    later from MLB Stats API by scripts/backfill_slate_results_and_hv_stats.py.
    """
    rows = []
    # Build team -> game_result mapping
    team_to_game = {}
    for g in games_envelope["games"]:
        result = f"{g['home']} {g['home_score']} {g['away']} {g['away_score']}"
        team_to_game[g["home"]] = result
        team_to_game[g["away"]] = result

    for sec in stats_payload.get("draftStats", []):
        if sec["sectionName"] != "highestBoostedValuePlayers":
            continue
        for p in sec["players"]:
            pl = p["player"]
            tm = p["team"]
            team = tm["key"]
            name = _name_normalize(pl["displayName"])
            rows.append({
                "date": target_date,
                "player_name": name,
                "team_actual": team,
                "position": _position_for(pl["id"], position_lookup),
                "real_score": float(p["value"]),
                "card_boost": float(p["multiplierBonus"]),
                "game_result": team_to_game.get(team, ""),
                "ab": "", "r": "", "h": "", "hr": "", "rbi": "", "bb": "", "so": "",
                "ip": "", "er": "", "k_pitching": "", "decision": "",
                "notes": "auto-scraped (box score backfill pending)",
            })
    return rows


# --- File writers --------------------------------------------------------

def _date_present_in_csv(path: Path, target_date: str) -> bool:
    if not path.exists():
        return False
    with open(path) as f:
        for row in csv.DictReader(f):
            if row.get("date") == target_date:
                return True
    return False


def append_csv(path: Path, rows: list[dict], target_date: str, force: bool):
    if not rows:
        log.warning(f"  no rows to write to {path.name}")
        return
    fieldnames = list(rows[0].keys())
    if path.exists():
        if _date_present_in_csv(path, target_date):
            if not force:
                log.warning(f"  {path.name}: {target_date} already present, skipping (use --force to overwrite)")
                return
            log.info(f"  {path.name}: removing existing rows for {target_date}")
            with open(path) as f:
                kept = [r for r in csv.DictReader(f) if r.get("date") != target_date]
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(kept)
        # Append rows
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writerows(rows)
    else:
        log.info(f"  {path.name}: creating new file")
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    log.info(f"  {path.name}: appended {len(rows)} rows")


def append_results_json(path: Path, envelope: dict, target_date: str, force: bool):
    if path.exists():
        existing = json.loads(path.read_text())
    else:
        existing = []
    if not isinstance(existing, list):
        sys.exit(f"ERROR: {path.name} is not a JSON array")
    if any(e.get("date") == target_date for e in existing):
        if not force:
            log.warning(f"  {path.name}: {target_date} already present, skipping (use --force to overwrite)")
            return
        existing = [e for e in existing if e.get("date") != target_date]
    existing.append(envelope)
    existing.sort(key=lambda e: e.get("date", ""))
    path.write_text(json.dumps(existing, indent=2))
    log.info(f"  {path.name}: wrote envelope for {target_date}")


# --- Entry point ---------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (default: yesterday in EST)")
    ap.add_argument("--force", action="store_true", help="overwrite existing rows for date")
    ap.add_argument("--refresh-auth", action="store_true",
                    help="login fresh and save storage_state.json")
    args = ap.parse_args()

    if args.refresh_auth:
        refresh_auth_state()
        return

    if args.date:
        target_date = args.date
    else:
        # Yesterday in EST (UTC-5; ignore DST for simplicity — close enough at 7am)
        est_now = datetime.now(timezone.utc) - timedelta(hours=5)
        target_date = (est_now - timedelta(days=1)).strftime("%Y-%m-%d")
    log.info(f"Target date: {target_date}")

    # Idempotency pre-check (cheap, before launching browser)
    all_present = (
        _date_present_in_csv(PLAYERS_CSV, target_date)
        and _date_present_in_csv(DRAFTS_CSV, target_date)
        and _date_present_in_csv(HV_STATS_CSV, target_date)
    )
    if all_present and not args.force:
        log.info(f"All files already contain {target_date}.  Use --force to re-scrape.  Exiting.")
        return

    payloads = fetch_slate_payloads(target_date)

    log.info("Building position + team lookups ...")
    all_player_ids = _collect_all_player_ids(payloads["stats"], payloads["entries"]["entries"])
    log.info(f"  fetching positions for {len(all_player_ids)} unique playerIds via MLB Stats API ...")
    position_lookup = _fetch_mlb_positions(all_player_ids)
    team_lookup = _build_team_lookup(payloads["daily"], payloads["stats"])
    log.info(f"  built team lookup: {len(team_lookup)} teamIds")

    log.info("Parsing payloads ...")
    games_env = parse_games(payloads["daily"], target_date)
    log.info(f"  games: {games_env['game_count']} ({len(games_env['games'])} finalized)")
    players_rows = parse_players(payloads["stats"], position_lookup, target_date)
    log.info(f"  players: {len(players_rows)} unique")
    drafts_rows = parse_winning_drafts(payloads["entries"]["entries"], position_lookup, team_lookup, target_date)
    log.info(f"  winning drafts: {len(drafts_rows)} rows ({len(payloads['entries']['entries'])} lineups × ~5 slots)")
    hv_stats_rows = parse_hv_stats_blank(payloads["stats"], position_lookup, target_date, games_env)
    log.info(f"  hv stats stubs: {len(hv_stats_rows)} rows")

    log.info("Writing files ...")
    append_csv(PLAYERS_CSV, players_rows, target_date, args.force)
    append_csv(DRAFTS_CSV, drafts_rows, target_date, args.force)
    append_results_json(RESULTS_JSON, games_env, target_date, args.force)
    append_csv(HV_STATS_CSV, hv_stats_rows, target_date, args.force)

    log.info(f"Done.  All four files updated for {target_date}.")


if __name__ == "__main__":
    main()
