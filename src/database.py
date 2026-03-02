"""Engines de SQLAlchemy para web async y workers sync."""
from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import settings


def _sync_database_url(url: str) -> str:
    """Convierte una URL asyncpg a psycopg2 para contextos síncronos."""
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "+psycopg2")
    return url

engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # Verifica conexión antes de usar
    echo=False,
)

AsyncSessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

sync_engine = create_engine(
    _sync_database_url(settings.database_url),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=False,
)

SyncSessionFactory = sessionmaker(
    bind=sync_engine,
    class_=Session,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection de FastAPI — provee sesión de DB por request."""
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
