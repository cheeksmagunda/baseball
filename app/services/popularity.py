"""
Popularity signal aggregator.

Scrapes external sources to estimate which players the crowd will over-draft.
Used to classify players as FADE (over-hyped), TARGET (under the radar), or NEUTRAL.

All sources are pre-game public signals — no DFS platform ownership data.
DFS platform ownership is only visible during the draft session; using it as
a predictive input would violate the "pre-game signals only" constraint.

Signal sources (weighted):
  - Social trending (45%): Google Trends autocomplete + daily trends
  - Sports news (25%): ESPN, MLB.com headlines
  - Search volume (30%): Google Trends search interest

Classification logic:
  High attention + high/mid performance → FADE (crowd already on it)
  High performance + low media          → TARGET (under the radar)
  High attention + low performance      → FADE (name-recognition trap)
  Trending upward + low media           → TARGET (breakout)

Sharp signal (Moonshot only):
  Separate from mainstream signals. Scrapes niche baseball communities
  (Reddit, prospect blogs, advanced-stats sites) to find players the
  underground is quietly on but mainstream hasn't caught yet.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 8.0
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class PopularityClass(str, Enum):
    FADE = "FADE"
    TARGET = "TARGET"
    NEUTRAL = "NEUTRAL"


@dataclass
class SignalResult:
    source: str
    score: float  # 0-100 (0 = invisible, 100 = everywhere)
    context: str = ""


@dataclass
class PopularityProfile:
    player_name: str
    team: str
    social_score: float = 0.0
    news_score: float = 0.0
    search_score: float = 0.0
    sharp_score: float = 0.0
    composite_score: float = 0.0
    classification: PopularityClass = PopularityClass.NEUTRAL
    reason: str = ""
    signals: list[SignalResult] = field(default_factory=list)


# Signal weights — pre-game public signals only (no DFS platform ownership).
# DFS ownership is only visible during the draft; using it would violate
# the pre-game-signals-only constraint.
SIGNAL_WEIGHTS = {
    "social": 0.45,   # Google Trends autocomplete + daily trends
    "news":   0.25,   # ESPN + MLB.com RSS
    "search": 0.30,   # Google search interest
}


# ---------------------------------------------------------------------------
# Mainstream signal fetchers (used for FADE/TARGET classification)
# ---------------------------------------------------------------------------

async def fetch_social_signal(player_name: str, team: str) -> SignalResult:
    """
    Estimate social media buzz via Google Trends.

    Presence in Google autocomplete or daily trends = mainstream attention.
    Both endpoints are fetched in parallel; each handles its own errors
    so a single 429 doesn't kill the entire signal.
    """
    query = f"{player_name} MLB"
    last_name = player_name.split()[-1].lower()

    async def _autocomplete(client: httpx.AsyncClient) -> bool:
        try:
            r = await client.get(
                "https://trends.google.com/trends/api/autocomplete",
                params={"hl": "en-US", "tz": "300", "q": query},
            )
            r.raise_for_status()
            return last_name in r.text.lower()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            raise RuntimeError(f"Google Trends autocomplete failed for {player_name}: {exc}") from exc

    async def _dailytrends(client: httpx.AsyncClient) -> bool:
        try:
            r = await client.get(
                "https://trends.google.com/trends/api/dailytrends",
                params={"hl": "en-US", "tz": "300", "geo": "US", "ns": "15"},
            )
            r.raise_for_status()
            return last_name in r.text.lower()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            raise RuntimeError(f"Google Trends daily trends failed for {player_name}: {exc}") from exc

    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        in_autocomplete, in_daily = await asyncio.gather(
            _autocomplete(client), _dailytrends(client)
        )
    if in_daily:
        return SignalResult("social", 85.0, f"In Google daily trends: '{player_name}'")
    if in_autocomplete:
        return SignalResult("social", 70.0, f"Trending on Google: '{query}'")
    return SignalResult("social", 0.0, "No social signal detected")


async def fetch_news_signal(player_name: str, team: str) -> SignalResult:
    """
    Check sports news for recent headlines mentioning the player.

    Uses ESPN and MLB.com RSS feeds — free, no auth. Each feed handles
    its own errors so one broken feed doesn't kill the signal.
    """
    last_name = player_name.split()[-1].lower()

    async def _feed(client: httpx.AsyncClient, name: str, url: str) -> str | None:
        try:
            r = await client.get(url)
            r.raise_for_status()
            return name if last_name in r.text.lower() else None
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            raise RuntimeError(f"{name} RSS feed failed for {player_name}: {exc}") from exc

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}) as client:
        results = await asyncio.gather(
            _feed(client, "ESPN", "https://www.espn.com/espn/rss/mlb/news"),
            _feed(client, "MLB", "https://www.mlb.com/feeds/news/rss.xml"),
        )
    sources_found = [s for s in results if s]
    score = min(len(sources_found) * 40.0, 100.0)
    context = f"Found in: {', '.join(sources_found)}" if sources_found else "No news mentions"
    return SignalResult("news", score, context)


async def fetch_search_signal(player_name: str, team: str) -> SignalResult:
    """
    Google search volume proxy via autocomplete.

    High casual search interest = the crowd knows about this player.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        resp = await client.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "firefox", "q": f"{player_name} "},
        )
    resp.raise_for_status()
    suggestions = resp.text.lower()

    hot_terms = ["stats", "today", "home run", "injury", "lineup", "dfs"]
    matches = sum(1 for term in hot_terms if term in suggestions)
    if matches >= 3:
        return SignalResult("search", 80.0, f"High search interest ({matches} context terms)")
    if matches >= 1:
        return SignalResult("search", 45.0, f"Moderate search interest ({matches} context terms)")
    return SignalResult("search", 0.0, "Low search volume")


