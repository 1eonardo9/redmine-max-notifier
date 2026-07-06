"""Иерархия исключений клиента Redmine.

Все исключения наследуются от RedmineError — это позволяет ловить их разом
(except RedmineError) там, где не важна конкретная причина сбоя, и по
конкретным классам там, где нужна реакция «по ситуации» (ретрай, пропустить,
упасть громко).

Две ветки:
- RedmineTransportError — не долетели до сервера или ответ не пришёл
  (сеть, DNS, таймаут). Ретраить имеет смысл.
- RedmineAPIError — сервер ответил, но статус плохой (4xx/5xx). Дальше
  делится по коду: auth, not_found, validation, rate_limit, server.
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


class RedmineError(Exception):
    """Базовое исключение клиента Redmine.

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


class RedmineTransportError(RedmineError):
    """Транспортная ошибка: до сервера не долетели или ответ не получили.

    Причины: DNS, обрыв соединения, таймаут, TLS-ошибка.
    Имеет смысл ретраить с бэкоффом.
    """


class RedmineAPIError(RedmineError):
    """Сервер ответил, но статус неудачный (4xx/5xx).

    Хранит код статуса и (возможно обрезанное) тело ответа.
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


class RedmineAuthError(RedmineAPIError):
    """401 Unauthorized или 403 Forbidden.

    Причины: невалидный/просроченный API-ключ, недостаточно прав.
    Ретраить бессмысленно.
    """


class RedmineNotFoundError(RedmineAPIError):
    """404 Not Found.

    Ресурс не существует или недоступен под текущим ключом.
    """


class RedmineValidationError(RedmineAPIError):
    """422 Unprocessable Entity.

    Redmine отбраковал запрос (например, обязательные поля не заполнены).
    Тело ответа содержит поле "errors" — список причин, распарсенных
    в _request и переданных сюда готовым списком.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        url: str | None = None,
        response_body: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            url=url,
            response_body=response_body,
        )
        self.errors = errors or []


class RedmineRateLimitError(RedmineAPIError):
    """429 Too Many Requests.

    Redmine ограничил частоту запросов. Ретрай возможен с задержкой.
    """


class RedmineServerError(RedmineAPIError):
    """5xx Internal Server Error / Bad Gateway / Service Unavailable.

    Проблема на стороне сервера. Имеет смысл ретраить.
    """
