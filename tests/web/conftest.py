"""Фикстуры для тестов веб-слоя (FastAPI-приложение).

Именование фикстур — с префиксом app_*, чтобы в тестах не путать
с фикстурами redmine и maxbot клиентов.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from redmine_max_notifier.config import Settings
from redmine_max_notifier.web.app import create_app


@pytest.fixture
def app_settings() -> Settings:
    """Settings для тестов: in-memory SQLite.

    'sqlite+aiosqlite:///:memory:' — БД живёт в оперативке процесса,
    исчезает вместе с engine. Никаких файлов на диске, никаких
    коллизий между тестами. Каждый create_engine() поднимает свою
    новую in-memory БД — идеально для изоляции.

    # type: ignore[call-arg] — mypy не знает, что pydantic-settings
    # умеет брать значения из окружения; передаём напрямую как kwarg
    # — это разрешённый способ создать Settings в тесте.
    """
    return Settings(database_url="sqlite+aiosqlite:///:memory:")


@pytest.fixture
def app_raw(app_settings: Settings) -> FastAPI:
    """Сырой FastAPI без запущенного lifespan.

    Нужна тестам, которые сами гоняют lifespan_context — иначе
    получилась бы вложенность 'lifespan внутри lifespan' с двумя
    engine на один тест. Обычные тесты роутов пусть берут
    app_instance — там lifespan уже прогнан фикстурой.
    """
    return create_app(settings=app_settings)


@pytest.fixture
async def app_instance(app_settings: Settings) -> AsyncIterator[FastAPI]:
    """Свежий экземпляр FastAPI-приложения на каждый тест.

    Собираем через create_app(settings=...) с in-memory-БД. Проходим
    полный lifespan вручную через app.router.lifespan_context(app):
    ASGITransport НЕ прогоняет lifespan (осознанное ограничение httpx),
    поэтому startup/shutdown зовём сами. Так же тестируем, что engine
    реально создаётся и корректно закрывается по dispose().
    """
    app = create_app(settings=app_settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def app_client(app_instance: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент, гоняющий запросы напрямую в ASGI-приложение.

    ASGITransport подменяет сетевой транспорт httpx: вместо TCP-сокета
    запрос уходит прямо в приложение как ASGI-scope. Никакого реального
    сервера не поднимается — быстро и без побочки.

    base_url обязателен для httpx, но фактически не используется
    (сеть не задействована). "http://testserver" — общепринятая
    конвенция в FastAPI/Starlette-тестах.
    """
    transport = httpx.ASGITransport(app=app_instance)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client
