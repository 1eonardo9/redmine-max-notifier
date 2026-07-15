"""Тесты успешных путей публичных методов MaxClient.

Каждый метод проверяем на:
- правильный HTTP-метод и URL уходят на сервер;
- правильные заголовки (в частности, Authorization без Bearer);
- корректный разбор JSON в Pydantic-модели;
- корректная сериализация тел запросов.
"""

from __future__ import annotations

import json

from pytest_httpx import HTTPXMock

from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.maxbot.models import MessageFormat
from tests.conftest import TEST_TOKEN

# Готовый payload ответа GET /me — используем в нескольких тестах.
# Timestamp: 1_720_000_000_000 мс = 2024-07-03 11:06:40 UTC.
BOT_INFO_PAYLOAD: dict[str, object] = {
    "user_id": 443542051,
    "first_name": "TestBot",
    "username": "test_bot",
    "is_bot": True,
    "last_activity_time": 1_720_000_000_000,
    "description": "Тестовый бот для юнит-тестов",
}


async def test_get_me_success(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /me → BotInfo с корректно распарсенными полями и правильным
    заголовком Authorization (голый токен, без Bearer)."""
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        json=BOT_INFO_PAYLOAD,
        status_code=200,
    )

    bot = await max_client.get_me()

    assert bot.user_id == 443542051
    assert bot.username == "test_bot"
    assert bot.is_bot is True
    # Миллисекундный timestamp конвертирован в timezone-aware datetime
    assert bot.last_activity_time.tzinfo is not None
    assert bot.last_activity_time.year == 2024

    # Проверяем что клиент ушёл с правильным Authorization-заголовком.
    # Ключевая проверка: БЕЗ префикса "Bearer " — это специфика MAX API.
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == TEST_TOKEN
    assert requests[0].headers["Content-Type"] == "application/json"


async def test_send_message_without_format(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """send_message без format: в теле запроса должен быть только text,
    а поле format НЕ должно попасть в JSON (exclude_none=True)."""
    httpx_mock.add_response(
        method="POST",
        url=f"{max_base_url}/messages?chat_id=-12345",
        json={"message": {"body": {"mid": "msg-abc", "seq": 1, "text": "Hello, MAX"}}},
        status_code=200,
    )

    sent = await max_client.send_message(chat_id=-12345, text="Hello, MAX")

    assert sent.message.body.mid == "msg-abc"
    assert sent.message.body.seq == 1

    # Проверяем содержимое отправленного тела запроса.
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    sent_body = json.loads(requests[0].content)
    assert sent_body == {"text": "Hello, MAX"}
    # format отсутствует — не должно уйти "format": null
    assert "format" not in sent_body


async def test_send_message_with_markdown_format(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """send_message с format=MARKDOWN: enum должен сериализоваться
    в строку 'markdown' (mode='json' в model_dump)."""
    httpx_mock.add_response(
        method="POST",
        url=f"{max_base_url}/messages?chat_id=-12345",
        json={"message": {"body": {"mid": "msg-xyz", "seq": 2, "text": "*bold*"}}},
        status_code=200,
    )

    await max_client.send_message(
        chat_id=-12345,
        text="*bold*",
        format=MessageFormat.MARKDOWN,
    )

    sent_body = json.loads(httpx_mock.get_requests()[0].content)
    assert sent_body == {"text": "*bold*", "format": "markdown"}


async def test_get_updates_without_marker(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /updates без marker: в query уходят только timeout и limit;
    ответ парсится в UpdatesResponse со списком событий."""
    payload = {
        "updates": [
            {
                "update_type": "bot_started",
                "timestamp": 1_720_000_000_000,
                "chat_id": 100,
            },
            {
                "update_type": "message_created",
                "timestamp": 1_720_000_001_000,
                "chat_id": 100,
            },
        ],
        "marker": 999,
    }
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/updates?timeout=30&limit=100",
        json=payload,
        status_code=200,
    )

    response = await max_client.get_updates()

    assert response.marker == 999
    assert len(response.updates) == 2
    assert response.updates[0].update_type == "bot_started"
    assert response.updates[0].chat_id == 100
    # Timestamp сконвертирован через тот же валидатор миллисекунд
    assert response.updates[0].timestamp.tzinfo is not None


async def test_get_updates_with_marker(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /updates с marker: marker уходит в query как отдельный параметр."""
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/updates?timeout=30&limit=100&marker=500",
        json={"updates": [], "marker": 501},
        status_code=200,
    )

    response = await max_client.get_updates(marker=500)

    assert response.marker == 501
    assert response.updates == []


async def test_get_updates_empty_response(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Long polling может завершиться без новых событий: пустой список
    и marker=None не должны ломать парсинг."""
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/updates?timeout=30&limit=100",
        json={"updates": []},
        status_code=200,
    )

    response = await max_client.get_updates()

    assert response.marker is None
    assert response.updates == []
