from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# Configure database connection pool for production stability
# pool_size: Max number of connections to maintain in the pool (Railway dyno typical)
# max_overflow: Allow temporary connections above pool_size for traffic spikes
# pool_recycle: Recycle connections every 1 hour (Railway Postgres timeout ~4 hours)
# pool_pre_ping: Test connection before use (auto-recovery from stale connections)
# SQLite requires check_same_thread=False; PostgreSQL does not accept this arg.
_connect_args = {"check_same_thread": False} if "sqlite" in settings.database_url else {}
engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
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
