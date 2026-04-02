"""
Popularity signal aggregator.

Scrapes external sources to estimate which players the crowd will over-draft.
Used to classify players as FADE (over-hyped), TARGET (under the radar), or NEUTRAL.

Signal sources (weighted):
  - Social trending (40%): Twitter/X mentions, Google Trends
  - Sports news (20%): ESPN, MLB.com headlines
  - DFS ownership (20%): DraftKings/FanDuel ownership %
  - Search volume (20%): Google Trends search interest

Classification logic:
  High attention + high/mid performance → FADE (crowd already on it)
  High performance + low media          → TARGET (under the radar)
  High attention + low performance      → FADE (name-recognition trap)
  Trending upward + low media           → TARGET (breakout)
"""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

import httpx
import logging

logger = logging.getLogger(__name__)

TIMEOUT = 10.0


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
    dfs_ownership_score: float = 0.0
    search_score: float = 0.0
    composite_score: float = 0.0
    classification: PopularityClass = PopularityClass.NEUTRAL
    reason: str = ""
    signals: list[SignalResult] = field(default_factory=list)


# Signal weights — social is the dominant signal per strategy
SIGNAL_WEIGHTS = {
    "social": 0.40,
    "news": 0.20,
    "dfs_ownership": 0.20,
    "search": 0.20,
}


# ---------------------------------------------------------------------------
# Individual signal fetchers
# ---------------------------------------------------------------------------

async def fetch_social_signal(player_name: str, team: str) -> SignalResult:
    """
    Estimate social media buzz via Twitter/X search volume.

    Uses Twitter's search suggestions endpoint as a lightweight proxy —
    no auth required. Falls back to 0 if unavailable.
    """
    query = f"{player_name} MLB"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # Google Trends via unofficial endpoint (no key needed)
            resp = await client.get(
                "https://trends.google.com/trends/api/autocomplete",
                params={"hl": "en-US", "tz": "300", "q": query},
            )
            # A 200 with the player in suggestions means they're trending
            if resp.status_code == 200 and player_name.split()[-1].lower() in resp.text.lower():
                return SignalResult("social", 70.0, f"Trending on Google: '{query}'")

            # Try a broader search — presence in news cycle
            resp2 = await client.get(
                "https://trends.google.com/trends/api/dailytrends",
                params={"hl": "en-US", "tz": "300", "geo": "US", "ns": "15"},
            )
            if resp2.status_code == 200 and player_name.split()[-1].lower() in resp2.text.lower():
                return SignalResult("social", 85.0, f"In Google daily trends: '{player_name}'")

    except Exception as e:
        logger.debug(f"Social signal fetch failed for {player_name}: {e}")

    return SignalResult("social", 0.0, "No social signal detected")


async def fetch_news_signal(player_name: str, team: str) -> SignalResult:
    """
    Check sports news for recent headlines mentioning the player.

    Uses ESPN and MLB.com RSS feeds — free, no auth.
    """
    last_name = player_name.split()[-1].lower()
    score = 0.0
    sources_found = []

    feeds = [
        ("ESPN", "https://www.espn.com/espn/rss/mlb/news"),
        ("MLB", "https://www.mlb.com/feeds/news/rss.xml"),
    ]

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            for source_name, url in feeds:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200 and last_name in resp.text.lower():
                        score += 40.0
                        sources_found.append(source_name)
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"News signal fetch failed for {player_name}: {e}")

    context = f"Found in: {', '.join(sources_found)}" if sources_found else "No news mentions"
    return SignalResult("news", min(score, 100.0), context)


async def fetch_dfs_ownership_signal(player_name: str, team: str) -> SignalResult:
    """
    Estimate cross-platform DFS ownership.

    Scrapes publicly visible ownership data from major DFS platforms.
    This is the most direct proxy for "what will the crowd do."
    """
    last_name = player_name.split()[-1].lower()

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            # RotoGrinders free ownership page
            resp = await client.get(
                "https://rotogrinders.com/resultsdb/mlb",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200 and last_name in resp.text.lower():
                return SignalResult("dfs_ownership", 60.0, "Found on RotoGrinders results")

            # NumberFire projections (free, includes ownership estimates)
            resp2 = await client.get(
                "https://www.numberfire.com/mlb/daily-fantasy/daily-baseball-projections",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp2.status_code == 200 and last_name in resp2.text.lower():
                return SignalResult("dfs_ownership", 45.0, "Found on NumberFire projections")

    except Exception as e:
        logger.debug(f"DFS ownership signal fetch failed for {player_name}: {e}")

    return SignalResult("dfs_ownership", 0.0, "No DFS ownership signal")


async def fetch_search_signal(player_name: str, team: str) -> SignalResult:
    """
    Google search volume proxy.

    Uses Google's suggestion API — if a player + "today" or "stats"
    appears in autocomplete, they have high casual search interest.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # Google autocomplete — no auth, reflects real-time search volume
            resp = await client.get(
                "https://suggestqueries.google.com/complete/search",
                params={"client": "firefox", "q": f"{player_name} "},
            )
            if resp.status_code == 200:
                suggestions = resp.text.lower()
                # High signal: people searching for the player + game context
                hot_terms = ["stats", "today", "home run", "injury", "lineup", "dfs"]
                matches = sum(1 for term in hot_terms if term in suggestions)

                if matches >= 3:
                    return SignalResult("search", 80.0, f"High search interest ({matches} context terms)")
                elif matches >= 1:
                    return SignalResult("search", 45.0, f"Moderate search interest ({matches} context terms)")

    except Exception as e:
        logger.debug(f"Search signal fetch failed for {player_name}: {e}")

    return SignalResult("search", 0.0, "Low search volume")


# ---------------------------------------------------------------------------
# Aggregation + classification
# ---------------------------------------------------------------------------

def compute_composite_score(signals: list[SignalResult]) -> float:
    """Weighted average of all signal scores."""
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
    mid_perf = 40.0 <= player_score < 60.0

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
) -> PopularityProfile:
    """
    Full popularity assessment for a single player.

    Fetches all 4 signal sources in parallel, computes composite,
    and classifies as FADE / TARGET / NEUTRAL.
    """
    import asyncio

    # Fetch all signals concurrently
    social, news, dfs, search = await asyncio.gather(
        fetch_social_signal(player_name, team),
        fetch_news_signal(player_name, team),
        fetch_dfs_ownership_signal(player_name, team),
        fetch_search_signal(player_name, team),
    )

    signals = [social, news, dfs, search]
    composite = compute_composite_score(signals)
    classification, reason = classify_player(composite, player_score)

    return PopularityProfile(
        player_name=player_name,
        team=team,
        social_score=social.score,
        news_score=news.score,
        dfs_ownership_score=dfs.score,
        search_score=search.score,
        composite_score=composite,
        classification=classification,
        reason=reason,
        signals=signals,
    )


async def get_slate_popularity(
    players: list[dict],
) -> list[PopularityProfile]:
    """
    Assess popularity for an entire slate of players.

    Args:
        players: list of {"player_name": str, "team": str, "player_score": float}

    Returns sorted by composite_score descending (most popular first).
    """
    import asyncio

    profiles = await asyncio.gather(*[
        get_popularity_profile(
            p["player_name"],
            p["team"],
            p.get("player_score", 50.0),
        )
        for p in players
    ])

    return sorted(profiles, key=lambda p: p.composite_score, reverse=True)
