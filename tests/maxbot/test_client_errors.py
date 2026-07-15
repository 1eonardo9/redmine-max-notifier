"""Тесты обработки ошибок MaxClient.

Каждый статус → своё исключение с корректно заполненными полями.
Отдельно проверяем что токен бота не утекает в текст/атрибуты исключений.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.maxbot.exceptions import (
    MaxAPIError,
    MaxAuthError,
    MaxNotFoundError,
    MaxRateLimitError,
    MaxServerError,
    MaxValidationError,
)
from tests.conftest import TEST_TOKEN

# ---------- 400 -> MaxValidationError --------------------------------------


async def test_400_raises_validation_error(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """400 Bad Request → MaxValidationError со статусом и телом ответа.
    У MAX формат ошибки — {"code": "...", "message": "..."}, но в клиенте
    мы его не парсим отдельно, а храним как response_body."""
    httpx_mock.add_response(
        method="POST",
        url=f"{max_base_url}/messages?chat_id=-12345",
        status_code=400,
        json={"code": "text.too.long", "message": "Text exceeds 4000 chars"},
    )

    with pytest.raises(MaxValidationError) as exc_info:
        await max_client.send_message(chat_id=-12345, text="x" * 5000)

    exc = exc_info.value
    assert exc.status_code == 400
    # Тело ответа целиком доступно для разбора вызывающим кодом
    assert exc.response_body is not None
    assert "text.too.long" in exc.response_body
    # 4xx не ретраится — ровно один поход
    assert len(httpx_mock.get_requests()) == 1


# ---------- 401 -> MaxAuthError --------------------------------------------


async def test_401_raises_auth_error(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """401 Unauthorized → MaxAuthError.
    Причина: невалидный/отозванный токен, бот удалён."""
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        status_code=401,
        text="Unauthorized",
    )

    with pytest.raises(MaxAuthError) as exc_info:
        await max_client.get_me()

    assert exc_info.value.status_code == 401
    assert exc_info.value.response_body == "Unauthorized"
    assert len(httpx_mock.get_requests()) == 1


# ---------- 404 -> MaxNotFoundError ----------------------------------------


async def test_404_raises_not_found_error(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """404 Not Found → MaxNotFoundError. Например, отправка в чат,
    из которого бот удалён."""
    httpx_mock.add_response(
        method="POST",
        url=f"{max_base_url}/messages?chat_id=-99999",
        status_code=404,
        text="chat not found",
    )

    with pytest.raises(MaxNotFoundError) as exc_info:
        await max_client.send_message(chat_id=-99999, text="hi")

    assert exc_info.value.status_code == 404
    assert len(httpx_mock.get_requests()) == 1


# ---------- 405 и прочие 4xx -> базовый MaxAPIError ------------------------


async def test_405_raises_generic_api_error(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """405 Method Not Allowed → базовый MaxAPIError, а не конкретный подкласс.
    Отдельного класса для 405 не заводили — экзотика."""
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        status_code=405,
        text="Method Not Allowed",
    )

    with pytest.raises(MaxAPIError) as exc_info:
        await max_client.get_me()

    exc = exc_info.value
    # Именно "голый" MaxAPIError, а не Auth/NotFound/Validation/RateLimit
    assert type(exc) is MaxAPIError
    assert exc.status_code == 405
    assert len(httpx_mock.get_requests()) == 1


async def test_unexpected_4xx_raises_generic_api_error(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Экзотические 4xx (например, 418) → базовый MaxAPIError."""
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        status_code=418,
        text="I'm a teapot",
    )

    with pytest.raises(MaxAPIError) as exc_info:
        await max_client.get_me()

    assert type(exc_info.value) is MaxAPIError
    assert exc_info.value.status_code == 418


# ---------- 429 -> MaxRateLimitError без ретраев ---------------------------


async def test_429_raises_rate_limit_error_and_not_retried(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """429 Too Many Requests → MaxRateLimitError с первой же попытки.

    Осознанное решение: 429 в ретраи не попадает (ретраим только Transport
    и 5xx). Фиксируем тестом — если кто-то добавит 429 в ретраи, тест упадёт,
    потому что второго ответа для него не зарегистрировано.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{max_base_url}/me",
        status_code=429,
        text="Too Many Requests",
    )

    with pytest.raises(MaxRateLimitError) as exc_info:
        await max_client.get_me()

    assert exc_info.value.status_code == 429
    assert len(httpx_mock.get_requests()) == 1


# ---------- 5xx -> MaxServerError после исчерпания попыток ----------------


async def test_503_raises_server_error_after_retries_exhausted(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """503 Service Unavailable ретраится. При max_attempts=3 клиент сделает
    3 попытки и пробросит MaxServerError последней."""
    # Регистрируем один и тот же ответ 3 раза — pytest-httpx отдаст их
    # по очереди. Одиночный add_response выдаётся только один раз.
    for _ in range(3):
        httpx_mock.add_response(
            method="GET",
            url=f"{max_base_url}/me",
            status_code=503,
            text="Service Unavailable",
        )

    with pytest.raises(MaxServerError) as exc_info:
        await max_client.get_me()

    assert exc_info.value.status_code == 503
    # Именно 3 попытки: max_attempts из фикстуры max_client в conftest
    assert len(httpx_mock.get_requests()) == 3


# ---------- Санитизация: токен не должен утечь -----------------------------


async def test_token_not_leaked_in_exception(
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Токен бота не должен появляться ни в str(exc), ни в атрибутах.

    Это защита от логов вида logger.error("MAX error: %s", exc) —
    токен не должен всплывать в централизованных логах.
    """
    # 5xx ретраится — клиент сделает max_attempts=3 попытки.
    for _ in range(3):
        httpx_mock.add_response(
            method="GET",
            url=f"{max_base_url}/me",
            status_code=500,
            text="boom",
        )

    with pytest.raises(MaxServerError) as exc_info:
        await max_client.get_me()

    exc = exc_info.value

    # 1. В строковом представлении исключения токена быть не должно
    assert TEST_TOKEN not in str(exc)
    assert TEST_TOKEN not in repr(exc)

    # 2. В сохранённых атрибутах — тоже
    assert exc.url is not None
    assert TEST_TOKEN not in exc.url
    if exc.response_body is not None:
        assert TEST_TOKEN not in exc.response_body

    # 3. У исключения не должно быть словаря/атрибута с заголовками —
    #    там жил бы Authorization: <token>. Проверяем известные "опасные" имена.
    for danger_attr in ("headers", "request_headers", "_headers"):
        assert not hasattr(exc, danger_attr), (
            f"У исключения не должно быть атрибута {danger_attr!r}: "
            "там могут оказаться заголовки с токеном"
        )


# tests/maxbot/test_client_errors.py — в самый низ

# ---------- Валидация входа: пустой токен ---------------------------------


async def test_empty_token_raises_value_error(max_base_url: str) -> None:
    """Пустой токен → ValueError в конструкторе. Лучше упасть с понятным
    сообщением сразу, чем ловить 401 при первом запросе."""
    with pytest.raises(ValueError, match="токен"):
        MaxClient(token="", base_url=max_base_url)
