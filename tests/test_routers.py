"""
HTTP contract tests for key API routers.

All tests use an isolated FastAPI app with in-memory SQLite (StaticPool) and
a mocked lineup cache — no real DB file, no Redis, no MLB API calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models.player import Player, normalize_name
from app.routers import players, scoring


# ---------------------------------------------------------------------------
# Shared app fixture
# ---------------------------------------------------------------------------

def _make_app(session_factory) -> FastAPI:
    """Minimal FastAPI app wired to the in-memory session factory."""
    app = FastAPI()

    def _override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    app.include_router(players.router, prefix="/api/players")
    app.include_router(scoring.router, prefix="/api/score")
    return app


@pytest.fixture
def client():
    """TestClient backed by an isolated in-memory SQLite DB."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    app = _make_app(Session)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_player(session, name="Aaron Judge", team="NYY", position="OF"):
    p = Player(
        name=name,
        name_normalized=normalize_name(name),
        team=team,
        position=position,
    )
    session.add(p)
    session.commit()
    return p


# ---------------------------------------------------------------------------
# GET /api/players
# ---------------------------------------------------------------------------

class TestListPlayers:
    def test_empty_db_returns_empty_list(self, client):
        c, _ = client
        r = c.get("/api/players")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_inserted_player(self, client):
        c, Session = client
        with Session() as db:
            _add_player(db)
        r = c.get("/api/players")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "Aaron Judge"
        assert data[0]["team"] == "NYY"

    def test_filter_by_team(self, client):
        c, Session = client
        with Session() as db:
            _add_player(db, name="Aaron Judge", team="NYY")
            _add_player(db, name="Rafael Devers", team="BOS")
        r = c.get("/api/players?team=NYY")
        assert r.status_code == 200
        names = [p["name"] for p in r.json()]
        assert "Aaron Judge" in names
        assert "Rafael Devers" not in names

    def test_filter_by_position(self, client):
        c, Session = client
        with Session() as db:
            _add_player(db, name="Gerrit Cole", team="NYY", position="P")
            _add_player(db, name="Aaron Judge", team="NYY", position="OF")
        r = c.get("/api/players?position=P")
        assert r.status_code == 200
        positions = {p["position"] for p in r.json()}
        assert positions == {"P"}

    def test_search_by_name(self, client):
        c, Session = client
        with Session() as db:
            _add_player(db, name="Aaron Judge", team="NYY")
            _add_player(db, name="Gerrit Cole", team="NYY", position="P")
        r = c.get("/api/players?search=judge")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "Aaron Judge"

    def test_limit_enforced(self, client):
        c, Session = client
        with Session() as db:
            for i in range(10):
                _add_player(db, name=f"Player {i}", team="NYY")
        r = c.get("/api/players?limit=3")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_limit_above_500_rejected(self, client):
        c, _ = client
        r = c.get("/api/players?limit=501")
        assert r.status_code == 422

    def test_get_player_not_found(self, client):
        c, _ = client
        r = c.get("/api/players/9999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/score/player
# ---------------------------------------------------------------------------

class TestScorePlayer:
    def test_player_not_found_returns_404(self, client):
        c, _ = client
        r = c.post("/api/score/player?player_name=Nobody+McFakename")
        assert r.status_code == 404

    def test_card_boost_param_no_longer_accepted(self, client):
        """V11.0: card_boost was removed from /api/score/player.  Sending it
        as a query param is silently ignored (FastAPI doesn't 422 on extras),
        and the response no longer contains a card_boost field."""
        c, Session = client
        with Session() as db:
            _add_player(db, name="Aaron Judge", team="NYY", position="OF")

        with patch("app.routers.scoring.score_player") as mock_score:
            mock_result = MagicMock()
            mock_result.player_name = "Aaron Judge"
            mock_result.team = "NYY"
            mock_result.position = "OF"
            mock_result.total_score = 72.5
            mock_result.traits = []
            mock_score.return_value = mock_result

            r = c.post("/api/score/player?player_name=Aaron+Judge&card_boost=3.1")

        assert r.status_code == 200
        assert "card_boost" not in r.json()
        assert "expected_value" not in r.json()

    def test_valid_player_returns_score(self, client):
        c, Session = client
        with Session() as db:
            _add_player(db, name="Aaron Judge", team="NYY", position="OF")

        with patch("app.routers.scoring.score_player") as mock_score:
            mock_result = MagicMock()
            mock_result.player_name = "Aaron Judge"
            mock_result.team = "NYY"
            mock_result.position = "OF"
            mock_result.total_score = 72.5
            mock_result.traits = []
            mock_score.return_value = mock_result

            r = c.post("/api/score/player?player_name=Aaron+Judge&team=NYY")

        assert r.status_code == 200
        data = r.json()
        assert data["player_name"] == "Aaron Judge"
        assert data["total_score"] == 72.5


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------

class TestHealth:
    def _health_app(self, startup_set: bool):
        """Build a minimal app with the health endpoint and a controlled startup event."""
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        import asyncio
        from app.services import app_state

        # Reset the event to a known state for this test
        if startup_set:
            app_state.startup_done_event.set()
        else:
            app_state.startup_done_event.clear()

        test_app = FastAPI()

        # Re-register the health endpoint inline so we don't import main.py
        # (which triggers the lifespan with real Redis/DB validation)
        @test_app.get("/api/health")
        async def health():
            checks: dict = {}
            checks["startup"] = "ok" if app_state.startup_done_event.is_set() else "starting"

            async def _check_redis() -> str:
                return "ok"

            async def _check_db() -> str:
                return "ok"

            checks["redis"], checks["db"] = await asyncio.gather(
                _check_redis(), _check_db()
            )
            ok = all(v == "ok" for v in checks.values())
            return JSONResponse(
                status_code=200 if ok else 503,
                content={"status": "ok" if ok else "degraded", "checks": checks},
            )

        return test_app

    def test_startup_not_set_returns_503(self):
        app = self._health_app(startup_set=False)
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/api/health")
        assert r.status_code == 503
        body = r.json()
        assert body["checks"]["startup"] == "starting"

    def test_startup_set_returns_200(self):
        app = self._health_app(startup_set=True)
        with TestClient(app, raise_server_exceptions=True) as c:
            r = c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["checks"]["startup"] == "ok"

    def teardown_method(self, _):
        """Always clear the startup event after health tests to avoid leaking state."""
        from app.services import app_state
        app_state.startup_done_event.clear()
