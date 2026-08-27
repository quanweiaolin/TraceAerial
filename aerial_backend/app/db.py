"""
异步 SQLAlchemy 引擎 + session。
FastAPI 每个请求通过 Depends(get_session) 拿一个独立session，
避免请求之间串用连接。
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,   # 连接失活自动重连，长时间跑的扫盘/后台任务尤其需要
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine, expire_on_commit=False, class_=AsyncSession
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI依赖：路由函数里写 session: AsyncSession = Depends(get_session)"""
    async with AsyncSessionLocal() as session:
        yield session


class SessionContext:
    """给后台任务(扫盘/通知worker)用，不在FastAPI请求上下文里时手动开关session。

    用法:
        async with SessionContext() as session:
            ...
    """

    async def __aenter__(self) -> AsyncSession:
        self._session = AsyncSessionLocal()
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            await self._session.rollback()
        await self._session.close()
