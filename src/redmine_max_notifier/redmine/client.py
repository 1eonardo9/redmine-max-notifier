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
from redmine_max_notifier.redmine.models import Issue, Journal, User


class RedmineClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
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

    async def __aenter__(self) -> "RedmineClient":
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
    ) -> dict[str, Any]:
        """Выполнить HTTP-запрос к Redmine и вернуть распарсенный JSON.

        Транспортные ошибки (сеть, таймаут, TLS) оборачиваются в
        RedmineTransportError. Ошибки уровня API (4xx/5xx) — в конкретный
        подкласс RedmineAPIError по коду статуса.
        """
        # --- 1. Сам HTTP-вызов: транспортные ошибки httpx оборачиваем в наши. ---
        try:
            response = await self._client.request(
                method=method,
                url=path,
                params=params,
                json=json_body,
            )
        except httpx.TimeoutException as exc:
            # Таймаут — специфичнее TransportError, ловим первым.
            raise RedmineTransportError(
                f"таймаут при запросе {method} {path}",
                url=path,
            ) from exc
        except httpx.TransportError as exc:
            # DNS, обрыв соединения, TLS-ошибка, отказ подключения и т.п.
            raise RedmineTransportError(
                f"сетевая ошибка при запросе {method} {path}: {exc}",
                url=path,
            ) from exc

        # --- 2. Успех: сразу возвращаем распарсенный JSON. ---
        if 200 <= response.status_code < 300:
            data: dict[str, Any] = response.json()
            return data

        # --- 3. Ошибка API: собираем контекст, маппим статус на нужное исключение. ---
        # response.text безопасен: тело сохраняется в исключении в обрезанном виде
        # (см. _truncate в exceptions.py), а X-Redmine-API-Key в тело не попадает.
        body = response.text
        status = response.status_code

        # 422 — пытаемся вытащить список errors из JSON-тела Redmine.
        if status == 422:
            errors: list[str] | None = None
            try:
                body_json = response.json()
                if isinstance(body_json, dict):
                    raw_errors = body_json.get("errors")
                    if isinstance(raw_errors, list):
                        errors = [str(e) for e in raw_errors]
            except ValueError:
                # Тело не JSON — оставим errors как None, body уже сохранён.
                pass
            raise RedmineValidationError(
                f"валидация не пройдена ({method} {path}): {errors or body}",
                status_code=status,
                url=path,
                response_body=body,
                errors=errors,
            )

        # Остальные коды — маппинг на конкретные классы.
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

        # Всё остальное (неожиданные 4xx: 400, 405, 409 и т.д.) — общий APIError.
        raise RedmineAPIError(
            f"неожиданный статус {status} ({method} {path})",
            status_code=status,
            url=path,
            response_body=body,
        )

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

        data = await self._request("GET", f"/issues/{issue_id}.json", params=params)
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

            data = await self._request("GET", "/issues.json", params=params)

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
