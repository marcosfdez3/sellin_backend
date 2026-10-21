from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

_is_sqlite = "sqlite" in settings.DATABASE_URL

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    # Para SQLite en dev; en PostgreSQL quita este parámetro
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    # PostgreSQL cierra las conexiones que llevan un rato sin usarse; sin esto,
    # la primera petición que coge una de ellas del pool falla con un 500.
    # pool_pre_ping la comprueba antes de usarla y pool_recycle la renueva.
    **({} if _is_sqlite else {"pool_pre_ping": True, "pool_recycle": 300}),
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    """Dependency para inyectar la sesión de BD en los endpoints."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    """Crea todas las tablas al arrancar la app."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
