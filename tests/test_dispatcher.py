"""Тесты диспетчера отправки и идемпотентности.

Рендерер тут настоящий, не мок: шаблоны Jinja — часть контракта, и если
событие не рендерится, диспетчер обязан упасть в тесте, а не в проде.
Мокается только HTTP к MAX (pytest-httpx) и БД (in-memory SQLite).

Форма ответа MAX — {"message": {"body": {...}}} — взята из tests/maxbot,
где она подтверждена живым API (якорь 4.12: мок-фикстуры пишем по
реальному ответу).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime

import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.dispatcher import dispatch_events
from redmine_max_notifier.events.models import (
    DueDateApproachingEvent,
    Event,
    IssueUpdatedEvent,
    NameChange,
    NewIssueEvent,
)
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.redmine.models import Issue, NamedRef
from redmine_max_notifier.renderer import MessageRenderer
from redmine_max_notifier.routing import add_route
from redmine_max_notifier.sent_notifications import is_already_sent, mark_sent
from redmine_max_notifier.user_mapping import add_mapping

PROJECT_ID = 5
CHAT_ID = -1001
ASSIGNEE_ID = 10  # id исполнителя в Redmine
OCCURRED_AT = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

# Ответ MAX на POST /messages.
SENT_MESSAGE_PAYLOAD = {"message": {"body": {"mid": "msg-1", "seq": 1, "text": "ok"}}}


def _issue(issue_id: int = 101) -> Issue:
    return Issue(
        id=issue_id,
        project=NamedRef(id=PROJECT_ID, name="D-TELEKOM Infra"),
        tracker=NamedRef(id=1, name="Bug"),
        status=NamedRef(id=1, name="Новая"),
        priority=NamedRef(id=2, name="Normal"),
        author=NamedRef(id=42, name="Leo Test"),
        assigned_to=NamedRef(id=ASSIGNEE_ID, name="Максим Мерзляков"),
        subject="DHCP conflict on redmine host",
        created_on=OCCURRED_AT,
        updated_on=OCCURRED_AT,
    )


def _new_issue_event(issue_id: int = 101) -> NewIssueEvent:
    return NewIssueEvent(occurred_at=OCCURRED_AT, issue=_issue(issue_id))


def _issue_with_due_date(issue_id: int = 102) -> Issue:
    """Задача со сроком — для событий о дедлайне (шаблон зовёт
    due_date.strftime, без срока упадёт)."""
    return _issue(issue_id).model_copy(update={"due_date": date(2026, 7, 17)})


def _comment_event(journal_id: int = 501) -> IssueUpdatedEvent:
    """Обновление задачи с комментарием — для базовых тестов доставки."""
    return IssueUpdatedEvent(
        occurred_at=OCCURRED_AT,
        issue=_issue(),
        journal_id=journal_id,
        author=NamedRef(id=42, name="Leo Test"),
        notes="Починил через nmcli.",
    )


def _status_event(journal_id: int = 502) -> IssueUpdatedEvent:
    """Обновление задачи со сменой статуса — другой journal_id, другой факт."""
    return IssueUpdatedEvent(
        occurred_at=OCCURRED_AT,
        issue=_issue(),
        journal_id=journal_id,
        author=NamedRef(id=42, name="Leo Test"),
        status_change=NameChange(old="В работе", new="Решена"),
    )


@pytest.fixture
def renderer() -> MessageRenderer:
    return MessageRenderer(redmine_base_url="http://redmine.test.local")


@pytest.fixture
def mock_max_ok(httpx_mock: HTTPXMock, max_base_url: str) -> HTTPXMock:
    """MAX принимает любое сообщение."""
    httpx_mock.add_response(
        method="POST",
        json=SENT_MESSAGE_PAYLOAD,
        status_code=200,
        is_reusable=True,
    )
    return httpx_mock


async def _dispatch(
    events: list[Event],
    session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
) -> int:
    return await dispatch_events(
        events,
        session=session,
        renderer=renderer,
        max_client=max_client,
    )


async def test_event_is_sent_and_marked(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    mock_max_ok: HTTPXMock,
) -> None:
    """Базовый путь: событие рендерится, уходит в чат проекта и
    отмечается отправленным."""
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
    event = _comment_event()

    sent = await _dispatch([event], db_session, renderer, max_client)

    assert sent == 1
    requests = mock_max_ok.get_requests()
    assert len(requests) == 1
    assert requests[0].url.params["chat_id"] == str(CHAT_ID)
    assert await is_already_sent(db_session, event) is True


async def test_already_sent_event_is_not_resent(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Событие с отметкой в БД в MAX не уходит.

    Моки MAX намеренно не регистрируем: любой HTTP-запрос здесь —
    это провал теста, и pytest-httpx его покажет.
    """
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
    event = _comment_event()
    await mark_sent(db_session, event)

    sent = await _dispatch([event], db_session, renderer, max_client)

    assert sent == 0
    assert httpx_mock.get_requests() == []