# ---------------------------------------------------------------------------
# Sharp signal fetcher (underground / niche — used by Moonshot)
# ---------------------------------------------------------------------------

async def fetch_sharp_signal(player_name: str, team: str) -> SignalResult:
    """
    Underground / sharp money signal.

    Scrapes niche baseball communities that mainstream doesn't follow.
    If small, smart accounts are talking about a player but ESPN isn't,
    that's a Moonshot BUY signal.

    Sources:
      - Reddit r/fantasybaseball (hot posts, daily threads)
      - Reddit r/baseball (rising posts)
      - Prospect/analytics blogs (FanGraphs community, Prospects Live)

    Each source handles its own errors so one down site doesn't zero the signal.
    """
    last_name = player_name.split()[-1].lower()

    SOURCES = [
        ("r/fantasybaseball", "https://www.reddit.com/r/fantasybaseball/hot.json", 35.0,
         {"limit": "50", "raw_json": "1"}, {"User-Agent": "BaseballDFS/1.0 (by /u/baseballdfs)"}),
        ("r/baseball", "https://www.reddit.com/r/baseball/hot.json", 25.0,
         {"limit": "50", "raw_json": "1"}, {"User-Agent": "BaseballDFS/1.0 (by /u/baseballdfs)"}),
        ("FanGraphs community", "https://community.fangraphs.com/feed/", 30.0,
         {}, {"User-Agent": _USER_AGENT}),
        ("Prospects Live", "https://www.prospectslive.com/feed", 25.0,
         {}, {"User-Agent": _USER_AGENT}),
    ]

    async def _fetch(client: httpx.AsyncClient, name: str, url: str, pts: float,
                     params: dict, headers: dict) -> tuple[str, float]:
        try:
            r = await client.get(url, params=params or None, headers=headers)
            r.raise_for_status()
            return (name, pts) if last_name in r.text.lower() else (name, 0.0)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.debug("Sharp source %s failed for %s: %s", name, player_name, exc)
            return (name, 0.0)

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        results = await asyncio.gather(
            *[_fetch(client, name, url, pts, params, hdrs) for name, url, pts, params, hdrs in SOURCES]
        )
    score = 0.0
    sources_found = []
    for name, pts in results:
        if pts > 0:
            score += pts
            sources_found.append(name)
    context = f"Underground buzz: {', '.join(sources_found)}" if sources_found else "No underground signal"
    return SignalResult("sharp", min(score, 100.0), context)


