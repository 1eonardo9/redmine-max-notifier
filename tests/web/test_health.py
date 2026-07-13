"""Тесты health-эндпоинта и базовой работы веб-приложения."""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI


async def test_health_returns_ok(app_client: httpx.AsyncClient) -> None:
    """GET /health отдаёт 200 и {"status": "ok"}."""
    response = await app_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_content_type_is_json(app_client: httpx.AsyncClient) -> None:
    """Content-Type ответа — application/json.

    Проверка страхует от случайной регрессии, если кто-то решит
    вернуть Response(content=..., media_type="text/plain") напрямую.
    """
    response = await app_client.get("/health")

    assert response.headers["content-type"].startswith("application/json")


async def test_unknown_path_returns_404(app_client: httpx.AsyncClient) -> None:
    """Приложение не роняется на несуществующем пути, корректно возвращает 404."""
    response = await app_client.get("/definitely-not-a-real-endpoint")

    assert response.status_code == 404


async def test_lifespan_logs_startup_and_shutdown(
    app_instance: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Lifespan проходит startup и shutdown, каждый пишет в лог.

    ASGITransport в httpx не гоняет lifespan-события — это осознанное
    ограничение httpx (в тестах роутов lifespan обычно не нужен, и
    поднимать его на каждый запрос дорого).

    Вход/выход в lifespan-контекст FastAPI прогоняем вручную через
    app.router.lifespan_context — тот же механизм, что FastAPI
    использует под капотом при реальном запуске через uvicorn.
    """
    caplog.set_level(logging.INFO, logger="redmine_max_notifier.web.app")

    async with app_instance.router.lifespan_context(app_instance):
        # Внутри контекста приложение "запущено" — startup уже прошёл.
        pass
    # После выхода — shutdown завершён.

    messages = [record.message for record in caplog.records]
    assert "Приложение запускается" in messages
    assert "Приложение остановлено" in messages
