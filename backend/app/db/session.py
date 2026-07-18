"""SQLAlchemy engine and session management for PostgreSQL."""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Create one reusable PostgreSQL engine per process."""

    settings = get_settings()
    return create_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        connect_args={"connect_timeout": settings.postgres_connect_timeout},
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Create one configured SQLAlchemy session factory per process."""

    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False)


def get_db_session() -> Generator[Session, None, None]:
    """FastAPI dependency that opens one session and always closes it."""

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def close_database_engine() -> None:
    """Release pooled database connections during FastAPI shutdown."""

    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
