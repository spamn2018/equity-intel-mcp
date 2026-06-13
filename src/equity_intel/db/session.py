"""Database session factory and helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from equity_intel.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

if _is_sqlite:
    from sqlalchemy.pool import NullPool

    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
        echo=settings.log_level.lower() == "debug",
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        # busy_timeout first -- no exclusive lock needed
        dbapi_conn.execute("PRAGMA busy_timeout=30000")
        dbapi_conn.execute("PRAGMA synchronous=NORMAL")
        # WAL mode needs a brief exclusive lock; ignore if DB is held by another process.
        # Once set it persists on disk -- subsequent calls are no-ops.
        try:
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass

else:
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        echo=settings.log_level.lower() == "debug",
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager that provides a database session and handles commit/rollback."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all_tables() -> None:
    """Create all tables (use Alembic for production migrations)."""
    from equity_intel.db.models import Base  # noqa: F401

    Base.metadata.create_all(bind=engine)


def enable_pg_fts_indexes(session: Session) -> None:
    """Add GIN indexes for full-text search if they don't exist."""
    statements = [
        """
        CREATE INDEX IF NOT EXISTS idx_filing_docs_fts
        ON filing_documents USING GIN (to_tsvector('english', coalesce(plain_text, '')));
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_news_articles_fts
        ON news_articles USING GIN (
            to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(body, ''))
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_events_fts
        ON events USING GIN (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, '')));
        """,
    ]
    for stmt in statements:
        try:
            session.execute(text(stmt))
        except Exception:
            pass
    session.commit()
