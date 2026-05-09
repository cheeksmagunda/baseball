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

DATE RANGE LIMITATION — IMPORTANT:
    This scraper navigates the realsports.io UI date strip, which only shows
    approximately the last 14 days of completed slates.  Any date older than
    ~2 weeks is NOT accessible via this browser-based path.

    For backfilling arbitrary past dates (e.g. the full historical corpus
    starting 2026-03-25) use scripts/backfill_card_boost_and_drafts.py, which
    calls the JSON API directly (web.realapp.com/home/mlb/day/next?day=YYYY-MM-DD)
    and works for any date regardless of the UI date strip.
"""
import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
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
        # The platform uses TWO endpoint shapes for the daily slate:
        #   /home/mlb/day/next?day=YYYY-MM-DD  → top-level 'content' with games
        #     (used when navigating to dates other than today/yesterday)
        #   /home/mlb/next                      → top-level 'latestDay' (date) +
        #                                         'latestDayContent' (games etc.)
        #     (used when the date strip's "today" or "yesterday" slot is clicked)
        # Normalize both into the {'content': {...games...}} shape so downstream
        # code (parse_games, _build_team_lookup) is identical.
        url = resp.url
        if "realapp.com" not in url:
            return
        if "/home/mlb/" not in url or "next" not in url:
            return
        try:
            data = json.loads(resp.text())
        except Exception:
            return
        normalized = None
        if target_date in url:
            # /home/mlb/day/next?day=YYYY-MM-DD shape
            normalized = data
        elif data.get("latestDay") == target_date and isinstance(data.get("latestDayContent"), dict):
            # /home/mlb/next shape — wrap latestDayContent as 'content'
            normalized = {"content": data["latestDayContent"]}
        if normalized is None:
            return
        captured["daily"] = normalized
        log.info(f"  captured daily payload ({len(resp.text())}b) [{_humanize_url(url)}]")

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
        # Scroll into view so bounding-box coords are within the visible viewport
        vr.scroll_into_view_if_needed(timeout=10000)
        time.sleep(0.5)
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
            timeout=30000,
        ) as ev:
            page.mouse.click(cx, cy)
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
            timeout=30000,
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


def _round_dp(value: float, dp: int) -> float:
    """
    Round to `dp` decimal places using banker's rounding (half-to-even),
    via Decimal to avoid IEEE-754 float-repr quirks. The platform displays
    exact-half values consistently with this rule (e.g. raw -0.45 displays
    as '-0.4' because 4 is even — Python's round(-0.45, 1) returns -0.5
    because -0.45 isn't exactly representable in float64).
    """
    if value is None:
        return value
    quant = Decimal("1") if dp <= 0 else Decimal("1e-{}".format(dp))
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_EVEN))


def _team_key_normalize(key: str) -> str:
    """Map platform's legacy team keys to the CLAUDE.md V9.1 standard codes."""
    if key == "OAK":
        return "ATH"
    return key


def _away_home_result(g: dict) -> str:
    """Format game_result as '{away} {away_score} {home} {home_score}',
    matching the manual ingest convention (e.g. 'KC 0 NYY 7' — away team
    listed first regardless of which team the player is on)."""
    if not g:
        return ""
    return f"{g['away']} {g['away_score']} {g['home']} {g['home_score']}"


def _fetch_mlb_player_info(player_ids: list[int]) -> dict[int, dict]:
    """
    Batch-query the MLB Stats API for primary positions and full names.
    Real Sports player IDs ARE MLB player IDs.

    Returns {mlb_id: {'position': 'P'|'OF', 'fullName': str}}.
    Pitchers (primaryPosition.type == 'Pitcher') get 'P'; everyone else
    (including Two-Way Players like Ohtani — they hit more often in DFS) gets 'OF',
    per the CLAUDE.md V9.1 generic-OF convention.

    Uses /people?personIds=... batch endpoint. Splits into chunks of 50 to
    keep URLs short.
    """
    import requests
    lookup: dict[int, dict] = {}
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
                full_name = p.get("fullName") or ""
                if pid is not None:
                    lookup[pid] = {
                        "position": "P" if pp_type == "Pitcher" else "OF",
                        "fullName": full_name,
                    }
        except Exception as e:
            log.warning(f"  MLB API lookup failed for chunk {i}-{i+CHUNK}: {e}")
    n_p = sum(1 for v in lookup.values() if v["position"] == "P")
    n_of = sum(1 for v in lookup.values() if v["position"] == "OF")
    log.info(f"  fetched MLB player info for {len(lookup)}/{len(unique_ids)} players "
             f"(P={n_p}, OF={n_of})")
    return lookup


def _build_team_lookup(daily_payload: dict, stats_payload: dict) -> dict[int, str]:
    """
    Build teamId → 3-letter key (e.g. 113 → 'CIN') from the games array (which
    has full home/awayTeam objects) plus the stats payload's player.team objects
    (which cover any teams not in today's games). Keys are normalized to the
    CLAUDE.md V9.1 standard (e.g. platform 'OAK' → 'ATH').
    """
    lookup: dict[int, str] = {}
    for g in daily_payload.get("content", {}).get("games", []):
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side) or {}
            if t.get("id") and t.get("key"):
                lookup[t["id"]] = _team_key_normalize(t["key"])
    for sec in stats_payload.get("draftStats", []):
        for p in sec.get("players", []):
            t = p.get("team") or {}
            if t.get("id") and t.get("key"):
                lookup[t["id"]] = _team_key_normalize(t["key"])
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


