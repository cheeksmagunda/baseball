"""Tests for the RotoWire daily-lineups parser.

The fixture file (`tests/fixtures/rotowire_sample.html`) mirrors the actual
RotoWire markup as of April 2026.  Tests run pure-function against the
fixture — no network required.

Coverage:
  * Two complete games parse with all 9 batters in HTML order
  * `is-confirmed` vs `is-expected` lineup status maps to the right enum
  * Tools/ad cards (`lineup is-mlb is-tools`) are skipped
  * Accent characters in <a title="..."> are preserved verbatim
  * Display name (link text) is captured separately from full name (title)
  * Switch hitters expose bats="S"; pitchers expose throws on the highlight
  * Pipeline integration: graceful failure, populate-on-success, official override
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.rotowire import (
    GameLineup,
    LineupStatus,
    parse_lineups_html,
)
from app.database import Base
# These imports register the SQLAlchemy models with Base.metadata so
# create_all(bind=engine) below can build their tables.  Without them, the
# in-memory DB is empty and inserts fail with "no such table".
from app.models import player as _player_models  # noqa: F401
from app.models import slate as _slate_models  # noqa: F401


@pytest.fixture
def db_session():
    """In-memory SQLite session with all tables created (mirrors test_smoke fixture)."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


FIXTURE = Path(__file__).parent / "fixtures" / "rotowire_sample.html"


@pytest.fixture
def sample_html() -> str:
    return FIXTURE.read_text()


@pytest.fixture
def games(sample_html) -> list[GameLineup]:
    return parse_lineups_html(sample_html)


class TestParseLineupsHtml:
    def test_skips_tools_and_ad_cards(self, games):
        # Fixture contains one `lineup is-mlb is-tools` decoy and two real games.
        assert len(games) == 2

    def test_first_game_team_abbreviations(self, games):
        g = games[0]
        assert g.visitor.team == "TB"
        assert g.home.team == "CLE"
        assert g.visitor.is_home is False
        assert g.home.is_home is True

    def test_second_game_team_abbreviations(self, games):
        g = games[1]
        assert g.visitor.team == "STL"
        assert g.home.team == "NYY"

    def test_status_confirmed(self, games):
        g = games[0]
        assert g.visitor.status == LineupStatus.CONFIRMED
        assert g.home.status == LineupStatus.CONFIRMED

    def test_status_expected(self, games):
        # Second game's visitor (STL) is "Expected Lineup".
        g = games[1]
        assert g.visitor.status == LineupStatus.EXPECTED
        assert g.home.status == LineupStatus.CONFIRMED

    def test_full_batting_order(self, games):
        g = games[0]
        # TB confirmed lineup, in order:
        names = [p.full_name for p in g.visitor.players]
        assert names == [
            "Yandy Diaz",
            "Jonathan Aranda",
            "Junior Caminero",
            "Ryan Vilade",
            "Jonny DeLuca",
            "Chandler Simpson",
            "Ben Williamson",
            "Nick Fortes",
            "Taylor Walls",
        ]
        assert [p.batting_order for p in g.visitor.players] == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    def test_positions_preserved(self, games):
        g = games[0]
        positions = [p.position for p in g.visitor.players]
        assert positions == ["DH", "1B", "3B", "RF", "CF", "LF", "2B", "C", "SS"]

    def test_display_name_vs_full_name(self, games):
        # RotoWire often abbreviates the link text but keeps the full name in
        # the title attribute.  The optimizer should match on full_name.
        g = games[0]
        aranda = g.visitor.players[1]
        assert aranda.name == "J. Aranda"
        assert aranda.full_name == "Jonathan Aranda"

    def test_accents_preserved(self, games):
        # Cleveland: J. Ramírez (display) / Jose Ramirez (title — accent-stripped
        # at the source).  We accept both forms — name normalisation happens
        # at the SlatePlayer match site, not in the parser.
        g = games[0]
        gimenez = g.home.players[0]
        assert gimenez.name == "A. Giménez"
        assert gimenez.full_name == "Andres Gimenez"
        ramirez = g.home.players[1]
        assert ramirez.name == "José Ramírez"

    def test_switch_hitter_bats_S(self, games):
        g = games[0]
        walls = g.visitor.players[8]
        assert walls.bats == "S"

    def test_starting_pitcher_with_throws(self, games):
        g = games[0]
        assert g.visitor.starting_pitcher == "Steven Matz"
        assert g.visitor.pitcher_throws == "L"
        assert g.home.starting_pitcher == "Tanner Bibee"
        assert g.home.pitcher_throws == "R"

    def test_judge_at_top_of_yankees_order(self, games):
        # Sanity: Yankees confirmed card has Judge in slot 1.
        g = games[1]
        judge = g.home.players[0]
        assert judge.full_name == "Aaron Judge"
        assert judge.batting_order == 1
        assert judge.position == "DH"


