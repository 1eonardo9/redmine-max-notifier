"""Общие фикстуры для всех тестов клиента Redmine.

conftest.py — специальное имя: pytest автоматически подгружает этот файл
и делает объявленные в нём фикстуры доступными во всех тестах текущего
каталога и подкаталогов, без импорта.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from redmine_max_notifier.redmine.client import RedmineClient

# Каталог с JSON-фикстурами (реальные примеры ответов Redmine).
FIXTURES_DIR = Path(__file__).parent / "fixtures"

# База, к которой клиент "как будто" ходит.
# Реального сервера нет — pytest-httpx перехватит все запросы.
TEST_BASE_URL = "https://redmine.test.local"
TEST_API_KEY = "test-api-key-do-not-log"


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
