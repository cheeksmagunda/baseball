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


def init_db():
    """Bring the schema up to date via Alembic.

    Pre-Alembic DBs (tables present but no alembic_version) are stamped to
    the baseline first, then upgraded — this avoids "table already exists"
    errors on existing deployments.
    """
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if tables and "alembic_version" not in tables:
        # Existing pre-Alembic DB — adopt it at the baseline revision.
        command.stamp(cfg, "792e0bd8996d")
    command.upgrade(cfg, "head")