class TestParseLineupsResilience:
    def test_empty_html_returns_empty_list(self):
        assert parse_lineups_html("") == []

    def test_html_without_lineup_cards(self):
        assert parse_lineups_html("<html><body><p>No games today</p></body></html>") == []

    def test_card_with_only_pitchers_no_lineup(self):
        # A `lineup__list` block containing only the highlighted pitcher (no
        # batters posted yet) should be skipped — we have no batting orders.
        html = """
        <div class="lineup is-mlb">
          <div class="lineup__teams">
            <div class="lineup__abbr">TB</div>
            <div class="lineup__abbr">CLE</div>
          </div>
          <ul class="lineup__list is-visit">
            <li class="lineup__player-highlight">
              <div class="lineup__player-highlight-name">
                <a href="/p/matz">Steven Matz</a>
                <span class="lineup__throws">L</span>
              </div>
            </li>
          </ul>
          <ul class="lineup__list is-home">
            <li class="lineup__player-highlight">
              <div class="lineup__player-highlight-name">
                <a href="/p/bibee">Tanner Bibee</a>
                <span class="lineup__throws">R</span>
              </div>
            </li>
          </ul>
        </div>
        """
        assert parse_lineups_html(html) == []


# ---------------------------------------------------------------------------
# Pipeline integration: _enrich_batting_order_from_rotowire
# ---------------------------------------------------------------------------

class TestRotoWirePipelineIntegration:
    """The RotoWire enrichment must:
      1. Fail gracefully (warn + return 0) when the network call raises
      2. Skip pipeline cleanly when no parseable games come back
      3. Populate batting_order + batting_order_source on matching SlatePlayers
      4. Be overrideable by the official-card enrichment (Phase 2)
    """

    def _setup_slate(self, db_session):
        from datetime import date
        from app.models.player import Player, normalize_name
        from app.models.slate import Slate, SlateGame, SlatePlayer

        slate = Slate(date=date(2026, 4, 28))
        db_session.add(slate)
        db_session.flush()
        # Single game NYY vs BOS, two batters from each
        game = SlateGame(slate_id=slate.id, home_team="NYY", away_team="BOS",
                         mlb_game_pk=12345, game_status="Preview")
        db_session.add(game)
        db_session.flush()
        for name, team in [("Aaron Judge", "NYY"), ("Juan Soto", "NYY"),
                           ("Rafael Devers", "BOS"), ("Triston Casas", "BOS")]:
            p = Player(name=name, name_normalized=normalize_name(name),
                       team=team, position="OF")
            db_session.add(p)
            db_session.flush()
            sp = SlatePlayer(slate_id=slate.id, player_id=p.id, game_id=game.id)
            db_session.add(sp)
        db_session.commit()
        return slate

    def test_rotowire_failure_raises(self, db_session, monkeypatch):
        """RotoWire is the single source of truth for batting orders at T-65.
        A fetch failure must raise so the pipeline crashes and /optimize
        returns HTTP 503 — no silent degradation under the no-fallbacks rule."""
        import asyncio
        import pytest
        from app.services.data_collection import _enrich_batting_order_from_rotowire
        import logging

        slate = self._setup_slate(db_session)

        async def boom():
            raise RuntimeError("RotoWire fetch failed: connection reset")

        monkeypatch.setattr("app.core.rotowire.fetch_expected_lineups", boom)
        with pytest.raises(RuntimeError, match="RotoWire expected-lineup fetch failed"):
            asyncio.run(_enrich_batting_order_from_rotowire(
                db_session, slate, logging.getLogger("test")
            ))

    def test_rotowire_empty_result_raises(self, db_session, monkeypatch):
        """Zero parseable games means RotoWire's HTML markup changed — fail
        loudly so the parser can be fixed, never silently lose lineup data."""
        import asyncio
        import pytest
        from app.services.data_collection import _enrich_batting_order_from_rotowire
        import logging

        slate = self._setup_slate(db_session)

        async def empty():
            return []

        monkeypatch.setattr("app.core.rotowire.fetch_expected_lineups", empty)
        with pytest.raises(RuntimeError, match="RotoWire returned 0 parseable games"):
            asyncio.run(_enrich_batting_order_from_rotowire(
                db_session, slate, logging.getLogger("test")
            ))

    def test_rotowire_success_populates_batting_order_and_source(
        self, db_session, monkeypatch
    ):
        """A successful fetch sets batting_order + source on matching players."""
        import asyncio
        from app.core.rotowire import GameLineup, LineupPlayer, LineupStatus, TeamLineup
        from app.models.slate import SlatePlayer
        from app.services.data_collection import _enrich_batting_order_from_rotowire
        import logging

        slate = self._setup_slate(db_session)

        # Fake RotoWire result: NYY confirmed (Judge=1, Soto=2),
        # BOS expected (Devers=3, Casas=4).
        nyy = TeamLineup(
            team="NYY", is_home=True, starting_pitcher=None, pitcher_throws=None,
            status=LineupStatus.CONFIRMED,
            players=(
                LineupPlayer(name="Aaron Judge", full_name="Aaron Judge",
                             batting_order=1, position="DH", bats="R"),
                LineupPlayer(name="Juan Soto", full_name="Juan Soto",
                             batting_order=2, position="RF", bats="L"),
            ),
        )
        bos = TeamLineup(
            team="BOS", is_home=False, starting_pitcher=None, pitcher_throws=None,
            status=LineupStatus.EXPECTED,
            players=(
                LineupPlayer(name="Rafael Devers", full_name="Rafael Devers",
                             batting_order=3, position="3B", bats="L"),
                LineupPlayer(name="Triston Casas", full_name="Triston Casas",
                             batting_order=4, position="1B", bats="L"),
            ),
        )
        async def fake_fetch():
            return [GameLineup(visitor=bos, home=nyy)]

        monkeypatch.setattr("app.core.rotowire.fetch_expected_lineups", fake_fetch)

        result = asyncio.run(_enrich_batting_order_from_rotowire(
            db_session, slate, logging.getLogger("test")
        ))
        assert result == 4

        # Verify each player got the right order + source
        sps = (
            db_session.query(SlatePlayer)
            .filter_by(slate_id=slate.id)
            .all()
        )
        by_name = {sp.player.name: sp for sp in sps}
        assert by_name["Aaron Judge"].batting_order == 1
        assert by_name["Aaron Judge"].batting_order_source == "rotowire_confirmed"
        assert by_name["Juan Soto"].batting_order == 2
        assert by_name["Juan Soto"].batting_order_source == "rotowire_confirmed"
        assert by_name["Rafael Devers"].batting_order == 3
        assert by_name["Rafael Devers"].batting_order_source == "rotowire_expected"
        assert by_name["Triston Casas"].batting_order == 4
        assert by_name["Triston Casas"].batting_order_source == "rotowire_expected"


