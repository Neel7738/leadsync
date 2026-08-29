"""
Database persistence layer for conversation history and audit trail.

Supports:
  - PostgreSQL (production, via psycopg2)
  - SQLite (development/testing, built-in)

Usage:
    from core.database import get_db, init_db
    from core.database.models import ConversationRecord, AuditLog

    # Initialize tables (call once at startup)
    init_db()

    # Get a session
    with get_db() as db:
        db.add(ConversationRecord(...))
        db.commit()
"""

import logging
from contextlib import contextmanager
from typing import Optional, Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase

from ..config import get_settings

logger = logging.getLogger("Database")


# ── Base class for all models ──────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Engine & Session Factory ──────────────────────────────────
_engine = None
_SessionFactory = None


def _get_database_url() -> str:
    """Get database URL from settings, with SQLite fallback."""
    settings = get_settings()
    url = getattr(settings, "database_url", None)
    if url:
        return url
    # Default to SQLite for zero-config development
    import os
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "sfa.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return f"sqlite:///{db_path}"


def get_engine():
    """Get or create the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        url = _get_database_url()
        kwargs = {"echo": False}

        if url.startswith("sqlite"):
            # SQLite-specific settings
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # PostgreSQL connection pooling
            kwargs["pool_size"] = 5
            kwargs["max_overflow"] = 10
            kwargs["pool_pre_ping"] = True

        _engine = create_engine(url, **kwargs)

        # Enable WAL mode for SQLite (better concurrency)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        logger.info(f"Database engine created: {url.split('@')[-1] if '@' in url else url}")
    return _engine


def get_session_factory():
    """Get or create the session factory."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Get a database session with automatic commit/rollback."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables. Call once at application startup."""
    # Import models to register them with Base.metadata
    from . import models  # noqa: F401
    engine = get_engine()
    Base.metadata.create_all(engine)
    logger.info("Database tables created/verified")


def reset_db() -> None:
    """Drop and recreate all tables. USE WITH CAUTION (tests only)."""
    global _engine, _SessionFactory
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _SessionFactory = None  # Reset session factory
    logger.warning("Database reset — all data dropped and recreated")