def _position_for(player_id: int, player_info: dict[int, dict]) -> str:
    """Default to 'OF' if MLB API didn't have data (e.g. very recent call-up)."""
    info = player_info.get(player_id) or {}
    return info.get("position") or "OF"


def _name_for(player_id: int, player_info: dict[int, dict],
              fallback_display_name: str) -> str:
    """Prefer MLB Stats API fullName ('Aaron Judge'); fall back to platform
    displayName ('A. Judge') if MLB API didn't return data for this id."""
    info = player_info.get(player_id) or {}
    return _name_normalize(info.get("fullName") or fallback_display_name)


def _safe_round(v, dp: int):
    if v is None:
        return ""
    try:
        return round(float(v), dp)
    except (TypeError, ValueError):
        return ""


def parse_players(stats_payload: dict, player_info: dict[int, dict],
                  target_date: str) -> list[dict]:
    """
    historical_players.csv columns:
      date, player_name, team, position, real_score, total_value,
      is_highest_value, is_most_popular, is_most_drafted_3x,
      draft_count, avg_draft_slot, most_common_slot, avg_draft_mult,
      avg_draft_tv, highest_draft_tv, injury_status

    Dedup by (player_name, team), merging flags from HV/MP/3X sections.
    Rich stats (draft_count, avg_draft_slot, etc.) are taken from the section
    with the highest count (MP = total drafters, most representative).
    real_score is rounded to 1 dp to match the platform display + manual ingest;
    total_value is computed from real_score and the per-row card_boost (boost
    itself is not persisted — only the post-boost value).
    """
    by_key: dict[tuple[str, str], dict] = {}
    best_count: dict[tuple[str, str], int] = {}  # tracks which section had the highest count

    for sec in stats_payload.get("draftStats", []):
        # Use .get() — platform added a 'My draft' section without sectionName
        section_name = sec.get("sectionName")
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
            name = _name_for(pl["id"], player_info, pl["displayName"])
            team = _team_key_normalize(tm["key"])
            key = (name, team)

            real_score = _round_dp(float(p["value"]), 1)
            boost = float(p["multiplierBonus"])
            total_value = _round_dp(real_score * (BASE_SLOT_MULT + boost), 2)
            position = _position_for(pl["id"], player_info)

            count = p.get("count") or 0

            if key not in by_key:
                by_key[key] = {
                    "date": target_date,
                    "player_name": name,
                    "team": team,
                    "position": position,
                    "real_score": real_score,
                    "total_value": total_value,
                    "_mlb_id": pl["id"],          # internal — not written to CSV
                    "_card_boost": boost,         # internal — feeds label_event(card_boost)
                    "is_highest_value": 0,
                    "is_most_popular": 0,
                    "is_most_drafted_3x": 0,
                    "draft_count": count,
                    "avg_draft_slot": _safe_round(p.get("avgPosition"), 3),
                    "most_common_slot": p.get("mostCommonPosition", ""),
                    "avg_draft_mult": _safe_round(p.get("avgMultiplier"), 4),
                    "avg_draft_tv": _safe_round(p.get("avgScore"), 4),
                    "highest_draft_tv": _safe_round(p.get("highestScore"), 4),
                    "injury_status": pl.get("injuryStatus", ""),
                }
                best_count[key] = count
            else:
                row = by_key[key]
                if abs(row["real_score"] - real_score) > 0.05:
                    log.warning(f"    {name} ({team}): real_score drift "
                                f"{row['real_score']} vs {real_score}")
                # draft_count: keep the max (MP section carries the total count)
                if count > (row.get("draft_count") or 0):
                    row["draft_count"] = count
                # avg_draft_* fields: take from the section with highest count
                if count > best_count.get(key, 0):
                    row["avg_draft_slot"] = _safe_round(p.get("avgPosition"), 3)
                    row["most_common_slot"] = p.get("mostCommonPosition", "")
                    row["avg_draft_mult"] = _safe_round(p.get("avgMultiplier"), 4)
                    row["avg_draft_tv"] = _safe_round(p.get("avgScore"), 4)
                    row["highest_draft_tv"] = _safe_round(p.get("highestScore"), 4)
                    best_count[key] = count
                # injury_status: keep first non-empty value
                if not row.get("injury_status") and pl.get("injuryStatus"):
                    row["injury_status"] = pl["injuryStatus"]

            by_key[key][flag_field] = 1

    return list(by_key.values())