# ---------------------------------------------------------------------------
# Probable-starter parser robustness — title attribute fallback
# ---------------------------------------------------------------------------

class TestParseStartingPitcherTitleAttr:
    """RotoWire's pitcher highlight link sometimes uses an abbreviated text
    label ("L. Webb") with the full name in the <a title="..."> attribute,
    same as their batter rows.  The parser must prefer `title` so the
    downstream `_backfill_probable_starters_from_rotowire` lookup against
    `Player.name_normalized` actually finds the active-roster row.  Pre-fix,
    abbreviated links silently failed the lookup for any RotoWire-projected
    starter on a game where MLB hydrate had not announced one.
    """

    def _make_html(self, title_attr: str | None, text: str) -> str:
        title_html = f' title="{title_attr}"' if title_attr is not None else ""
        return f"""
        <div class="lineup is-mlb">
          <div class="lineup__teams">
            <div class="lineup__abbr">PIT</div>
            <div class="lineup__abbr">SF</div>
          </div>
          <ul class="lineup__list is-visit">
            <li class="lineup__player-highlight">
              <div class="lineup__player-highlight-name">
                <a href="/p/x"{title_html}>{text}</a>
                <span class="lineup__throws">R</span>
              </div>
            </li>
            <li class="lineup__player">
              <div class="lineup__pos">DH</div>
              <a href="/p/y" title="Some Hitter">S. Hitter</a>
              <span class="lineup__bats">R</span>
            </li>
          </ul>
          <ul class="lineup__list is-home">
            <li class="lineup__player-highlight">
              <div class="lineup__player-highlight-name">
                <a href="/p/z" title="Logan Webb">L. Webb</a>
                <span class="lineup__throws">R</span>
              </div>
            </li>
            <li class="lineup__player">
              <div class="lineup__pos">DH</div>
              <a href="/p/w" title="Other Hitter">O. Hitter</a>
              <span class="lineup__bats">R</span>
            </li>
          </ul>
        </div>
        """

    def test_uses_title_attribute_when_present(self):
        # Title attribute carries the full name; visible text is abbreviated.
        html = self._make_html("Logan Webb", "L. Webb")
        games = parse_lineups_html(html)
        assert len(games) == 1
        assert games[0].home.starting_pitcher == "Logan Webb"

    def test_falls_back_to_text_when_no_title(self):
        # If the link has no title attribute, the visible text is used —
        # historical fixture compatibility.
        html = self._make_html(None, "Steven Matz")
        games = parse_lineups_html(html)
        assert len(games) == 1
        assert games[0].visitor.starting_pitcher == "Steven Matz"


