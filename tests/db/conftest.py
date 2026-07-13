"""Фикстуры для тестов слоя БД.

Именование — с префиксом db_*, чтобы в тестах не путать с
фикстурами Redmine, MAX и веб-слоя.

Стратегия: in-memory SQLite (sqlite+aiosqlite:///:memory:) со
свежей схемой на каждый тест. Никаких файлов на диске, никакого
шаринга состояния между тестами.

ВАЖНО про :memory: и async: у in-memory SQLite схема живёт в
рамках одного соединения. Если engine откроет разные соединения
для разных операций, они увидят разные (пустые) БД. Поэтому в
db_engine ставим StaticPool: один и тот же коннекшн переиспользуется.
Это нормально только для тестов — в проде так делать нельзя.

Схему поднимаем через Base.metadata.create_all(), а НЕ через
alembic upgrade head. На этом этапе миграцию мы уже проверили
руками, а тесты моделей должны быть быстрыми. Тест «миграция
создаёт правильную схему» вернём на Этапе 8 (интеграционные).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from redmine_max_notifier.db import Base


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Свежий AsyncEngine к in-memory SQLite со схемой из Base.metadata."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
def db_session_factory(
    db_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Фабрика async-сессий на движке из фикстуры db_engine.

    Тесты, которые хотят проверить настоящий round-trip через БД
    (пишем в одной сессии, читаем в новой), берут эту фабрику
    и открывают несколько сессий подряд. Разные сессии = разные
    identity map'ы, свежее чтение из SQLite гарантировано.
    """
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
async def db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Одна async-сессия для простых тестов, где хватит одной сессии.

    Для тестов, где важен именно round-trip запись→чтение через БД,
    бери db_session_factory и открывай две сессии.
    """
    async with db_session_factory() as session:
        yield session