def parse_winning_drafts(entries: list[dict], player_info: dict[int, dict],
                         team_lookup: dict[int, str], target_date: str) -> list[dict]:
    """
    historical_winning_drafts.csv columns:
      date, winner_rank, slot_index, player_name, team, position,
      real_score, slot_mult, card_boost, total_mult
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
            team_key = team_lookup.get(tid, "")  # already normalized in _build_team_lookup
            if not team_key and tid is not None:
                missing_teams.add(tid)
            rows.append({
                "date": target_date,
                "winner_rank": rank,
                "slot_index": slot_index,
                "player_name": _name_for(pid, player_info, player["displayName"]),
                "team": team_key,
                "position": _position_for(pid, player_info),
                "real_score": _round_dp(float(player["value"]), 1),
                "slot_mult": slot_mult,
                "card_boost": player.get("multiplierBonus", ""),
                "total_mult": player.get("multiplier", ""),
                "_mlb_id": pid,                # internal — not written to CSV
            })
    if missing_teams:
        log.warning(f"  no team_key for teamIds: {sorted(missing_teams)}")
    return rows


def parse_games(daily_payload: dict, stats_payload: dict, target_date: str) -> dict:
    """
    historical_slate_results.json envelope:
      {date, game_count, games[], num_brawlers, season_stage, source, saved_at, notes}
    """
    content = daily_payload["content"]
    games_in = content.get("games", [])
    games_out = []
    for g in games_in:
        ht, at = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        hk = _team_key_normalize(ht.get("key", ""))
        ak = _team_key_normalize(at.get("key", ""))
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

    num_brawlers = stats_payload.get("contest", {}).get("numBrawlers") or 0

    return {
        "date": target_date,
        "game_count": len(games_in),
        "games": games_out,
        "num_brawlers": num_brawlers,
        "season_stage": "regular-season",
        "source": "realsports_scraper",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "notes": f"Scraped via realsports.io on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
    }


def parse_hv_stats_blank(stats_payload: dict, player_info: dict[int, dict],
                         target_date: str, games_envelope: dict) -> list[dict]:
    """
    hv_player_game_stats.csv columns:
      date, player_name, team_actual, position, real_score, game_result,
      ab, r, h, hr, rbi, bb, so, ip, er, k_pitching, decision, notes

    Populates identifying fields + game_result (player-team-first format,
    matching manual ingest convention) and leaves per-player batting/pitching
    stats blank — those are backfilled later from MLB Stats API by
    scripts/backfill_slate_results_and_hv_stats.py.
    """
    rows = []
    # Build team -> full game object so we can format player-centric results
    team_to_game: dict[str, dict] = {}
    for g in games_envelope["games"]:
        team_to_game[g["home"]] = g
        team_to_game[g["away"]] = g

    for sec in stats_payload.get("draftStats", []):
        if sec.get("sectionName") != "highestBoostedValuePlayers":
            continue
        for p in sec["players"]:
            pl = p["player"]
            tm = p["team"]
            team = _team_key_normalize(tm["key"])
            name = _name_for(pl["id"], player_info, pl["displayName"])
            rows.append({
                "date": target_date,
                "player_name": name,
                "team_actual": team,
                "position": _position_for(pl["id"], player_info),
                "real_score": _round_dp(float(p["value"]), 1),
                "game_result": _away_home_result(team_to_game.get(team)),
                "ab": "", "r": "", "h": "", "hr": "", "rbi": "", "bb": "", "so": "",
                "ip": "", "er": "", "k_pitching": "", "decision": "",
                "notes": "auto-scraped (box score backfill pending)",
                "_mlb_id": pl["id"],           # internal — not written to CSV
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
    """Append rows for target_date to path, preserving all existing columns.

    When the file already has backfilled columns (Statcast, at-slate stats,
    card_boost, etc.) that the scraper doesn't produce, a naive rewrite using
    only the new rows' fieldnames would silently strip those extra columns for
    every kept row.  This function prevents that by:

      1. Reading the existing file's fieldnames first.
      2. Merging them with the new rows' fieldnames (union, existing order
         preserved, new fields appended at the end).
      3. Writing kept rows and new rows with the merged fieldname set.
         Rows missing a field get an empty string for that column.
    """
    if not rows:
        log.warning(f"  no rows to write to {path.name}")
        return

    new_fieldnames = list(rows[0].keys())

    if path.exists():
        # Read existing fieldnames so we can preserve unknown/backfilled columns.
        with open(path) as f:
            reader = csv.DictReader(f)
            existing_fieldnames = list(reader.fieldnames or [])

        # Union: existing columns first (preserves order + extra backfilled cols),
        # then any genuinely new columns from this scrape run appended at the end.
        merged_fieldnames = existing_fieldnames + [
            c for c in new_fieldnames if c not in existing_fieldnames
        ]

        if _date_present_in_csv(path, target_date):
            if not force:
                log.warning(f"  {path.name}: {target_date} already present, skipping (use --force to overwrite)")
                return
            log.info(f"  {path.name}: removing existing rows for {target_date}")
            with open(path) as f:
                kept = [r for r in csv.DictReader(f) if r.get("date") != target_date]
            # Rewrite file with merged fieldnames so kept rows keep all their columns.
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=merged_fieldnames, extrasaction="ignore")
                w.writeheader()
                for row in kept:
                    w.writerow({col: row.get(col, "") for col in merged_fieldnames})

        # Append new rows (fill any extra backfilled columns with empty string).
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=merged_fieldnames, extrasaction="ignore")
            for row in rows:
                w.writerow({col: row.get(col, "") for col in merged_fieldnames})
    else:
        log.info(f"  {path.name}: creating new file")
        merged_fieldnames = new_fieldnames
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=merged_fieldnames)
            w.writeheader()
            w.writerows(rows)

    log.info(f"  {path.name}: appended {len(rows)} rows")


def _write_slate_to_db(
    *,
    target_date: str,
    games_env: dict,
    players_rows: list[dict],
    drafts_rows: list[dict],
    hv_stats_rows: list[dict],
    force: bool,
) -> None:
    """Write the day's scrape into data/historical.db.  CSVs/JSON are
    regenerated by export_historical_csvs.export_all() afterwards.

    --force semantics: if the slate_date already has rows, delete them first.
    Without --force, fail loudly so we don't double-write.

    The scraper's parsed-row dicts carry an internal `_mlb_id` field per row
    (added in this Step 2 patch) so we don't need a name+team→mlb_id lookup
    here; the dedup work happens at parse_players() time.
    """
    sys.path.insert(0, str(ROOT))
    from app.core import historical_db

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        existing = conn.execute(
            "SELECT 1 FROM slate WHERE slate_date = ?", (target_date,)
        ).fetchone()
        if existing and not force:
            log.info(
                f"  data/historical.db already has slate {target_date}; "
                "skipping (use --force)"
            )
            return
        if existing:
            log.info(f"  data/historical.db: removing existing rows for {target_date}")
            for tbl in (
                "label_event",
                "player_slate",
                "slate_game",
                "player_game_log",
            ):
                conn.execute(
                    f"DELETE FROM {tbl} WHERE slate_date = ?", (target_date,)
                )
            conn.execute("DELETE FROM slate WHERE slate_date = ?", (target_date,))

        observed_at = datetime.now(timezone.utc).isoformat()

        # --- slate envelope ---
        historical_db.upsert_slate(conn, {
            "slate_date": target_date,
            "game_count": games_env.get("game_count") or 0,
            "num_brawlers": games_env.get("num_brawlers"),
            "season_stage": games_env.get("season_stage") or "regular-season",
            "source": games_env.get("source") or "realsports_scraper",
            "saved_at": games_env.get("saved_at") or observed_at,
            "notes": games_env.get("notes") or "",
        })

        # --- slate_game ---
        # The daily scrape's parse_games produces lean game dicts (home/away/
        # scores).  game_pk is added by scripts/backfill_slate_env_conditions.py
        # later.  We skip rows without game_pk; the env backfill will create
        # them when it knows the pk.
        seen_pks: dict[int, int] = {}
        for g in games_env.get("games", []):
            pk = g.get("game_pk")
            if pk is None:
                continue
            game_number = seen_pks.get(int(pk), 1)
            seen_pks[int(pk)] = game_number + 1
            historical_db.upsert_slate_game(conn, {
                "slate_date": target_date,
                "game_pk": int(pk),
                "game_number": game_number,
                "home_team": g.get("home", ""),
                "away_team": g.get("away", ""),
                "home_score": g.get("home_score"),
                "away_score": g.get("away_score"),
                "winner": g.get("winner"),
                "loser": g.get("loser"),
                "winner_score": g.get("winner_score"),
                "loser_score": g.get("loser_score"),
            })

        # --- player_slate + label_event from players_rows ---
        for r in players_rows:
            mlb_id = int(r["_mlb_id"])
            historical_db.upsert_player_slate(conn, {
                "slate_date": target_date,
                "mlb_id": mlb_id,
                "player_name": r["player_name"],
                "team": r["team"],
                "position": r["position"] or "OF",
            })

            # numeric scalar labels
            for key, label in [
                ("real_score", "real_score"),
                ("total_value", "total_value"),
                ("_card_boost", "card_boost"),
                ("draft_count", "draft_count"),
                ("avg_draft_slot", "avg_draft_slot"),
                ("avg_draft_mult", "avg_draft_mult"),
                ("avg_draft_tv", "avg_draft_tv"),
                ("highest_draft_tv", "highest_draft_tv"),
            ]:
                v = r.get(key)
                if v in (None, ""):
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                historical_db.upsert_label_event(
                    conn,
                    slate_date=target_date, mlb_id=mlb_id, label_type=label,
                    label_value=fv, label_text=None,
                    source=historical_db.SOURCE_REALSPORTS_STATS,
                    observed_at=observed_at,
                )

            # boolean flags
            for key, label in [
                ("is_highest_value", "highest_value"),
                ("is_most_popular", "most_popular"),
                ("is_most_drafted_3x", "most_drafted_3x"),
            ]:
                if r.get(key) == 1:
                    historical_db.upsert_label_event(
                        conn,
                        slate_date=target_date, mlb_id=mlb_id, label_type=label,
                        label_value=1.0, label_text=None,
                        source=historical_db.SOURCE_REALSPORTS_STATS,
                        observed_at=observed_at,
                    )

            mcs = r.get("most_common_slot")
            if mcs:
                historical_db.upsert_label_event(
                    conn,
                    slate_date=target_date, mlb_id=mlb_id, label_type="most_common_slot",
                    label_value=None, label_text=str(mcs),
                    source=historical_db.SOURCE_REALSPORTS_STATS,
                    observed_at=observed_at,
                )
            inj = r.get("injury_status")
            if inj:
                historical_db.upsert_label_event(
                    conn,
                    slate_date=target_date, mlb_id=mlb_id, label_type="injury_status",
                    label_value=None, label_text=inj,
                    source=historical_db.SOURCE_REALSPORTS_STATS,
                    observed_at=observed_at,
                )

        # --- winning_lineup_slot from drafts_rows ---
        for csv_idx, r in enumerate(drafts_rows):
            mlb_id_raw = r.get("_mlb_id")
            if mlb_id_raw is None:
                continue
            mlb_id = int(mlb_id_raw)
            label_text = json.dumps({
                "rank": int(r["winner_rank"]),
                "slot": int(r["slot_index"]),
                "slot_mult": float(r["slot_mult"]),
                "card_boost": str(r.get("card_boost", "") or ""),
                "total_mult": str(r.get("total_mult", "") or ""),
                "name": r["player_name"],
                "team": r["team"],
                "position": r["position"],
            }, sort_keys=True)
            historical_db.upsert_label_event(
                conn,
                slate_date=target_date, mlb_id=mlb_id,
                label_type="winning_lineup_slot",
                label_value=float(r["real_score"]),
                label_text=label_text,
                source=f"row={csv_idx}|rank={r['winner_rank']}|slot={r['slot_index']}",
                observed_at=observed_at,
            )

        # --- box_score from hv_stats_rows ---
        for r in hv_stats_rows:
            mlb_id = int(r["_mlb_id"])
            payload = {
                k: r.get(k, "") for k in (
                    "game_result", "ab", "r", "h", "hr", "rbi", "bb", "so",
                    "ip", "er", "k_pitching", "decision", "notes",
                )
            }
            historical_db.upsert_label_event(
                conn,
                slate_date=target_date, mlb_id=mlb_id,
                label_type="box_score",
                label_value=float(r["real_score"]) if r.get("real_score") not in (None, "") else None,
                label_text=json.dumps(payload),
                source=historical_db.SOURCE_MLB_BOXSCORE,
                observed_at=observed_at,
            )

        conn.commit()
    finally:
        conn.close()


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

    log.info("Building player + team lookups ...")
    all_player_ids = _collect_all_player_ids(payloads["stats"], payloads["entries"]["entries"])
    log.info(f"  fetching player info for {len(all_player_ids)} unique playerIds via MLB Stats API ...")
    player_info = _fetch_mlb_player_info(all_player_ids)
    team_lookup = _build_team_lookup(payloads["daily"], payloads["stats"])
    log.info(f"  built team lookup: {len(team_lookup)} teamIds")

    # DEBUG: dump raw stats payload structure for inspection
    import os as _os
    if _os.environ.get("BO_SCRAPER_DEBUG"):
        Path("/tmp/scraper_stats_payload.json").write_text(json.dumps(payloads["stats"], indent=2))
        log.info("  DEBUG: dumped stats payload to /tmp/scraper_stats_payload.json")

    log.info("Parsing payloads ...")
    games_env = parse_games(payloads["daily"], payloads["stats"], target_date)
    log.info(f"  games: {games_env['game_count']} ({len(games_env['games'])} finalized)")
    players_rows = parse_players(payloads["stats"], player_info, target_date)
    log.info(f"  players: {len(players_rows)} unique")
    drafts_rows = parse_winning_drafts(payloads["entries"]["entries"], player_info, team_lookup, target_date)
    log.info(f"  winning drafts: {len(drafts_rows)} rows ({len(payloads['entries']['entries'])} lineups × ~5 slots)")
    hv_stats_rows = parse_hv_stats_blank(payloads["stats"], player_info, target_date, games_env)
    log.info(f"  hv stats stubs: {len(hv_stats_rows)} rows")

    log.info("Writing to data/historical.db (canonical store) ...")
    _write_slate_to_db(
        target_date=target_date,
        games_env=games_env,
        players_rows=players_rows,
        drafts_rows=drafts_rows,
        hv_stats_rows=hv_stats_rows,
        force=args.force,
    )

    log.info("Refreshing derived CSVs / JSON exports ...")
    sys.path.insert(0, str(ROOT))
    from scripts.export_historical_csvs import export_all
    results = export_all()
    for name, n in results.items():
        log.info(f"  {name}: {n} rows/envelopes")

    log.info(f"Done.  data/historical.db + 5 derived files updated for {target_date}.")


if __name__ == "__main__":
    main()
