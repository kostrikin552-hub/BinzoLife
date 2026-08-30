from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,      # проверка соединения перед использованием
    pool_recycle=1800,       # пересоздавать соединения через 30 минут (избегает "server closed connection")
    pool_size=20,            # размер пула
    max_overflow=10,         # дополнительные соединения при пиковой нагрузке
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
