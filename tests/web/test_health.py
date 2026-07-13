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
    app_raw: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Lifespan проходит startup и shutdown, каждый пишет в лог.

    Берём app_raw (без запущенного lifespan), устанавливаем caplog
    ДО входа в контекст — иначе startup-лог не будет пойман, — и
    сами прогоняем полный цикл. Проверяем подстроки, а не точный
    текст: сообщения могут дополняться (сейчас там уже дописано
    "инициализация БД" / "закрытие БД"), тест не должен ломаться
    от косметических правок.
    """
    caplog.set_level(logging.INFO, logger="redmine_max_notifier.web.app")

    async with app_raw.router.lifespan_context(app_raw):
        pass

    messages = [record.message for record in caplog.records]
    assert any("запускается" in m for m in messages)
    assert any("остановлено" in m for m in messages)
