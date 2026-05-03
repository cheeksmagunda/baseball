"""
RotoWire daily-lineups scraper — expected MLB starting lineups.

Source: https://www.rotowire.com/baseball/daily-lineups.php

Why scrape:
    The MLB Stats API does not expose pre-card "expected" lineups.  At T-65
    the official boxscore lineup is rarely posted yet (MLB usually serves
    it 30-60 minutes before first pitch).  RotoWire aggregates beat-reporter
    info and publishes expected lineups up to 4 hours before first pitch,
    covering ~90% of games at T-65.  This is the de-facto source for the
    open-source MLB DFS community (chanzer0/MLB-DFS-Tools, evolve-dfs, etc.)
    — there is no free first-party JSON API.

No-fallbacks compliance:
    A network error, non-200 response, or zero parseable games raises
    RuntimeError.  The caller in app/services/data_collection.py re-raises,
    crashing the T-65 pipeline so /optimize returns 503.  RotoWire is the
    only source of batting order data at T-65 and is treated as required
    infrastructure, identical to the Odds API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import httpx
import tenacity
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_URL = "https://www.rotowire.com/baseball/daily-lineups.php"
# A real-looking User-Agent.  RotoWire returns a thin 403/empty body for
# bare "python-requests/x.y" headers; a desktop UA gets the same HTML a
# browser would.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class LineupStatus(str, Enum):
    """RotoWire's per-team lineup status (from the lineup__status li)."""

    CONFIRMED = "confirmed"   # Green dot — official lineup card posted
    EXPECTED = "expected"     # Yellow — beat-reporter projection
    UNKNOWN = "unknown"       # Status element absent or unrecognised


@dataclass(frozen=True)
class LineupPlayer:
    """One player slot in a team's batting order."""

    name: str            # Text shown in the <a> (often abbreviated, e.g. "J. Aranda")
    full_name: str       # The <a title="..."> attribute — always the full name
    batting_order: int   # 1-9, derived from HTML order
    position: str        # 1B, SS, OF, DH, etc.
    bats: str | None     # "R" / "L" / "S" (switch); None if unknown


@dataclass(frozen=True)
class TeamLineup:
    team: str            # 3-letter MLB abbreviation, as shown in lineup__abbr
    is_home: bool
    starting_pitcher: str | None
    pitcher_throws: str | None     # "R" / "L"
    status: LineupStatus
    players: tuple[LineupPlayer, ...]


@dataclass(frozen=True)
class GameLineup:
    visitor: TeamLineup
    home: TeamLineup


@tenacity.retry(
    # 5 attempts with full jitter — RotoWire occasionally returns a 502/503
    # for ~1-3s during edge cache rotation; bare-once was flaky at T-65.
    # Total worst-case wait: ~16s before the final failure raises.
    stop=tenacity.stop_after_attempt(5),
    wait=tenacity.wait_random_exponential(multiplier=1, max=8),
    retry=tenacity.retry_if_exception_type(
        (httpx.HTTPError, httpx.TimeoutException, RuntimeError)
    ),
    reraise=True,
)
async def fetch_expected_lineups() -> list[GameLineup]:
    """Fetch RotoWire's daily-lineups page and parse every game on it.

    Returns one GameLineup per game in HTML order.  Raises RuntimeError on
    network failure, non-200 response, or empty parse after all retries
    exhaust.  Parse failures for individual games are skipped silently —
    this is intentional: a single malformed card on a 12-game slate must
    not block enrichment of the other 11.

    Use parse_lineups_html() directly with a pre-fetched HTML string for
    tests / offline reproduction.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(_URL, headers={"User-Agent": _USER_AGENT})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"RotoWire fetch failed: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"RotoWire returned HTTP {resp.status_code} for {_URL} — "
            "expected lineups cannot be enriched."
        )
    games = parse_lineups_html(resp.text)
    if not games:
        # Empty parse is treated like a transient failure: tenacity will retry
        # before raising terminally.  Helps when RotoWire serves a partial /
        # placeholder HTML during their CDN warmup window.
        raise RuntimeError(
            "RotoWire returned 200 but parsed zero games — likely a partial "
            "page from CDN warmup; will retry"
        )
    return games


def parse_lineups_html(html: str) -> list[GameLineup]:
    """Parse RotoWire's daily-lineups HTML into a list of GameLineup objects.

    Pure function — call directly from tests with fixture HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    games: list[GameLineup] = []

    for game_div in soup.select("div.lineup.is-mlb"):
        # Skip ad / tools cards — they have the same outer class but no
        # lineup__teams structure.
        abbrs = [t.get_text(strip=True) for t in game_div.select(".lineup__abbr")]
        if len(abbrs) != 2:
            continue
        visitor_abbr, home_abbr = abbrs

        visit_ul = game_div.select_one("ul.lineup__list.is-visit")
        home_ul = game_div.select_one("ul.lineup__list.is-home")
        if visit_ul is None or home_ul is None:
            continue

        visitor = _parse_team_block(visit_ul, visitor_abbr, is_home=False)
        home = _parse_team_block(home_ul, home_abbr, is_home=True)
        if visitor is None or home is None:
            # No batters parseable — TBA card or HTML drift.  Skip the game.
            continue
        games.append(GameLineup(visitor=visitor, home=home))

    return games


def _parse_team_block(ul, team_abbr: str, *, is_home: bool) -> TeamLineup | None:
    """Parse a single <ul class='lineup__list is-visit/is-home'> block."""
    # Starting pitcher — the lineup__player-highlight item, if present.
    pitcher_name = None
    pitcher_throws = None
    sp = ul.select_one("li.lineup__player-highlight .lineup__player-highlight-name")
    if sp is not None:
        link = sp.select_one("a")
        if link:
            pitcher_name = link.get_text(strip=True)
        throws = sp.select_one(".lineup__throws")
        if throws:
            pitcher_throws = (throws.get_text(strip=True) or None)

    # Per-team status (from the lineup__status li).
    status = LineupStatus.UNKNOWN
    status_li = ul.select_one("li.lineup__status")
    if status_li is not None:
        classes = " ".join(status_li.get("class", []))
        text = status_li.get_text(strip=True).lower()
        if "is-confirmed" in classes or "confirm" in text:
            status = LineupStatus.CONFIRMED
        elif (
            "is-expected" in classes
            or "is-projected" in classes
            or "expect" in text
            or "project" in text
        ):
            status = LineupStatus.EXPECTED

    # Batting order — sequential lineup__player <li>'s in HTML order, 1-9.
    players: list[LineupPlayer] = []
    for slot, li in enumerate(ul.select("li.lineup__player"), start=1):
        if slot > 9:
            break
        pos_el = li.select_one(".lineup__pos")
        link = li.select_one("a")
        bats_el = li.select_one(".lineup__bats")
        if pos_el is None or link is None:
            continue
        full_name = (link.get("title") or link.get_text(strip=True)).strip()
        if not full_name:
            continue
        name = link.get_text(strip=True)
        bats = bats_el.get_text(strip=True) if bats_el else None
        players.append(LineupPlayer(
            name=name,
            full_name=full_name,
            batting_order=slot,
            position=pos_el.get_text(strip=True),
            bats=bats or None,
        ))

    if not players:
        return None

    return TeamLineup(
        team=team_abbr,
        is_home=is_home,
        starting_pitcher=pitcher_name,
        pitcher_throws=pitcher_throws,
        status=status,
        players=tuple(players),
    )
