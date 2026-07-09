"""Общие фикстуры для тестов MaxClient.

Отдельный conftest на подпапке — pytest подхватывает его автоматически
для всех тестов в tests/maxbot/. Фикстуры называются max_*, чтобы явно
отличать их от redmine-фикстур в родительском tests/conftest.py — так
внутри тестов невозможно случайно перепутать клиента.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from redmine_max_notifier.maxbot.client import MaxClient

# База, к которой клиент "как будто" ходит.
# Реального сервера нет — pytest-httpx перехватит все запросы.
TEST_BASE_URL = "https://max.test.local"
TEST_TOKEN = "test-token-do-not-log"


@pytest.fixture
def max_base_url() -> str:
    """Базовый URL, к которому 'ходит' тестовый MaxClient."""
    return TEST_BASE_URL


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

    Используется как async-фикстура: yield отдаёт клиент тесту,
    после теста управление возвращается сюда и клиент закрывается.
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
