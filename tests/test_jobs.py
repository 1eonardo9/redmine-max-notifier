"""Тесты цикла поллинга (run_poll_cycle) и курсора в БД.

Это самый «интеграционный» уровень юнит-тестов проекта: настоящие БД
(in-memory SQLite), рендерер и все три модуля 7c-7e, замокан только
HTTP к Redmine и MAX. Именно здесь ловятся ошибки стыковки, которых
не видно, пока каждый кусок тестируется отдельно.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.jobs import PollerDeps, run_poll_cycle
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.poller import PollCursor
from redmine_max_notifier.polling_state import load_cursor, save_cursor
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.renderer import MessageRenderer
from redmine_max_notifier.routing import add_route
from redmine_max_notifier.status_resolver import StatusResolver
from tests.conftest import load_fixture

PROJECT_ID = 5
CHAT_ID = -1001

SENT_MESSAGE_PAYLOAD = {"message": {"body": {"mid": "msg-1", "seq": 1, "text": "ok"}}}


def _issue_payload(
    issue_id: int,
    *,
    created_on: str,
    updated_on: str,
    journals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "project": {"id": PROJECT_ID, "name": "D-TELEKOM Infra"},
        "tracker": {"id": 1, "name": "Bug"},
        "status": {"id": 1, "name": "Новая"},
        "priority": {"id": 2, "name": "Normal"},
        "author": {"id": 42, "name": "Leo Test"},
        "subject": f"issue-{issue_id}",
        "created_on": created_on,
        "updated_on": updated_on,
        "journals": journals or [],
    }


def _mock_redmine_issues(
    httpx_mock: HTTPXMock,
    base_url: str,
    issues: list[dict[str, Any]],
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=load_fixture("issue_statuses.json"),
        status_code=200,
        is_optional=True,
        is_reusable=True,
    )
    # Query-строка запроса содержит текущее время, поэтому матчим
    # регуляркой по пути, а не точным URL.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/issues\.json.*"),
        json={
            "issues": issues,
            "total_count": len(issues),
            "offset": 0,
            "limit": 100,
        },
        status_code=200,
        is_reusable=True,
    )


@pytest.fixture
def deps(
    client: RedmineClient,
    max_client: MaxClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> PollerDeps:
    return PollerDeps(
        client=client,
        resolver=StatusResolver(client, ttl=timedelta(hours=1)),
        renderer=MessageRenderer(redmine_base_url="http://redmine.test.local"),
        max_client=max_client,
        session_factory=db_session_factory,
        lookback=timedelta(minutes=5),
    )


async def test_cursor_roundtrip_through_db(db_session: AsyncSession) -> None:
    """Курсор переживает запись и чтение, время остаётся aware."""
    now = datetime.now(UTC)
    await save_cursor(
        db_session,
        PollCursor(
            last_seen_issue_id=201,
            last_seen_journal_id=501,
            last_check_at=now,
        ),
    )
    await db_session.commit()

    loaded = await load_cursor(db_session)

    assert loaded.last_seen_issue_id == 201
    assert loaded.last_seen_journal_id == 501
    assert loaded.last_check_at is not None
    # Ключевая проверка: SQLite не хранит смещение, и без нормализации
    # в load_cursor отсюда приехал бы naive datetime, который взорвался
    # бы при сравнении с aware created_on из Redmine.
    assert loaded.last_check_at.tzinfo is not None


async def test_load_cursor_on_empty_db_is_cold_start(
    db_session: AsyncSession,
) -> None:
    """Строки состояния ещё нет — это холодный старт, а не ошибка."""
    cursor = await load_cursor(db_session)

    assert cursor.is_cold_start is True
    assert cursor.last_seen_issue_id is None


async def test_first_cycle_sends_nothing_and_second_sends(
    deps: PollerDeps,
    base_url: str,
    httpx_mock: HTTPXMock,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Главный сценарий этапа: первый цикл ставит baseline молча,
    второй отправляет только то, что появилось после него.

    Именно эта склейка ловит ошибки, которых не видно в отдельных
    тестах poller'а и dispatcher'а.
    """
    async with db_session_factory() as session:
        await add_route(session, project_id=PROJECT_ID, chat_id=CHAT_ID)
        await session.commit()

    now = datetime.now(UTC)
    old = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Первый цикл: в Redmine есть задача 201.
    _mock_redmine_issues(
        httpx_mock,
        base_url,
        [_issue_payload(201, created_on=old, updated_on=old)],
    )
    httpx_mock.add_response(
        method="POST",
        json=SENT_MESSAGE_PAYLOAD,
        status_code=200,
        is_reusable=True,
    )

    await run_poll_cycle(deps)

    # Ни одного сообщения: холодный старт только запоминает baseline.
    assert [r for r in httpx_mock.get_requests() if r.method == "POST"] == []

    async with db_session_factory() as session:
        cursor = await load_cursor(session)
    assert cursor.last_seen_issue_id == 201
    assert cursor.is_cold_start is False

    # Второй цикл: появилась задача 202.
    httpx_mock.reset()
    _mock_redmine_issues(
        httpx_mock,
        base_url,
        [
            _issue_payload(201, created_on=old, updated_on=old),
            _issue_payload(202, created_on=old, updated_on=old),
        ],
    )
    httpx_mock.add_response(
        method="POST",
        json=SENT_MESSAGE_PAYLOAD,
        status_code=200,
        is_reusable=True,
    )

    await run_poll_cycle(deps)

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 1  # только про 202, задача 201 уже виденная
    assert posts[0].url.params["chat_id"] == str(CHAT_ID)


async def test_cycle_survives_redmine_failure(
    deps: PollerDeps,
    httpx_mock: HTTPXMock,
    db_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Redmine лежит — job не падает, курсор не сдвигается.

    Исключение наружу означало бы «Job raised an exception» в логе
    APScheduler без контекста. А несдвинутый курсор гарантирует,
    что пропущенное будет подобрано следующим тиком.
    """
    async with db_session_factory() as session:
        await save_cursor(
            session,
            PollCursor(
                last_seen_issue_id=200,
                last_seen_journal_id=500,
                last_check_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
        )
        await session.commit()

    httpx_mock.add_response(status_code=500, is_reusable=True)

    with caplog.at_level(logging.ERROR):
        await run_poll_cycle(deps)  # не должно бросить

    assert "цикл поллинга завершился ошибкой" in caplog.text

    async with db_session_factory() as session:
        cursor = await load_cursor(session)
    assert cursor.last_seen_issue_id == 200
