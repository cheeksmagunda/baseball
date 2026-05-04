from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# SQLite uses SingletonThreadPool / NullPool / StaticPool — none accept QueuePool
# args (pool_size, max_overflow, pool_recycle). Those are Postgres-only knobs.
# Likewise, check_same_thread=False is required for SQLite under FastAPI's
# multi-threaded request handling but is rejected by the Postgres driver.
_is_sqlite = settings.database_url.startswith("sqlite")
if _is_sqlite:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )
else:
    # Production pool config for Railway Postgres.
    # pool_size: connections kept open per dyno
    # max_overflow: burst capacity above pool_size
    # pool_recycle: recycle hourly (Railway Postgres idle timeout ~4h)
    # pool_pre_ping: test connection before use (recover from stale conns)
    engine = create_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=20,
        pool_recycle=3600,
        pool_pre_ping=True,
    )
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables from SQLAlchemy models.

    The DB is ephemeral by design — it stores only current-cycle live state
    (today's slate, players, scores, frozen picks). Every container restart
    on Railway wipes the SQLite file. There is no schema to evolve, so
    migrations are the wrong tool. The models are the single source of
    truth for the schema.
    """
    import app.models  # noqa: F401 — registers all models on Base.metadata

    Base.metadata.create_all(engine)
