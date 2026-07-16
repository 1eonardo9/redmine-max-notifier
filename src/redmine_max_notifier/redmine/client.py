from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from redmine_max_notifier.redmine.exceptions import (
    RedmineAPIError,
    RedmineAuthError,
    RedmineNotFoundError,
    RedmineRateLimitError,
    RedmineServerError,
    RedmineTransportError,
    RedmineValidationError,
)
from redmine_max_notifier.redmine.models import (
    Issue,
    Journal,
    Priority,
    Status,
    User,
)

log = logging.getLogger(__name__)


class RedmineClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float | httpx.Timeout = 10.0,
        *,
        max_attempts: int = 3,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 30.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts должно быть >= 1, получено {max_attempts}")
        self._base_url = base_url
        self._api_key = api_key
        if isinstance(timeout, int | float):
            self._timeout = httpx.Timeout(
                connect=5.0,  # Подключение
                read=timeout,  # чтение, дольше, основной лимит, передаем как есть.
                write=timeout,  # запись, так же как и чтение, нужно тестировать
                pool=5.0,  # ожидание соединения из пула
            )
        else:
            self._timeout = timeout
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "X-Redmine-API-Key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=self._timeout,
            trust_env=False,
        )

    async def aclose(self) -> None:
        """Закрыть внутренний HTTP-клиент и освободить пул соединений."""
        await self._client.aclose()

    async def __aenter__(self) -> RedmineClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int | bool | None] | None = None,
        json_body: dict[str, object] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        """Отправить HTTP-запрос к Redmine и вернуть распарсенный JSON.

        Транспортные ошибки (сеть, таймаут) и 5xx автоматически ретраятся
        с экспоненциальным бэкоффом и jitter'ом. 4xx ретраить бессмысленно —
        прокидываются наружу с первой попытки.
        """
        last_exc: RedmineTransportError | RedmineServerError | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                # --- 1. HTTP-вызов: транспортные ошибки httpx оборачиваем в наши. ---
                try:
                    request_kwargs: dict[str, Any] = {
                        "method": method,
                        "url": path,
                        "params": params,
                        "json": json_body,
                    }
                    if timeout is not None:
                        request_kwargs["timeout"] = timeout
                    response = await self._client.request(**request_kwargs)
                except httpx.TimeoutException as exc:
                    raise RedmineTransportError(
                        f"таймаут при запросе {method} {path}",
                        url=path,
                    ) from exc
                except httpx.TransportError as exc:
                    raise RedmineTransportError(
                        f"сетевая ошибка при запросе {method} {path}: {exc}",
                        url=path,
                    ) from exc

                # --- 2. Успех: сразу возвращаем распарсенный JSON. ---
                if 200 <= response.status_code < 300:
                    data: dict[str, Any] = response.json()
                    return data

                # --- 3. Ошибка API: маппинг статуса на исключение. ---
                body = response.text
                status = response.status_code

                if status == 422:
                    errors: list[str] | None = None
                    try:
                        body_json = response.json()
                        if isinstance(body_json, dict):
                            raw_errors = body_json.get("errors")
                            if isinstance(raw_errors, list):
                                errors = [str(e) for e in raw_errors]
                    except ValueError:
                        pass
                    raise RedmineValidationError(
                        f"валидация не пройдена ({method} {path}): {errors or body}",
                        status_code=status,
                        url=path,
                        response_body=body,
                        errors=errors,
                    )
                if status in (401, 403):
                    raise RedmineAuthError(
                        f"ошибка авторизации {status} для {method} {path}",
                        status_code=status,
                        url=path,
                        response_body=body,
                    )
                if status == 404:
                    raise RedmineNotFoundError(
                        f"ресурс не найден: {method} {path}",
                        status_code=status,
                        url=path,
                        response_body=body,
                    )
                if status == 429:
                    raise RedmineRateLimitError(
                        f"превышен лимит запросов ({method} {path})",
                        status_code=status,
                        url=path,
                        response_body=body,
                    )
                if 500 <= status < 600:
                    raise RedmineServerError(
                        f"серверная ошибка {status} ({method} {path})",
                        status_code=status,
                        url=path,
                        response_body=body,
                    )
                raise RedmineAPIError(
                    f"неожиданный статус {status} ({method} {path})",
                    status_code=status,
                    url=path,
                    response_body=body,
                )

            except (RedmineTransportError, RedmineServerError) as exc:
                # --- 4. Ретраим только транспорт и 5xx. ---
                last_exc = exc
                if attempt >= self._max_attempts:
                    log.error(
                        "исчерпаны попытки (%d/%d) для %s %s: %s",
                        attempt,
                        self._max_attempts,
                        method,
                        path,
                        exc,
                    )
                    raise
                delay = min(
                    self._retry_base_delay
                    * (2 ** (attempt - 1))
                    * random.uniform(0.8, 1.2),
                    self._retry_max_delay,
                )
                log.warning(
                    "попытка %d/%d неудачна (%s %s): %s — повтор через %.2fс",
                    attempt,
                    self._max_attempts,
                    method,
                    path,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        # Сюда попадаем только если цикл завершился без return и без raise —
        # теоретически невозможно, но mypy требует явного пути возврата.
        assert last_exc is not None
        raise last_exc

    async def get_current_user(self) -> User:
        """Получить текущего пользователя (того чей API-key используется)
        удобный метод для проверки работоспособности клиента и валидности ключа
        endpoint: GET /users/current.json"""
        data = await self._request("GET", "/users/current.json")
        return User.model_validate(data["user"])

    async def get_issue(
        self,
        issue_id: int,
        *,
        include: Sequence[str] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Issue:
        """Получить задачу по id.

        Args:
            issue_id: ID Задачи в Redmine
            include: Дополнительные связанные данные. Допустимые значения
            (по докам Redmine): "children", "attachments", "relations",
            "changesets", "journals", "watchers", "allowed_statuses".
            Пример: include=["journals", "attachments"].

        Returns:
            Issue с заполненными полями. journals будет пустым списком,
            если include не запрашивал их.

        Endpoint: GET /issues/{id}.json
        """
        params: dict[str, Any] = {}
        if include:
            params["include"] = ",".join(include)
        data = await self._request(
            "GET", f"/issues/{issue_id}.json", params=params, timeout=timeout
        )
        return Issue.model_validate(data["issue"])

    async def get_journals(self, issue_id: int) -> list[Journal]:
        """Получить журналы (историю изменений и комментарии) задачи.
        Удобный shortcut: эквивалентно get_issue(id, include=["journals"]).journals.

        Args:
            issue_id: ID задачи.

        Returns:
            Список записей журнала. Пустой, если изменений не было.
        """
        issue = await self.get_issue(issue_id, include=["journals"])
        return issue.journals

    async def list_issues(
        self,
        *,
        include: Sequence[str] | None = None,
        page_size: int = 100,
        timeout: float | httpx.Timeout | None = None,
        **filters: Any,
    ) -> AsyncIterator[Issue]:
        """Итерировать задачи Redmine с автоматической пагинацией.
        Прозрачно проходит все страницы — пользователю не нужно думать
        о limit/offset/total_count. Используется как async-generator:
            async for issue in client.list_issues(project_id=42, status_id="open"):
            print(issue.subject)
         Args:
            include: Связанные данные (см. get_issue).
            page_size: Размер страницы (1-100, по умолчанию 100 — максимум Redmine).
            **filters: Произвольные фильтры Redmine. Часто используемые:
                - project_id: int — фильтр по проекту
                - status_id: "open" | "closed" | "*" | int — фильтр по статусу
                - assigned_to_id: int | "me" — фильтр по исполнителю
                - tracker_id: int — фильтр по трекеру
                - sort: str — сортировка, например "updated_on:desc"
                - created_on, updated_on: str — фильтры по датам, например
                ">=2024-01-01" или "><2024-01-01|2024-12-31"
                Полный список: https://www.redmine.org/projects/redmine/wiki/Rest_Issues
            Yields:
            Issue по одному. Если задач нет — генератор завершится сразу.
            Endpoint: GET /issues.json
        """
        if not 1 <= page_size <= 100:
            raise ValueError(f"page_size must be in [1, 100], got {page_size}")
        offset = 0
        while True:
            params: dict[str, Any] = {
                **filters,
                "limit": page_size,
                "offset": offset,
            }
            if include:
                params["include"] = ",".join(include)

            data = await self._request(
                "GET",
                "/issues.json",
                params=params,
                timeout=timeout,
            )

            issues_data: list[dict[str, Any]] = data["issues"]
            total_count: int = data["total_count"]

            if not issues_data:
                return

            for issue_data in issues_data:
                yield Issue.model_validate(issue_data)

            # Условие выхода: получили меньше, чем просили = это последняя страница
            offset += len(issues_data)
            if offset >= total_count:
                return

    async def list_issue_statuses(
        self,
        *,
        timeout: float | httpx.Timeout | None = None,
    ) -> list[Status]:
        """Получить полный список статусов задач Redmine.

        Статусов обычно ~5-15 штук — одна страница, никакой пагинации.
        Метод предназначен для резолвера "id статуса → name" в поллере:
        journal-запись Redmine отдаёт только id старого/нового статуса
        (в details.old_value / details.new_value), а в шаблоне сообщения
        нужно человекочитаемое имя ("В работе" вместо "2").

        Возвращаемый список свежий на момент запроса — кэширование
        поверх этого метода делает StatusResolver (этап 7c).

        Endpoint: GET /issue_statuses.json

        Returns:
            Список Status с полями id, name, is_closed.
        """
        data = await self._request("GET", "/issue_statuses.json", timeout=timeout)
        statuses_data: list[dict[str, Any]] = data["issue_statuses"]
        return [Status.model_validate(s) for s in statuses_data]

    async def list_issue_priorities(
        self,
        *,
        timeout: float | httpx.Timeout | None = None,
    ) -> list[Priority]:
        """Получить список приоритетов задач Redmine (enumeration).

        Обычно ~5 штук — одна страница, пагинации нет. Нужен резолверу
        «id приоритета → name»: journal-запись отдаёт priority_id строкой
        (details.old_value / details.new_value), а в сообщении нужно имя
        ("Высокий" вместо "3").

        Кэширование поверх — NameResolver, как у статусов.

        Endpoint: GET /enumerations/issue_priorities.json

        Returns:
            Список Priority с полями id, name.
        """
        data = await self._request(
            "GET", "/enumerations/issue_priorities.json", timeout=timeout
        )
        priorities_data: list[dict[str, Any]] = data["issue_priorities"]
        return [Priority.model_validate(p) for p in priorities_data]
