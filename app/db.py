"""SQLite engine/session setup. Foreign keys are enforced on every connection."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

engine = None
SessionLocal: sessionmaker[Session] = sessionmaker(autoflush=False, expire_on_commit=False)


def init_engine(url: str | None = None):
    """(Re)bind the engine. Called at import time and by tests with a temp database."""
    global engine
    url = url or settings.database_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    if engine is not None:
        engine.dispose()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA secure_delete=ON")
        cur.close()

    SessionLocal.configure(bind=engine)
    return engine


init_engine()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
