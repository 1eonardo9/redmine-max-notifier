"""Иерархия исключений клиента MAX Bot API.

Все исключения наследуются от MaxError — это позволяет ловить их разом
(except MaxError) там, где не важна конкретная причина сбоя, и по
конкретным классам там, где нужна реакция «по ситуации» (ретрай,
пропустить, упасть громко).

Две ветки:
- MaxTransportError — не долетели до сервера или ответ не пришёл
  (сеть, DNS, таймаут, TLS). Ретраить имеет смысл.
- MaxAPIError — сервер ответил, но статус плохой (4xx/5xx). Дальше
  делится по коду: auth, not_found, validation, rate_limit, server.

ВАЖНО: в атрибутах исключений НЕТ заголовков запроса — там жил бы
токен бота. Храним только url, статус и (обрезанное) тело ответа.
"""

from __future__ import annotations

# Максимальная длина тела ответа, которую сохраняем в исключении.
# Всё что длиннее — обрезаем, чтобы в логах не оказалось мегабайта JSON.
_MAX_BODY_LEN = 2000


def _truncate(body: str | None) -> str | None:
    """Обрезать длинное тело ответа для хранения в исключении."""
    if body is None:
        return None
    if len(body) <= _MAX_BODY_LEN:
        return body
    return body[:_MAX_BODY_LEN] + f"... [обрезано, всего {len(body)} символов]"


class MaxError(Exception):
    """Базовое исключение клиента MAX.

    Ловить имеет смысл там, где не важна конкретная причина.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.url = url


class MaxTransportError(MaxError):
    """Транспортная ошибка: до сервера не долетели или ответ не получили.

    Причины: DNS, обрыв соединения, таймаут, TLS-ошибка.
    Имеет смысл ретраить с бэкоффом.
    """


class MaxAPIError(MaxError):
    """Сервер ответил, но статус неудачный (4xx/5xx).

    Хранит код статуса и (возможно обрезанное) тело ответа.
    Базовый класс для всех «сервер ответил плохим статусом» — конкретные
    подклассы ниже. Прямо MaxAPIError поднимается только для нестандартных
    статусов (например, 405 Method Not Allowed) — их отдельно не выделяем.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        url: str | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message, url=url)
        self.status_code = status_code
        self.response_body = _truncate(response_body)


class MaxAuthError(MaxAPIError):
    """401 Unauthorized.

    Причины: невалидный/отозванный токен, бот удалён.
    Ретраить бессмысленно — конфигурационная ошибка.
    """


class MaxNotFoundError(MaxAPIError):
    """404 Not Found.

    Ресурс не существует или недоступен: например, chat_id указывает
    на чат, из которого бот удалён, или на несуществующий чат.
    """


class MaxValidationError(MaxAPIError):
    """400 Bad Request.

    MAX отбраковал запрос: невалидное тело, слишком длинный текст,
    неподдерживаемый формат вложения и т. п. В теле ответа обычно
    приходит JSON вида {"code": "...", "message": "..."} — детали
    остаются в response_body, отдельно не парсим.
    """


class MaxRateLimitError(MaxAPIError):
    """429 Too Many Requests.

    Превышен лимит 30 rps. Ретрай возможен с задержкой, но в клиенте
    сознательно НЕ ретраится — поведение зафиксировано тестом,
    поднимается сразу наружу.
    """


class MaxServerError(MaxAPIError):
    """5xx Internal Server Error / Service Unavailable.

    Проблема на стороне сервера MAX. Имеет смысл ретраить.
    """
