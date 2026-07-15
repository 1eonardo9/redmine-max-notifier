"""Тесты фоновых циклов (run_poll_cycle, run_due_date_cycle) и курсора в БД.

Это самый «интеграционный» уровень юнит-тестов проекта: настоящие БД
(in-memory SQLite), рендерер и все три модуля 7c-7e, замокан только
HTTP к Redmine и MAX. Именно здесь ловятся ошибки стыковки, которых
не видно, пока каждый кусок тестируется отдельно.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.jobs import JobDeps, run_due_date_cycle, run_poll_cycle
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

# Бизнес-таймзона тестов. Намеренно НЕ таймзона машины, на которой
# гоняются тесты: job считает "сегодня" именно в ней, и тест обязан
# проверять это, а не совпадение с локалью разработчика.
TEST_TZ = ZoneInfo("Europe/Moscow")

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
) -> JobDeps:
    return JobDeps(
        client=client,
        resolver=StatusResolver(client, ttl=timedelta(hours=1)),
        renderer=MessageRenderer(redmine_base_url="http://redmine.test.local"),
        max_client=max_client,
        session_factory=db_session_factory,
        lookback=timedelta(minutes=5),
        due_date_threshold_days=3,
        tz=TEST_TZ,
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
    deps: JobDeps,
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
    deps: JobDeps,
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


# ── Ежедневная проверка дедлайнов ────────────────────────────────────────


def _issue_with_due_date(issue_id: int, due_date: str) -> dict[str, Any]:
    payload = _issue_payload(
        issue_id,
        created_on="2026-07-01T10:00:00Z",
        updated_on="2026-07-01T10:00:00Z",
    )
    payload["due_date"] = due_date
    return payload


def _local_today() -> date:
    """Дата в бизнес-таймзоне — та же, которой оперирует job."""
    return datetime.now(TEST_TZ).date()


async def test_due_date_reminder_is_sent_once(
    deps: JobDeps,
    httpx_mock: HTTPXMock,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Напоминание уходит один раз за жизнь задачи.

    Ключ дедупликации (due_date_approaching, issue_id, NULL) не содержит
    даты — поэтому второй прогон в тот же/следующий день промолчит.
    Без этого сервис долбил бы напоминанием каждое утро до самого срока.
    """
    async with db_session_factory() as session:
        await add_route(session, project_id=PROJECT_ID, chat_id=CHAT_ID)
        await session.commit()

    due = (_local_today() + timedelta(days=2)).isoformat()
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/issues\.json.*"),
        json={
            "issues": [_issue_with_due_date(301, due)],
            "total_count": 1,
            "offset": 0,
            "limit": 100,
        },
        status_code=200,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST",
        json=SENT_MESSAGE_PAYLOAD,
        status_code=200,
        is_reusable=True,
    )

    await run_due_date_cycle(deps)
    await run_due_date_cycle(deps)

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 1


async def test_moved_due_date_triggers_new_reminder(
    deps: JobDeps,
    httpx_mock: HTTPXMock,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Срок передвинули — про новый срок напоминаем заново.

    Ключ дедупа включает notified_due_date, поэтому (задача, срок №1)
    и (задача, срок №2) — разные факты. Без этой колонки чем важнее
    задача (её и двигают), тем вероятнее сервис бы про неё замолчал
    навсегда.
    """
    async with db_session_factory() as session:
        await add_route(session, project_id=PROJECT_ID, chat_id=CHAT_ID)
        await session.commit()

    httpx_mock.add_response(
        method="POST",
        json=SENT_MESSAGE_PAYLOAD,
        status_code=200,
        is_reusable=True,
    )

    first_due = (_local_today() + timedelta(days=2)).isoformat()
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/issues\.json.*"),
        json={
            "issues": [_issue_with_due_date(303, first_due)],
            "total_count": 1,
            "offset": 0,
            "limit": 100,
        },
        status_code=200,
    )

    await run_due_date_cycle(deps)

    # Срок сдвинули на неделю вперёд — и он снова попал в порог.
    moved_due = (_local_today() + timedelta(days=1)).isoformat()
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/issues\.json.*"),
        json={
            "issues": [_issue_with_due_date(303, moved_due)],
            "total_count": 1,
            "offset": 0,
            "limit": 100,
        },
        status_code=200,
    )

    await run_due_date_cycle(deps)

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 2, "про новый срок должно прийти отдельное напоминание"


async def test_due_date_query_asks_only_open_issues_within_threshold(
    deps: JobDeps,
    httpx_mock: HTTPXMock,
) -> None:
    """Запрос к Redmine: только открытые задачи и срок не дальше порога.

    status_id=open — закрытая задача о дедлайне напоминать не должна.
    Фильтр `<=`, а не диапазон: просроченные тоже забираем, шаблон
    умеет days_before < 0, а от вечных напоминаний спасает дедуп.
    """
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/issues\.json.*"),
        json={"issues": [], "total_count": 0, "offset": 0, "limit": 100},
        status_code=200,
        is_reusable=True,
    )

    await run_due_date_cycle(deps)

    params = httpx_mock.get_requests()[0].url.params
    assert params["status_id"] == "open"
    expected = (_local_today() + timedelta(days=3)).isoformat()
    assert params["due_date"] == f"<={expected}"


async def test_overdue_issue_gets_negative_days_before(
    deps: JobDeps,
    httpx_mock: HTTPXMock,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Просроченная задача даёт days_before < 0 и рендерится шаблоном
    как «Задача просрочена» — проверяем по тексту, ушедшему в MAX."""
    async with db_session_factory() as session:
        await add_route(session, project_id=PROJECT_ID, chat_id=CHAT_ID)
        await session.commit()

    due = (_local_today() - timedelta(days=5)).isoformat()
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/issues\.json.*"),
        json={
            "issues": [_issue_with_due_date(302, due)],
            "total_count": 1,
            "offset": 0,
            "limit": 100,
        },
        status_code=200,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST",
        json=SENT_MESSAGE_PAYLOAD,
        status_code=200,
        is_reusable=True,
    )

    await run_due_date_cycle(deps)

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert "просрочена" in body["text"]
    assert "5 дн." in body["text"]


async def test_due_date_cycle_survives_redmine_failure(
    deps: JobDeps,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Redmine лежит — суточный job не падает, а логирует."""
    httpx_mock.add_response(status_code=500, is_reusable=True)

    with caplog.at_level(logging.ERROR):
        await run_due_date_cycle(deps)

    assert "проверка дедлайнов завершилась ошибкой" in caplog.text