# ---------------------------------------------------------------------------
# RotoWire-projected starter backfill onto SlateGame
# ---------------------------------------------------------------------------

class TestBackfillProbableStartersFromRotoWire:
    """The new T-65 fix: when the MLB Stats API probablePitcher hydrate has
    no starter for one team in a game, RotoWire's beat-reporter projection
    fills that gap.  Previously the pipeline crashed with
    'PIT@SF home starter NOT ANNOUNCED'; now the RotoWire-backfilled
    starter flows through the rest of stage 2 normally.

    The helper preserves the no-fallback rule:
      * It only writes a name + mlb_id sourced from a real live data
        provider (active-roster Player table or MLB /people/search).
      * It never invents a starter.  If RotoWire AND MLB both fail to
        resolve, the slot stays empty and the per-game drop in
        run_fetch_player_stats handles it.
      * It never overwrites an already-populated starter (MLB hydrate
        wins when both sources agree).
    """

    def _setup_two_team_game(self, db_session, *, home_starter=None, away_starter=None):
        """One SlateGame PIT@SF, with PIT and SF active rosters loaded."""
        from datetime import date as _date
        from app.models.player import Player, normalize_name
        from app.models.slate import Slate, SlateGame

        slate = Slate(date=_date(2026, 5, 8))
        db_session.add(slate)
        db_session.flush()

        game = SlateGame(
            slate_id=slate.id,
            home_team="SF",
            away_team="PIT",
            mlb_game_pk=99001,
            game_status="Preview",
            home_starter=home_starter,
            away_starter=away_starter,
        )
        db_session.add(game)
        db_session.flush()

        # Active rosters with mlb_ids.
        for full_name, team, mlb_id, position in [
            ("Logan Webb", "SF", 657277, "P"),
            ("Mitch Keller", "PIT", 656605, "P"),
        ]:
            p = Player(
                name=full_name,
                name_normalized=normalize_name(full_name),
                team=team,
                position=position,
                mlb_id=mlb_id,
                pitch_hand="R",
            )
            db_session.add(p)
        db_session.commit()
        return slate, game

    def _make_rw_games(self, *, sf_pitcher="Logan Webb", pit_pitcher="Mitch Keller",
                       sf_throws="R", pit_throws="R"):
        from app.core.rotowire import GameLineup, LineupStatus, TeamLineup

        pit_lineup = TeamLineup(
            team="PIT", is_home=False,
            starting_pitcher=pit_pitcher, pitcher_throws=pit_throws,
            status=LineupStatus.EXPECTED, players=(),
        )
        sf_lineup = TeamLineup(
            team="SF", is_home=True,
            starting_pitcher=sf_pitcher, pitcher_throws=sf_throws,
            status=LineupStatus.EXPECTED, players=(),
        )
        return [GameLineup(visitor=pit_lineup, home=sf_lineup)]

    def test_backfills_missing_home_starter_from_rotowire(self, db_session):
        """The user's exact scenario: MLB API has PIT's starter but not
        SF's; RotoWire has both.  After backfill the SlateGame must have
        SF starter set from RotoWire data."""
        import asyncio
        import logging
        from app.services.data_collection import _backfill_probable_starters_from_rotowire

        slate, game = self._setup_two_team_game(
            db_session,
            home_starter=None,                  # SF unannounced (the bug case)
            away_starter="Mitch Keller",        # PIT announced via MLB
        )
        rw_games = self._make_rw_games()

        backfilled = asyncio.run(_backfill_probable_starters_from_rotowire(
            db_session, slate, rw_games, logging.getLogger("test")
        ))
        db_session.commit()
        db_session.refresh(game)

        assert backfilled == 1
        assert game.home_starter == "Logan Webb"
        assert game.home_starter_mlb_id == 657277
        assert game.home_starter_hand == "R"
        # MLB-set away starter is untouched.
        assert game.away_starter == "Mitch Keller"

    def test_does_not_overwrite_mlb_announced_starter(self, db_session):
        """When MLB hydrate already populated the starter, RotoWire never
        overwrites it.  MLB is canonical when both have data."""
        import asyncio
        import logging
        from app.services.data_collection import _backfill_probable_starters_from_rotowire

        slate, game = self._setup_two_team_game(
            db_session,
            home_starter="Robbie Ray",          # MLB wins
            away_starter="Mitch Keller",
        )
        rw_games = self._make_rw_games(sf_pitcher="Logan Webb")

        backfilled = asyncio.run(_backfill_probable_starters_from_rotowire(
            db_session, slate, rw_games, logging.getLogger("test")
        ))
        db_session.refresh(game)

        assert backfilled == 0
        assert game.home_starter == "Robbie Ray"
        assert game.away_starter == "Mitch Keller"

    def test_no_op_when_rotowire_also_has_no_starter(self, db_session):
        """Both MLB and RotoWire missing a starter: the slot stays empty.
        We never invent a name.  The per-game drop in run_fetch_player_stats
        handles the resulting gap."""
        import asyncio
        import logging
        from app.core.rotowire import GameLineup, LineupStatus, TeamLineup
        from app.services.data_collection import _backfill_probable_starters_from_rotowire

        slate, game = self._setup_two_team_game(
            db_session, home_starter=None, away_starter=None,
        )
        # RotoWire has the visit team but no SF pitcher projected yet.
        rw_games = [GameLineup(
            visitor=TeamLineup(
                team="PIT", is_home=False,
                starting_pitcher="Mitch Keller", pitcher_throws="R",
                status=LineupStatus.EXPECTED, players=(),
            ),
            home=TeamLineup(
                team="SF", is_home=True,
                starting_pitcher=None, pitcher_throws=None,
                status=LineupStatus.EXPECTED, players=(),
            ),
        )]

        backfilled = asyncio.run(_backfill_probable_starters_from_rotowire(
            db_session, slate, rw_games, logging.getLogger("test")
        ))
        db_session.commit()
        db_session.refresh(game)

        assert backfilled == 1                        # PIT was filled
        assert game.away_starter == "Mitch Keller"    # PIT filled by RotoWire
        assert game.home_starter is None              # SF still empty — correct
        assert game.home_starter_mlb_id is None

    def test_falls_back_to_people_search_when_off_roster(self, db_session, monkeypatch):
        """RotoWire-projected starter not on the active roster (recent
        call-up / IL stash starting today) — resolve mlb_id via MLB's
        /people/search.  Strict team match required; never guesses."""
        import asyncio
        import logging
        from app.services import data_collection
        from app.services.data_collection import _backfill_probable_starters_from_rotowire

        slate, game = self._setup_two_team_game(
            db_session, home_starter=None, away_starter="Mitch Keller",
        )
        # RotoWire projects an off-roster pitcher for SF.
        rw_games = self._make_rw_games(sf_pitcher="Trevor McDonald")

        async def fake_search(name: str):
            assert name == "Trevor McDonald"
            return [
                {"id": 700001, "currentTeam": {"abbreviation": "AAA"}},  # mismatch
                {"id": 700002, "currentTeam": {"abbreviation": "SF"}},   # match
            ]

        monkeypatch.setattr(data_collection, "search_player", fake_search)

        backfilled = asyncio.run(_backfill_probable_starters_from_rotowire(
            db_session, slate, rw_games, logging.getLogger("test")
        ))
        db_session.commit()
        db_session.refresh(game)

        assert backfilled == 1
        assert game.home_starter == "Trevor McDonald"
        assert game.home_starter_mlb_id == 700002    # exact-team match, not first

    def test_skips_when_off_roster_and_no_team_match_in_search(
        self, db_session, monkeypatch
    ):
        """Never guess the mlb_id.  If /people/search has no exact team
        match, leave the slot empty and let the per-game drop handle it."""
        import asyncio
        import logging
        from app.services import data_collection
        from app.services.data_collection import _backfill_probable_starters_from_rotowire

        slate, game = self._setup_two_team_game(
            db_session, home_starter=None, away_starter="Mitch Keller",
        )
        rw_games = self._make_rw_games(sf_pitcher="Phantom Pitcher")

        async def fake_search(name: str):
            return [{"id": 999999, "currentTeam": {"abbreviation": "AAA"}}]

        monkeypatch.setattr(data_collection, "search_player", fake_search)

        backfilled = asyncio.run(_backfill_probable_starters_from_rotowire(
            db_session, slate, rw_games, logging.getLogger("test")
        ))
        db_session.refresh(game)

        assert backfilled == 0
        assert game.home_starter is None
        assert game.home_starter_mlb_id is None
