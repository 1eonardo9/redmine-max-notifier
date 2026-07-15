"""Общие фикстуры для всех тестов.

conftest.py — специальное имя: pytest автоматически подгружает этот файл
и делает объявленные в нём фикстуры доступными во всех тестах текущего
каталога и подкаталогов, без импорта.

Здесь живут фикстуры клиента Redmine и слоя БД. Фикстуры БД лежали
в tests/db/conftest.py, пока БД была нужна только тестам моделей;
с этапа 7e она нужна и диспетчеру (tests/test_dispatcher.py), и
поллер-job'у на 7f — а conftest виден только своему каталогу и ниже.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from redmine_max_notifier.db import Base
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.redmine.client import RedmineClient

# Каталог с JSON-фикстурами (реальные примеры ответов Redmine).
FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Базы, к которым клиенты "как будто" ходят.
# Реального сервера нет — pytest-httpx перехватит все запросы.
TEST_BASE_URL = "https://redmine.test.local"
TEST_API_KEY = "test-api-key-do-not-log"

MAX_TEST_BASE_URL = "https://max.test.local"
TEST_TOKEN = "test-token-do-not-log"


def load_fixture(name: str) -> dict[str, Any]:
    """Прочитать JSON-фикстуру из tests/fixtures/ по имени файла.

    Пример: load_fixture("user_current.json") -> dict с телом ответа.
    """
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


@pytest.fixture
def base_url() -> str:
    """Базовый URL, к которому 'ходит' тестовый клиент."""
    return TEST_BASE_URL


@pytest.fixture
def api_key() -> str:
    """API-ключ, используемый в тестах. Специально с говорящим именем —
    если он вдруг просочится в лог или в str(exc), это будет сразу видно.
    """
    return TEST_API_KEY


@pytest_asyncio.fixture
async def client(base_url: str, api_key: str) -> AsyncIterator[RedmineClient]:
    """Готовый RedmineClient с малыми задержками ретрая — чтобы тесты
    ретраев не занимали секунды.

    Используется как async-фикстура: yield отдаёт клиент тесту,
    после теста управление возвращается сюда и клиент закрывается.
    """
    c = RedmineClient(
        base_url=base_url,
        api_key=api_key,
        max_attempts=3,
        retry_base_delay=0.001,  # 1мс вместо 1с — тесты не должны тормозить
        retry_max_delay=0.01,
    )
    try:
        yield c
    finally:
        await c.aclose()


# ── MAX ──────────────────────────────────────────────────────────────────
# Именование с префиксом max_*, чтобы внутри теста было невозможно
# случайно перепутать клиента MAX с клиентом Redmine.


@pytest.fixture
def max_base_url() -> str:
    """Базовый URL, к которому 'ходит' тестовый MaxClient."""
    return MAX_TEST_BASE_URL


@pytest.fixture
def max_token() -> str:
    """Токен, используемый в тестах. Специально с говорящим именем —
    если он вдруг просочится в лог или в str(exc), это будет сразу видно.
    """
    return TEST_TOKEN


@pytest_asyncio.fixture
async def max_client(
    max_base_url: str,
    max_token: str,
) -> AsyncIterator[MaxClient]:
    """Готовый MaxClient с малыми задержками ретрая — чтобы тесты
    ретраев не занимали секунды.
    """
    c = MaxClient(
        token=max_token,
        base_url=max_base_url,
        max_attempts=3,
        retry_base_delay=0.001,  # 1мс вместо 1с — тесты не должны тормозить
        retry_max_delay=0.01,
    )
    try:
        yield c
    finally:
        await c.close()


# ── Слой БД ──────────────────────────────────────────────────────────────
# Именование с префиксом db_*, чтобы в тестах не путать с фикстурами
# Redmine, MAX и веб-слоя.
#
# Стратегия: in-memory SQLite со свежей схемой на каждый тест. Никаких
# файлов на диске, никакого шаринга состояния между тестами.
#
# ВАЖНО про :memory: и async: у in-memory SQLite схема живёт в рамках
# одного соединения. Если engine откроет разные соединения для разных
# операций, они увидят разные (пустые) БД. Поэтому StaticPool: один и
# тот же коннекшн переиспользуется. Это нормально только для тестов —
# в проде так делать нельзя.
#
# Схему поднимаем через Base.metadata.create_all(), а НЕ через
# alembic upgrade head: тесты должны быть быстрыми. Тест «миграция
# создаёт правильную схему» — на Этапе 8 (интеграционные).


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
