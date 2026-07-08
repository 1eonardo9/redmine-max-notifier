"""Тесты ретраев RedmineClient.

Правила ретраев:
- транспортные ошибки (сеть, таймаут) → ретраятся;
- 5xx → ретраятся;
- 4xx → не ретраятся, пробрасываются с первой попытки;
- после max_attempts безуспешных попыток пробрасывается последнее исключение.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.redmine.exceptions import (
    RedmineNotFoundError,
    RedmineTransportError,
)
from tests.conftest import load_fixture

# ---------- Успех после ретрая на транспортной ошибке ----------------------


async def test_retries_recover_from_transport_error(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Первая попытка — сеть упала, вторая — успех. Результат должен вернуться."""
    # Первый вызов: имитируем сетевую ошибку через callback.
    # add_exception подсовывает исключение вместо ответа — это как раз то,
    # что клиент увидит при разрыве соединения на реальном хосте.
    httpx_mock.add_exception(
        httpx.ConnectError("simulated network failure"),
        method="GET",
        url=f"{base_url}/users/current.json",
    )
    # Второй вызов: нормальный ответ.
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        json=load_fixture("user_current.json"),
        status_code=200,
    )

    user = await client.get_current_user()

    assert user.id == 42
    # Ровно две попытки: первая упала, вторая успешна
    assert len(httpx_mock.get_requests()) == 2


# ---------- Успех после ретрая на 5xx --------------------------------------


async def test_retries_recover_from_5xx(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Первая попытка — 500, вторая — 200. Клиент должен вернуть результат."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        status_code=500,
        text="Internal Server Error",
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        json=load_fixture("user_current.json"),
        status_code=200,
    )

    user = await client.get_current_user()

    assert user.id == 42
    assert len(httpx_mock.get_requests()) == 2


# ---------- Исчерпание попыток на транспорте -------------------------------


async def test_transport_error_exhausts_attempts(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Все max_attempts=3 попытки падают транспортной — пробрасывается
    RedmineTransportError."""
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ConnectError("simulated network failure"),
            method="GET",
            url=f"{base_url}/users/current.json",
        )

    with pytest.raises(RedmineTransportError):
        await client.get_current_user()

    assert len(httpx_mock.get_requests()) == 3


# ---------- Таймаут — тоже транспортная ошибка -----------------------------


async def test_timeout_is_treated_as_transport_error(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """httpx.TimeoutException оборачивается в RedmineTransportError
    и точно так же ретраится."""
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ReadTimeout("simulated read timeout"),
            method="GET",
            url=f"{base_url}/users/current.json",
        )

    with pytest.raises(RedmineTransportError):
        await client.get_current_user()

    assert len(httpx_mock.get_requests()) == 3


# ---------- 4xx НЕ ретраится ----------------------------------------------


async def test_4xx_is_not_retried(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """404 приходит с первой попытки — второго похода быть не должно.

    Если ретрай сработал бы по ошибке, pytest-httpx на второй запрос
    кинет 'no response matched' и упадёт teardown фикстуры.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues/999.json",
        status_code=404,
        text="",
    )

    with pytest.raises(RedmineNotFoundError):
        await client.get_issue(999)

    assert len(httpx_mock.get_requests()) == 1


# ---------- Смешанный сценарий: транспорт → 5xx → успех --------------------


async def test_retries_across_transport_and_5xx(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Проверяем что счётчик попыток общий для транспорта и 5xx:
    транспорт (попытка 1) → 500 (попытка 2) → 200 (попытка 3, успех)."""
    httpx_mock.add_exception(
        httpx.ConnectError("network down"),
        method="GET",
        url=f"{base_url}/users/current.json",
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        status_code=500,
        text="internal error",
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        json=load_fixture("user_current.json"),
        status_code=200,
    )

    user = await client.get_current_user()

    assert user.id == 42
    assert len(httpx_mock.get_requests()) == 3


# ---------- Крайний случай: max_attempts=1 отключает ретраи ----------------


async def test_max_attempts_one_disables_retries(
    base_url: str,
    api_key: str,
    httpx_mock: HTTPXMock,
) -> None:
    """С max_attempts=1 ретраев нет — первая же транспортная ошибка пробрасывается."""
    # Отдельный клиент, а не фикстура — фикстура настроена на max_attempts=3.
    single_shot = RedmineClient(
        base_url=base_url,
        api_key=api_key,
        max_attempts=1,
        retry_base_delay=0.001,
        retry_max_delay=0.01,
    )
    httpx_mock.add_exception(
        httpx.ConnectError("network down"),
        method="GET",
        url=f"{base_url}/users/current.json",
    )

    try:
        with pytest.raises(RedmineTransportError):
            await single_shot.get_current_user()
        assert len(httpx_mock.get_requests()) == 1
    finally:
        await single_shot.aclose()
