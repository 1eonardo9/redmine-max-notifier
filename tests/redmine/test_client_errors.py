"""Тесты обработки ошибок RedmineClient.

Каждый статус → своё исключение с корректно заполненными полями.
Отдельно проверяем что API-ключ не утекает в текст/атрибуты исключений.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.redmine.exceptions import (
    RedmineAPIError,
    RedmineAuthError,
    RedmineNotFoundError,
    RedmineRateLimitError,
    RedmineServerError,
    RedmineValidationError,
)
from tests.conftest import TEST_API_KEY

# ---------- 401 / 403 -> RedmineAuthError ----------------------------------


async def test_401_raises_auth_error(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """401 Unauthorized → RedmineAuthError со статусом и телом ответа."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        status_code=401,
        text="Unauthorized",
    )

    with pytest.raises(RedmineAuthError) as exc_info:
        await client.get_current_user()

    exc = exc_info.value
    assert exc.status_code == 401
    assert exc.response_body == "Unauthorized"
    # 4xx не ретраится — должен быть ровно один поход
    assert len(httpx_mock.get_requests()) == 1


async def test_403_raises_auth_error(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """403 Forbidden → тоже RedmineAuthError (не хватает прав)."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues/1.json",
        status_code=403,
        text="Forbidden",
    )

    with pytest.raises(RedmineAuthError) as exc_info:
        await client.get_issue(1)

    assert exc_info.value.status_code == 403
    assert len(httpx_mock.get_requests()) == 1


# ---------- 404 -> RedmineNotFoundError ------------------------------------


async def test_404_raises_not_found_error(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """404 Not Found → RedmineNotFoundError."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues/999999.json",
        status_code=404,
        text="",
    )

    with pytest.raises(RedmineNotFoundError) as exc_info:
        await client.get_issue(999999)

    assert exc_info.value.status_code == 404
    assert len(httpx_mock.get_requests()) == 1


# ---------- 422 -> RedmineValidationError с распарсенным errors -----------


async def test_422_raises_validation_error_with_parsed_errors(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """422 Unprocessable Entity: список errors из JSON должен распарситься
    в атрибут exc.errors."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues/1.json",
        status_code=422,
        json={"errors": ["Subject can't be blank", "Tracker is invalid"]},
    )

    with pytest.raises(RedmineValidationError) as exc_info:
        await client.get_issue(1)

    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.errors == ["Subject can't be blank", "Tracker is invalid"]
    assert len(httpx_mock.get_requests()) == 1


async def test_422_without_errors_field_has_empty_list(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Если 422 пришёл без поля errors — exc.errors должен быть [] (не None)."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues/1.json",
        status_code=422,
        text="just a plain text body, not json",
    )

    with pytest.raises(RedmineValidationError) as exc_info:
        await client.get_issue(1)

    assert exc_info.value.errors == []


# ---------- 429 -> RedmineRateLimitError -----------------------------------


async def test_429_raises_rate_limit_error(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """429 Too Many Requests → RedmineRateLimitError, без ретраев."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        status_code=429,
        text="Too Many Requests",
    )

    with pytest.raises(RedmineRateLimitError) as exc_info:
        await client.get_current_user()

    assert exc_info.value.status_code == 429
    # 429 сейчас в ретраи не попадает (ретраим только Transport и 5xx).
    # Это осознанное решение — фиксируем тестом.
    assert len(httpx_mock.get_requests()) == 1


# ---------- прочие 4xx -> базовый RedmineAPIError --------------------------


async def test_unexpected_4xx_raises_generic_api_error(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Экзотические 4xx (например, 418) → базовый RedmineAPIError,
    но НЕ его подклассы вроде Auth/NotFound/Validation/RateLimit."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        status_code=418,
        text="I'm a teapot",
    )

    with pytest.raises(RedmineAPIError) as exc_info:
        await client.get_current_user()

    exc = exc_info.value
    # Проверяем что это именно "голый" APIError, а не какой-то конкретный подкласс
    assert type(exc) is RedmineAPIError
    assert exc.status_code == 418
    assert len(httpx_mock.get_requests()) == 1


# ---------- 5xx -> RedmineServerError (с ретраями — детально в чекпоинте 4) --


async def test_5xx_raises_server_error_after_retries_exhausted(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """5xx ретраится. При max_attempts=3 клиент сделает 3 попытки
    и пробросит RedmineServerError последней."""
    # Регистрируем один и тот же ответ 3 раза — pytest-httpx отдаст их по очереди.
    for _ in range(3):
        httpx_mock.add_response(
            method="GET",
            url=f"{base_url}/users/current.json",
            status_code=500,
            text="Internal Server Error",
        )

    with pytest.raises(RedmineServerError) as exc_info:
        await client.get_current_user()

    assert exc_info.value.status_code == 500
    # Именно 3 попытки: max_attempts из фикстуры client в conftest.py
    assert len(httpx_mock.get_requests()) == 3


# ---------- Санитизация: API-ключ не должен утечь ---------------------------


async def test_api_key_not_leaked_in_exception(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """API-ключ не должен появляться ни в str(exc), ни в атрибутах.

    Это защита от логов вида logger.error("Redmine error: %s", exc) —
    ключ не должен всплывать в централизованных логах.
    """
    # 5xx ретраится — клиент сделает max_attempts=3 попытки.
    # Регистрируем ответ 3 раза, иначе pytest-httpx на второй попытке
    # кинет TimeoutException и результат теста будет о другом.
    for _ in range(3):
        httpx_mock.add_response(
            method="GET",
            url=f"{base_url}/users/current.json",
            status_code=500,
            text="boom",
        )

    with pytest.raises(RedmineServerError) as exc_info:
        await client.get_current_user()

    exc = exc_info.value

    # 1. В строковом представлении исключения ключа быть не должно
    assert TEST_API_KEY not in str(exc)
    assert TEST_API_KEY not in repr(exc)

    # 2. В сохранённых атрибутах — тоже
    assert exc.url is not None
    assert TEST_API_KEY not in exc.url
    if exc.response_body is not None:
        assert TEST_API_KEY not in exc.response_body

    # 3. У исключения не должно быть словаря/атрибута с заголовками —
    #    там жил бы X-Redmine-API-Key. Проверяем известные "опасные" имена.
    for danger_attr in ("headers", "request_headers", "_headers"):
        assert not hasattr(exc, danger_attr), (
            f"У исключения не должно быть атрибута {danger_attr!r}: "
            "там могут оказаться заголовки с API-ключом"
        )
