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

    def test_rotowire_failure_logs_and_returns_zero(self, db_session, monkeypatch):
        """A network error must NOT crash the pipeline — RotoWire is best-effort."""
        import asyncio
        from app.services.data_collection import _enrich_batting_order_from_rotowire
        import logging

        slate = self._setup_slate(db_session)

        async def boom():
            raise RuntimeError("RotoWire fetch failed: connection reset")

        monkeypatch.setattr("app.core.rotowire.fetch_expected_lineups", boom)
        result = asyncio.run(_enrich_batting_order_from_rotowire(
            db_session, slate, logging.getLogger("test")
        ))
        assert result == 0

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

    def test_official_boxscore_overrides_rotowire(self, db_session, monkeypatch):
        """Phase 2 (MLB API) must override Phase 1 (RotoWire) when official
        cards are posted — official is ground truth."""
        import asyncio
        from app.models.slate import SlatePlayer
        from app.services.data_collection import _enrich_batting_order
        import logging

        slate = self._setup_slate(db_session)

        # Pre-populate one batter with a RotoWire-projected slot 5 to simulate
        # the post-Phase-1 state.  Then call Phase 2 with an official card
        # that puts him in slot 1.
        sp = (
            db_session.query(SlatePlayer)
            .join(SlatePlayer.player)
            .filter(SlatePlayer.slate_id == slate.id)
            .filter_by()
            .first()
        )
        target_player = sp.player  # whoever was first
        sp.batting_order = 5
        sp.batting_order_source = "rotowire_expected"
        db_session.commit()

        # Build a fake boxscore that puts the player in slot 1.
        async def fake_boxscore(_pk):
            return {
                "teams": {
                    "home" if target_player.team == "NYY" else "away": {
                        "team": {"abbreviation": target_player.team},
                        "players": {
                            f"ID{target_player.mlb_id or 1}": {
                                "battingOrder": "100",
                                "person": {"id": target_player.mlb_id or 1},
                            },
                        },
                    },
                    "away" if target_player.team == "NYY" else "home": {
                        "team": {"abbreviation":
                                 "BOS" if target_player.team == "NYY" else "NYY"},
                        "players": {},
                    },
                },
            }

        # Boxscore enrich requires player.mlb_id to match what's in the response.
        target_player.mlb_id = 99999
        db_session.commit()

        async def patched_boxscore(pk):
            return await fake_boxscore(pk)

        monkeypatch.setattr(
            "app.services.data_collection.get_game_boxscore", patched_boxscore
        )

        from app.models.slate import SlateGame
        games = list(db_session.query(SlateGame).filter_by(slate_id=slate.id).all())
        result = asyncio.run(_enrich_batting_order(
            db_session, slate, games, logging.getLogger("test")
        ))
        assert result == 1

        db_session.refresh(sp)
        assert sp.batting_order == 1
        assert sp.batting_order_source == "official"
