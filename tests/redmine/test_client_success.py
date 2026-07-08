"""Тесты успешных путей публичных методов RedmineClient.

Каждый метод проверяем на:
- правильный HTTP-метод и URL уходят на сервер;
- правильные заголовки (в частности, X-Redmine-API-Key);
- корректный разбор JSON в Pydantic-модели.
"""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.redmine.models import User
from tests.conftest import TEST_API_KEY, load_fixture


async def test_get_current_user_success(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /users/current.json → User с корректно распарсенными полями."""
    # Готовим мок: на любой запрос к нашему URL отдаём заранее прочитанный JSON.
    payload = load_fixture("user_current.json")
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/users/current.json",
        json=payload,
        status_code=200,
    )

    # Выполняем метод клиента.
    user = await client.get_current_user()

    # Проверяем что метод вернул модель нужного типа с нужными полями.
    assert isinstance(user, User)
    assert user.id == 42
    assert user.login == "leo"
    assert user.admin is True
    assert user.mail == "leo@example.com"

    # Отдельно проверяем что клиент ушёл с правильным API-ключом в заголовке.
    # httpx_mock.get_requests() — список всех перехваченных запросов.
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].headers["X-Redmine-API-Key"] == TEST_API_KEY
    assert requests[0].headers["Accept"] == "application/json"


async def test_get_issue_success(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /issues/{id}.json → Issue со всеми базовыми полями."""
    payload = load_fixture("issue_single.json")
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues/101.json",
        json=payload,
        status_code=200,
    )

    issue = await client.get_issue(101)

    assert issue.id == 101
    assert issue.subject == "DHCP conflict on redmine host"
    assert issue.status.name == "In Progress"
    assert issue.project.id == 5
    assert issue.assigned_to is not None
    assert issue.assigned_to.id == 42
    assert issue.done_ratio == 30
    # journals не запрашивали → должен быть пустой список, а не None
    assert issue.journals == []
    # custom_fields распарсились
    assert len(issue.custom_fields) == 1
    assert issue.custom_fields[0].value == "prod"


async def test_get_issue_with_include_passes_query_param(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """include=[...] должен уйти в запрос как ?include=journals,attachments."""
    payload = load_fixture("issue_with_journals.json")
    httpx_mock.add_response(
        method="GET",
        # match_query позволяет проверить, что клиент реально отправил нужные параметры.
        # Если параметры не совпадут — тест упадёт с "no response matched".
        url=f"{base_url}/issues/101.json?include=journals%2Cattachments",
        json=payload,
        status_code=200,
    )

    issue = await client.get_issue(101, include=["journals", "attachments"])

    assert issue.id == 101
    assert len(issue.journals) == 2


async def test_get_journals_returns_list(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """get_journals — shortcut через get_issue(include=[journals])."""
    payload = load_fixture("issue_with_journals.json")
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues/101.json?include=journals",
        json=payload,
        status_code=200,
    )

    journals = await client.get_journals(101)

    assert len(journals) == 2
    assert journals[0].id == 555
    assert journals[0].notes is not None
    assert "netplan" in journals[0].notes
    # У второй записи — details с изменением статуса
    assert journals[1].details[0].name == "status_id"
    assert journals[1].details[0].new_value == "3"


async def test_list_issues_paginates_across_pages(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """list_issues прозрачно проходит все страницы и отдаёт все задачи."""
    page_1 = load_fixture("issues_page_1.json")
    page_2 = load_fixture("issues_page_2.json")

    # Регистрируем два ответа по порядку: pytest-httpx отдаёт их в очерёдности.
    # Первая страница: limit=2, offset=0
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues.json?limit=2&offset=0",
        json=page_1,
        status_code=200,
    )
    # Вторая страница: limit=2, offset=2
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues.json?limit=2&offset=2",
        json=page_2,
        status_code=200,
    )

    collected = [issue async for issue in client.list_issues(page_size=2)]

    assert [i.id for i in collected] == [201, 202, 203]
    # Именно две страницы — не больше, не меньше
    assert len(httpx_mock.get_requests()) == 2


async def test_list_issues_empty_response(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Если Redmine отдал пустой список — генератор молча завершается."""
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues.json?limit=100&offset=0",
        json={"issues": [], "total_count": 0, "offset": 0, "limit": 100},
        status_code=200,
    )

    collected = [issue async for issue in client.list_issues()]

    assert collected == []
    # Ровно один запрос — на пустой ответ повторного похода быть не должно
    assert len(httpx_mock.get_requests()) == 1


async def test_list_issues_early_break_stops_pagination(
    client: RedmineClient,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Если потребитель прервал итерацию через break — следующая страница
    не должна запрашиваться."""
    page_1 = load_fixture("issues_page_1.json")

    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issues.json?limit=2&offset=0",
        json=page_1,
        status_code=200,
    )
    # Заметь: вторую страницу НЕ регистрируем. Если клиент за ней полезет —
    # pytest-httpx поднимет ошибку "no response for request".

    first_id: int | None = None
    async for issue in client.list_issues(page_size=2):
        first_id = issue.id
        break  # прерываем после первой же задачи

    assert first_id == 201
    # Ровно один запрос — потому что break произошёл внутри первой страницы,
    # до того как генератор пошёл за второй.
    assert len(httpx_mock.get_requests()) == 1