# ---------------------------------------------------------------------------
# Aggregation + classification
# ---------------------------------------------------------------------------

def compute_composite_score(signals: list[SignalResult]) -> float:
    """Weighted average of mainstream signal scores (excludes sharp signal)."""
    total = 0.0
    for sig in signals:
        weight = SIGNAL_WEIGHTS.get(sig.source, 0.0)
        total += sig.score * weight
    return round(total, 1)


def classify_player(
    composite_popularity: float,
    player_score: float,
) -> tuple[PopularityClass, str]:
    """
    Classify a player based on popularity vs performance.

    High attention + high/mid performance → FADE (crowd already on it)
    High performance + low attention → TARGET (under the radar)
    High attention + low performance → FADE (name-recognition trap)
    Low attention + mid performance → NEUTRAL
    """
    high_pop = composite_popularity >= 50.0
    mid_pop = 25.0 <= composite_popularity < 50.0
    high_perf = player_score >= 60.0
    mid_perf = 25.0 <= player_score < 60.0

    if high_pop and high_perf:
        return PopularityClass.FADE, "High attention + strong performance — crowd is already on this player"
    if high_pop and mid_perf:
        return PopularityClass.FADE, "High attention + decent performance — over-drafted for name recognition"
    if high_pop and not mid_perf:
        return PopularityClass.FADE, "High attention + weak performance — name-recognition trap"
    if mid_pop and high_perf:
        return PopularityClass.FADE, "Moderate buzz + good stats — crowd is catching on"
    if not high_pop and not mid_pop and high_perf:
        return PopularityClass.TARGET, "Strong performance + under the radar — the crowd hasn't caught on"
    if not high_pop and not mid_pop and mid_perf:
        return PopularityClass.TARGET, "Decent performance + low attention — value pick"

    return PopularityClass.NEUTRAL, "No strong signal either way"


async def get_popularity_profile(
    player_name: str,
    team: str,
    player_score: float = 50.0,
    include_sharp: bool = False,
) -> PopularityProfile:
    """
    Full popularity assessment for a single player.

    Fetches all signal sources in parallel, computes composite,
    and classifies as FADE / TARGET / NEUTRAL.

    Args:
        include_sharp: If True, also fetches the underground sharp signal
                       (used by Moonshot optimizer).
    """
    # Build list of fetchers — pre-game public signals only.
    # DFS platform ownership (RotoGrinders, NumberFire) intentionally excluded:
    # it is only available during the draft session, not before.
    fetchers = [
        fetch_social_signal(player_name, team),
        fetch_news_signal(player_name, team),
        fetch_search_signal(player_name, team),
    ]
    if include_sharp:
        fetchers.append(fetch_sharp_signal(player_name, team))

    results = await asyncio.gather(*fetchers)

    social, news, search = results[0], results[1], results[2]
    sharp = results[3] if include_sharp else SignalResult("sharp", 0.0, "Not fetched")

    signals = [social, news, search, sharp]
    composite = compute_composite_score(signals)  # sharp excluded from composite
    classification, reason = classify_player(composite, player_score)

    return PopularityProfile(
        player_name=player_name,
        team=team,
        social_score=social.score,
        news_score=news.score,
        search_score=search.score,
        sharp_score=sharp.score,
        composite_score=composite,
        classification=classification,
        reason=reason,
        signals=signals,
    )


async def get_slate_popularity(
    players: list[dict],
    include_sharp: bool = False,
) -> list[PopularityProfile]:
    """
    Assess popularity for an entire slate of players.

    Args:
        players: list of {"player_name": str, "team": str, "player_score": float}
        include_sharp: If True, also fetches sharp signals for Moonshot.

    Returns sorted by composite_score descending (most popular first).
    """
    profiles = await asyncio.gather(*[
        get_popularity_profile(
            p["player_name"],
            p["team"],
            p.get("player_score", 50.0),
            include_sharp=include_sharp,
        )
        for p in players
    ])

    return sorted(profiles, key=lambda p: p.composite_score, reverse=True)
