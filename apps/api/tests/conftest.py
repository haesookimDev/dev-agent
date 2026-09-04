from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db import get_session
from app.main import app
from app.models import Base


@pytest.fixture
async def client(tmp_path) -> AsyncIterator[AsyncClient]:
    database_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        artifact_root=str(tmp_path / "artifacts"),
        preview_allowed_cidrs=["10.0.0.0/8", "127.0.0.0/8"],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer development-worker-secret-change-me"}
