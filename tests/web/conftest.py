"""Фикстуры для тестов веб-слоя (FastAPI-приложение).

Именование фикстур — с префиксом app_*, чтобы в тестах не путать
с фикстурами redmine и maxbot клиентов.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from redmine_max_notifier.web.app import create_app


@pytest.fixture
def app_instance() -> FastAPI:
    """Свежий экземпляр FastAPI-приложения на каждый тест.

    Собираем через create_app() — так же, как в проде через uvicorn --factory.
    Свежий инстанс на тест страхует от протечки состояния между тестами
    (в будущем — app.state с ресурсами вроде БД).
    """
    return create_app()


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