async def test_new_issue_dedup_works_despite_null_journal_id(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    mock_max_ok: HTTPXMock,
) -> None:
    """Якорь 4.10: у new_issue journal_id = NULL, а NULL != NULL в SQL,
    поэтому UNIQUE-констрейнт дубли НЕ ловит. Дедуп держится
    исключительно на явном SELECT в is_already_sent — этот тест его
    и проверяет.
    """
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
    event = _new_issue_event()

    first = await _dispatch([event], db_session, renderer, max_client)
    second = await _dispatch([event], db_session, renderer, max_client)

    assert first == 1
    assert second == 0
    # Ровно одно сообщение в чат, несмотря на два прохода.
    assert len(mock_max_ok.get_requests()) == 1


async def test_events_without_routing_are_skipped_with_warning(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """У проекта нет чатов — слать некуда.

    Отметку при этом НЕ ставим: routing могут прописать позже, и тогда
    событие имеет право уехать. Молчать тоже нельзя — это почти всегда
    забытый админом routing для нового проекта.
    """
    event = _comment_event()

    with caplog.at_level(logging.WARNING):
        sent = await _dispatch([event], db_session, renderer, max_client)

    assert sent == 0
    assert httpx_mock.get_requests() == []
    assert "routing не настроен" in caplog.text
    assert await is_already_sent(db_session, event) is False


async def test_failed_delivery_is_not_marked(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MAX ответил 500 — отметку не ставим, событие повторится.

    Это и есть at-least-once: лучше дубль, чем молча потерянное
    уведомление.
    """
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
    httpx_mock.add_response(method="POST", status_code=500, is_reusable=True)
    event = _status_event()

    with caplog.at_level(logging.ERROR):
        sent = await _dispatch([event], db_session, renderer, max_client)

    assert sent == 0
    assert await is_already_sent(db_session, event) is False
    assert "не удалось отправить" in caplog.text


async def test_one_failing_chat_does_not_block_others(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    max_base_url: str,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Бота выкинули из одного чата — остальные чаты проекта уведомление
    всё равно получают, а событие считается доставленным."""
    other_chat = -1002
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
    await add_route(db_session, project_id=PROJECT_ID, chat_id=other_chat)

    # Первый чат отвечает 404 (чат удалён), второй — успехом.
    httpx_mock.add_response(
        method="POST",
        url=f"{max_base_url}/messages?chat_id={CHAT_ID}",
        status_code=404,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{max_base_url}/messages?chat_id={other_chat}",
        json=SENT_MESSAGE_PAYLOAD,
        status_code=200,
        is_reusable=True,
    )
    event = _comment_event()

    with caplog.at_level(logging.ERROR):
        sent = await _dispatch([event], db_session, renderer, max_client)

    assert sent == 1
    assert await is_already_sent(db_session, event) is True
    assert f"в чат {CHAT_ID}" in caplog.text


async def test_assignee_is_mentioned(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    mock_max_ok: HTTPXMock,
) -> None:
    """Исполнитель сопоставлен с MAX — в сообщение уходит @упоминание.

    Формат ссылки проверен на живом MAX: `@username` подсвечивается
    только у ботов, у живых людей поля username нет.
    """
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
    await add_mapping(
        db_session,
        redmine_user_id=ASSIGNEE_ID,
        max_user_id=252123521,
        max_name="Leonid",
    )

    sent = await _dispatch([_new_issue_event()], db_session, renderer, max_client)

    assert sent == 1
    body = json.loads(mock_max_ok.get_requests()[0].content)
    assert "[Leonid](max://user/252123521)" in body["text"]


async def test_many_mentions_for_one_assignee(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    mock_max_ok: HTTPXMock,
) -> None:
    """Один Redmine-исполнитель может дёргать нескольких в MAX
    (например, самого исполнителя и его тимлида)."""
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
    await add_mapping(
        db_session, redmine_user_id=ASSIGNEE_ID, max_user_id=111, max_name="Петя"
    )
    await add_mapping(
        db_session, redmine_user_id=ASSIGNEE_ID, max_user_id=222, max_name="Вася"
    )

    await _dispatch([_new_issue_event()], db_session, renderer, max_client)

    text = json.loads(mock_max_ok.get_requests()[0].content)["text"]
    assert "[Петя](max://user/111)" in text
    assert "[Вася](max://user/222)" in text


async def test_due_date_reminder_mentions_assignee(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    mock_max_ok: HTTPXMock,
) -> None:
    """Напоминание о дедлайне тоже пингует исполнителя.

    Здесь пинг ценнее всего: смысл напоминания в том, чтобы человек
    его увидел, а не пролистал в общем потоке.
    """
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
    await add_mapping(
        db_session,
        redmine_user_id=ASSIGNEE_ID,
        max_user_id=252123521,
        max_name="Leonid",
    )
    event = DueDateApproachingEvent(
        occurred_at=OCCURRED_AT,
        issue=_issue_with_due_date(),
        days_before=2,
    )

    sent = await _dispatch([event], db_session, renderer, max_client)

    assert sent == 1
    text = json.loads(mock_max_ok.get_requests()[0].content)["text"]
    assert "[Leonid](max://user/252123521)" in text


async def test_unmapped_assignee_sends_without_mention(
    db_session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
    mock_max_ok: HTTPXMock,
) -> None:
    """Исполнителя не сопоставили — уведомление всё равно уходит.

    Подрядчик, уволившийся, робот-учётка: отсутствие маппинга не повод
    лишать чат уведомления.
    """
    await add_route(db_session, project_id=PROJECT_ID, chat_id=CHAT_ID)

    sent = await _dispatch([_new_issue_event()], db_session, renderer, max_client)

    assert sent == 1
    text = json.loads(mock_max_ok.get_requests()[0].content)["text"]
    assert "max://user/" not in text


async def test_mark_is_committed_per_event(
    db_session_factory: async_sessionmaker[AsyncSession],
    renderer: MessageRenderer,
    max_client: MaxClient,
    mock_max_ok: HTTPXMock,
) -> None:
    """Отметка коммитится сразу, а не копится до конца батча.

    Проверяем через ВТОРУЮ сессию: она видит только закоммиченное.
    Если бы диспетчер откладывал commit, упавший на середине цикл
    переотправил бы уже доставленные сообщения.
    """
    async with db_session_factory() as setup_session:
        await add_route(setup_session, project_id=PROJECT_ID, chat_id=CHAT_ID)
        await setup_session.commit()

    events: list[Event] = [_comment_event(501), _status_event(502)]

    async with db_session_factory() as session:
        sent = await dispatch_events(
            events,
            session=session,
            renderer=renderer,
            max_client=max_client,
        )

    assert sent == 2

    async with db_session_factory() as fresh_session:
        for event in events:
            assert await is_already_sent(fresh_session, event) is True
