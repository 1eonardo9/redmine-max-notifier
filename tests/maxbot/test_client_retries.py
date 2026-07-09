"""Тесты ретраев MaxClient.

Правила ретраев:
- транспортные ошибки (сеть, таймаут, TLS) → ретраятся;
- 5xx → ретраятся;
- 4xx (включая 429) → не ретраятся, пробрасываются с первой попытки;
- после max_attempts безуспешных попыток пробрасывается последнее исключение.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.maxbot.exceptions import (
    MaxNotFoundError,
    MaxTransportError,
)

# Готовый payload успешного ответа GET /me — переиспользуем в нескольких тестах.
BOT_INFO_PAYLOAD: dict[str, object] = {
    "user_id": 443542051,
    "first_name": "TestBot",
    "username": "test_bot",
    "is_bot": True,
    "last_activity_time": 1_720_000_000_000,
}


# ---------- Успех после ретрая на транспортной ошибке ----------------------


async def test_retries_recover_from_transport_error(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Первая попытка — сеть упала, вторая — успех. Результат должен вернуться."""
    # add_exception подсовывает исключение вместо ответа — это как раз то,
    # что клиент увидит при разрыве соединения на реальном хосте.
    httpx_mock.add_exception(
        httpx.ConnectError("simulated network failure"),
        method="GET",
        url=f"{max_base_url}/me",
    )
    # Второй вызов: нормальный ответ.
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        json=BOT_INFO_PAYLOAD,
        status_code=200,
    )

    bot = await max_client.get_me()

    assert bot.user_id == 443542051
    # Ровно две попытки: первая упала, вторая успешна
    assert len(httpx_mock.get_requests()) == 2


# ---------- Успех после ретрая на 5xx --------------------------------------


async def test_retries_recover_from_5xx(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Первая попытка — 503, вторая — 200. Клиент должен вернуть результат."""
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        status_code=503,
        text="Service Unavailable",
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        json=BOT_INFO_PAYLOAD,
        status_code=200,
    )

    bot = await max_client.get_me()

    assert bot.user_id == 443542051
    assert len(httpx_mock.get_requests()) == 2


# ---------- Исчерпание попыток на транспорте -------------------------------


async def test_transport_error_exhausts_attempts(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Все max_attempts=3 попытки падают транспортной — пробрасывается
    MaxTransportError."""
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ConnectError("simulated network failure"),
            method="GET",
            url=f"{max_base_url}/me",
        )

    with pytest.raises(MaxTransportError):
        await max_client.get_me()

    assert len(httpx_mock.get_requests()) == 3


# ---------- Таймаут — тоже транспортная ошибка -----------------------------


async def test_timeout_is_treated_as_transport_error(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """httpx.ReadTimeout оборачивается в MaxTransportError и точно так же
    ретраится. TimeoutException — подкласс TransportError; порядок except
    в клиенте (сначала TimeoutException) даёт более точное сообщение."""
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ReadTimeout("simulated read timeout"),
            method="GET",
            url=f"{max_base_url}/me",
        )

    with pytest.raises(MaxTransportError) as exc_info:
        await max_client.get_me()

    # Проверяем что сообщение сформировано веткой "таймаут",
    # а не "сетевая ошибка" — это подтверждает правильный порядок except.
    assert "таймаут" in exc_info.value.message
    assert len(httpx_mock.get_requests()) == 3


# ---------- 4xx НЕ ретраится ----------------------------------------------


async def test_4xx_is_not_retried(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """404 приходит с первой попытки — второго похода быть не должно.

    Если ретрай сработал бы по ошибке, pytest-httpx на второй запрос
    кинет 'no response for request' и тест упадёт.
    """
    httpx_mock.add_response(
        method="POST",
        url=f"{max_base_url}/messages?chat_id=-99999",
        status_code=404,
        text="chat not found",
    )

    with pytest.raises(MaxNotFoundError):
        await max_client.send_message(chat_id=-99999, text="hi")

    assert len(httpx_mock.get_requests()) == 1


# ---------- Смешанный сценарий: транспорт → 5xx → успех --------------------


async def test_retries_across_transport_and_5xx(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Счётчик попыток общий для транспорта и 5xx:
    транспорт (попытка 1) → 503 (попытка 2) → 200 (попытка 3, успех)."""
    httpx_mock.add_exception(
        httpx.ConnectError("network down"),
        method="GET",
        url=f"{max_base_url}/me",
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        status_code=503,
        text="Service Unavailable",
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        json=BOT_INFO_PAYLOAD,
        status_code=200,
    )

    bot = await max_client.get_me()

    assert bot.user_id == 443542051
    assert len(httpx_mock.get_requests()) == 3


# ---------- Крайний случай: max_attempts=1 отключает ретраи ----------------


async def test_max_attempts_one_disables_retries(
    max_base_url: str,
    max_token: str,
    httpx_mock: HTTPXMock,
) -> None:
    """С max_attempts=1 ретраев нет — первая же транспортная ошибка
    пробрасывается сразу."""
    # Отдельный клиент, а не фикстура — фикстура настроена на max_attempts=3.
    single_shot = MaxClient(
        token=max_token,
        base_url=max_base_url,
        max_attempts=1,
        retry_base_delay=0.001,
        retry_max_delay=0.01,
    )
    httpx_mock.add_exception(
        httpx.ConnectError("network down"),
        method="GET",
        url=f"{max_base_url}/me",
    )

    try:
        with pytest.raises(MaxTransportError):
            await single_shot.get_me()
        assert len(httpx_mock.get_requests()) == 1
    finally:
        await single_shot.close()


# ---------- Валидация max_attempts на входе -------------------------------


async def test_max_attempts_zero_raises_value_error(
    max_base_url: str,
    max_token: str,
) -> None:
    """max_attempts < 1 бессмысленно — конструктор должен ругнуться сразу,
    а не при первом запросе."""
    with pytest.raises(ValueError, match="max_attempts"):
        MaxClient(
            token=max_token,
            base_url=max_base_url,
            max_attempts=0,
        )
