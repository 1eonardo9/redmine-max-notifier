"""Тесты детектора изменений (poll_recent_changes).

Форма мок-ответов Redmine скопирована с реальных фикстур
(issues_page_1.json, issue_with_journals.json), а не выдумана — якорь 4.12.
Билдеры ниже нужны только чтобы варьировать id и даты, структуру
они не меняют.

ВАЖНО про журналы. Redmine отдаёт include=journals только для одиночной
задачи. В списке (/issues.json) параметр молча игнорируется — поля
journals в ответе просто нет. Моки это воспроизводят: _mock_issue_list
отдаёт задачи БЕЗ журналов, журналы живут только в ответах
/issues/{id}.json. Раньше тесты клали журналы прямо в список — и
подтверждали догадку вместо реальности: детектор был нерабочим
наполовину, а прогон был зелёным (поймано smoke'ом на 7h).

Мокаются три эндпоинта: /issues.json (список), /issues/{id}.json
(карточка с журналами) и /issue_statuses.json (резолвер).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from redmine_max_notifier.events.models import (
    CommentAddedEvent,
    NewIssueEvent,
    StatusChangedEvent,
)
from redmine_max_notifier.poller import PollCursor, poll_recent_changes
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.status_resolver import StatusResolver
from tests.conftest import load_fixture

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
LOOKBACK = timedelta(minutes=5)

# Курсор "мы уже работали": last_check_at минуту назад, максимумы id есть.
WARM_CURSOR = PollCursor(
    last_seen_issue_id=200,
    last_seen_journal_id=500,
    last_check_at=NOW - timedelta(minutes=1),
)


def _dt(minutes_ago: int) -> str:
    """Отметка времени за N минут до NOW в формате Redmine."""
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _journal(
    journal_id: int,
    *,
    created_on: str,
    notes: str | None = None,
    private_notes: bool = False,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": journal_id,
        "user": {"id": 42, "name": "Leo Test"},
        "notes": notes,
        "created_on": created_on,
        "private_notes": private_notes,
        "details": details or [],
    }


def _status_detail(old: str | None, new: str | None) -> dict[str, Any]:
    return {
        "property": "attr",
        "name": "status_id",
        "old_value": old,
        "new_value": new,
    }


def _issue(
    issue_id: int,
    *,
    created_on: str,
    updated_on: str,
    journals: list[dict[str, Any]] | None = None,
    is_private: bool = False,
) -> dict[str, Any]:
    """Карточка задачи.

    journals кладутся сюда только для ответа /issues/{id}.json —
    в списке их не бывает, см. _mock_redmine.
    """
    return {
        "id": issue_id,
        "project": {"id": 5, "name": "D-TELEKOM Infra"},
        "tracker": {"id": 1, "name": "Bug"},
        "status": {"id": 1, "name": "New"},
        "priority": {"id": 2, "name": "Normal"},
        "author": {"id": 42, "name": "Leo Test"},
        "subject": f"issue-{issue_id}",
        "created_on": created_on,
        "updated_on": updated_on,
        "is_private": is_private,
        "journals": journals or [],
    }


def _mock_redmine(
    httpx_mock: HTTPXMock,
    base_url: str,
    issues: list[dict[str, Any]],
) -> None:
    """Замокать Redmine так, как он ведёт себя на самом деле.

    Список отдаёт задачи БЕЗ поля journals (Redmine игнорирует
    include=journals для /issues.json), карточка задачи — с журналами.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=load_fixture("issue_statuses.json"),
        status_code=200,
        # Резолвер идёт в сеть только если в окне была смена статуса —
        # в тестах без статусов этот мок останется нетронутым, и это
        # правильное поведение, а не забытый мок.
        is_optional=True,
    )

    # Карточка каждой задачи — с журналами.
    for issue in issues:
        httpx_mock.add_response(
            method="GET",
            url=re.compile(rf".*/issues/{issue['id']}\.json.*"),
            json={"issue": issue},
            status_code=200,
            # Приватные задачи детектор до карточки не доводит.
            is_optional=True,
        )

    # Список — журналы из ответа выпиливаем, как это делает сам Redmine.
    listed = [{k: v for k, v in i.items() if k != "journals"} for i in issues]
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r".*/issues\.json.*"),
        json={
            "issues": listed,
            "total_count": len(listed),
            "offset": 0,
            "limit": 100,
        },
        status_code=200,
    )


@pytest.fixture
def resolver(client: RedmineClient) -> StatusResolver:
    return StatusResolver(client, ttl=timedelta(hours=1))


async def test_naive_now_is_rejected(
    client: RedmineClient,
    resolver: StatusResolver,
) -> None:
    """naive datetime до сравнения с created_on из Redmine не доживёт —
    ловим на входе, а не TypeError'ом из глубины цикла."""
    with pytest.raises(ValueError, match="aware datetime"):
        await poll_recent_changes(
            client,
            resolver,
            WARM_CURSOR,
            lookback=LOOKBACK,
            now=datetime(2026, 7, 15, 12, 0, 0),
        )


async def test_cold_start_sets_baseline_and_sends_nothing(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Первый запуск: событий ноль, курсор выставлен по максимумам.

    Без этого сервис на старте вывалил бы в чат всё, что попало в окно.
    """
    _mock_redmine(
        httpx_mock,
        base_url,
        [
            _issue(
                201,
                created_on=_dt(3),
                updated_on=_dt(2),
                journals=[_journal(501, created_on=_dt(2), notes="свежий коммент")],
            ),
            _issue(202, created_on=_dt(1), updated_on=_dt(1)),
        ],
    )

    events, cursor = await poll_recent_changes(
        client, resolver, PollCursor(), lookback=LOOKBACK, now=NOW
    )

    assert events == []
    assert cursor.last_seen_issue_id == 202
    assert cursor.last_seen_journal_id == 501
    assert cursor.last_check_at == NOW


async def test_new_issue_detected_by_id(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """issue.id > last_seen_issue_id → NewIssueEvent."""
    _mock_redmine(
        httpx_mock,
        base_url,
        [_issue(201, created_on=_dt(2), updated_on=_dt(2))],
    )

    events, cursor = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, NewIssueEvent)
    assert event.issue.id == 201
    assert event.occurred_at == datetime.fromisoformat(_dt(2).replace("Z", "+00:00"))
    assert cursor.last_seen_issue_id == 201


async def test_known_issue_updated_is_not_new(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Задача создана ВНУТРИ окна, но мы её уже видели (id <= last_seen).

    Это разделяющий случай между двумя подходами к детекции: окно
    lookback'а всегда захватывает часть прошлого цикла, поэтому по
    created_on такая задача выглядит новой — и уехала бы в чат вторым
    уведомлением. По id она новой не является.
    """
    _mock_redmine(
        httpx_mock,
        base_url,
        [_issue(200, created_on=_dt(3), updated_on=_dt(1))],
    )

    events, _ = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    assert events == []


async def test_status_change_resolves_names(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Смена статуса: id из журнала (строки!) резолвятся в имена
    до создания события — якорь 4.8."""
    _mock_redmine(
        httpx_mock,
        base_url,
        [
            _issue(
                200,
                created_on="2025-01-01T10:00:00Z",
                updated_on=_dt(1),
                journals=[
                    _journal(
                        501,
                        created_on=_dt(1),
                        details=[_status_detail("2", "3")],
                    )
                ],
            )
        ],
    )

    events, cursor = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, StatusChangedEvent)
    assert event.journal_id == 501
    assert event.old_status_id == 2
    assert event.old_status_name == "В работе"
    assert event.new_status_id == 3
    assert event.new_status_name == "Решена"
    assert event.changed_by.name == "Leo Test"
    assert cursor.last_seen_journal_id == 501


async def test_one_journal_yields_status_and_comment(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Человек меняет статус и пишет комментарий одним действием —
    для чата это два разных факта."""
    _mock_redmine(
        httpx_mock,
        base_url,
        [
            _issue(
                200,
                created_on="2025-01-01T10:00:00Z",
                updated_on=_dt(1),
                journals=[
                    _journal(
                        501,
                        created_on=_dt(1),
                        notes="Починил через nmcli.",
                        details=[_status_detail("2", "3")],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    assert len(events) == 2
    assert {type(e) for e in events} == {StatusChangedEvent, CommentAddedEvent}
    comment = next(e for e in events if isinstance(e, CommentAddedEvent))
    assert comment.notes == "Починил через nmcli."
    assert comment.author.id == 42


async def test_private_notes_skip_comment_but_keep_status(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """private_notes прячет текст заметки, но не изменение атрибутов:
    комментарий в общий чат не уходит, смена статуса — уходит."""
    _mock_redmine(
        httpx_mock,
        base_url,
        [
            _issue(
                200,
                created_on="2025-01-01T10:00:00Z",
                updated_on=_dt(1),
                journals=[
                    _journal(
                        501,
                        created_on=_dt(1),
                        notes="внутреннее: клиент невменяемый",
                        private_notes=True,
                        details=[_status_detail("2", "3")],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    assert len(events) == 1
    assert isinstance(events[0], StatusChangedEvent)


async def test_deleted_status_is_skipped_with_warning(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Статуса нет в Redmine (удалили после того, как он попал в журнал) —
    событие не собираем, но и молчать не имеем права."""
    _mock_redmine(
        httpx_mock,
        base_url,
        [
            _issue(
                200,
                created_on="2025-01-01T10:00:00Z",
                updated_on=_dt(1),
                journals=[
                    _journal(
                        501,
                        created_on=_dt(1),
                        details=[_status_detail("2", "99")],
                    )
                ],
            )
        ],
    )

    with caplog.at_level(logging.WARNING):
        events, _ = await poll_recent_changes(
            client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
        )

    assert events == []
    assert "статус id=99 не найден" in caplog.text


async def test_empty_window_keeps_cursor_ids(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Пустое окно означает «ничего не изменилось», а не «забудь всё»:
    максимумы id должны пережить тихий цикл, иначе следующий же цикл
    переотправит всё заново."""
    _mock_redmine(httpx_mock, base_url, [])

    events, cursor = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    assert events == []
    assert cursor.last_seen_issue_id == WARM_CURSOR.last_seen_issue_id
    assert cursor.last_seen_journal_id == WARM_CURSOR.last_seen_journal_id
    assert cursor.last_check_at == NOW


async def test_events_sorted_by_occurred_at(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """В чат события должны попадать в том порядке, в котором произошли,
    а не в том, в котором Redmine отдал задачи."""
    _mock_redmine(
        httpx_mock,
        base_url,
        [
            _issue(
                201,
                created_on=_dt(1),  # создана позже всех
                updated_on=_dt(1),
            ),
            _issue(
                200,
                created_on="2025-01-01T10:00:00Z",
                updated_on=_dt(2),
                journals=[_journal(501, created_on=_dt(4), notes="раньше всех")],
            ),
        ],
    )

    events, _ = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    assert len(events) == 2
    assert [e.occurred_at for e in events] == sorted(e.occurred_at for e in events)
    assert isinstance(events[0], CommentAddedEvent)
    assert isinstance(events[1], NewIssueEvent)


async def test_list_request_uses_window_and_all_statuses(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Запрос списка: окно updated_on и все статусы.

    status_id=* — не косметика: по умолчанию /issues.json отдаёт только
    открытые задачи, и уведомление «задачу закрыли» не пришло бы никогда.

    include здесь НЕ просим: Redmine его для списка игнорирует, а лишний
    параметр в запросе намекал бы читателю, что журналы приедут отсюда.
    """
    _mock_redmine(httpx_mock, base_url, [])

    await poll_recent_changes(client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW)

    params = httpx_mock.get_requests()[0].url.params
    assert params["status_id"] == "*"
    assert "include" not in params
    # Окно = last_check_at (NOW - 1мин) - lookback (5мин) = NOW - 6мин.
    assert params["updated_on"] == f">={_dt(6)}"


async def test_journals_are_fetched_per_issue(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Журналы тянутся карточкой каждой задачи, а не из списка.

    Redmine отдаёт include=journals только для /issues/{id}.json — в списке
    молча игнорирует. Без второго запроса детектор не увидел бы ни смен
    статуса, ни комментариев, то есть половину всех уведомлений.
    """
    _mock_redmine(
        httpx_mock,
        base_url,
        [
            _issue(
                201,
                created_on=_dt(2),
                updated_on=_dt(1),
                journals=[_journal(501, created_on=_dt(1), notes="из карточки")],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    # Событие из журнала доехало — значит карточку реально запросили.
    assert any(isinstance(e, CommentAddedEvent) for e in events)

    requests = httpx_mock.get_requests()
    card = next(r for r in requests if "/issues/201.json" in str(r.url))
    assert card.url.params["include"] == "journals"


async def test_private_issue_is_skipped_entirely(
    client: RedmineClient,
    resolver: StatusResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Приватная задача не даёт никаких событий.

    Задачу спрятали от посторонних — значит и факт её существования
    не для общего чата проекта. Карточку тоже не запрашиваем: нечего
    тянуть то, что всё равно не отправим.
    """
    _mock_redmine(
        httpx_mock,
        base_url,
        [
            _issue(
                201,
                created_on=_dt(2),
                updated_on=_dt(1),
                is_private=True,
                journals=[_journal(501, created_on=_dt(1), notes="секрет")],
            )
        ],
    )

    events, cursor = await poll_recent_changes(
        client, resolver, WARM_CURSOR, lookback=LOOKBACK, now=NOW
    )

    assert events == []
    assert not any("/issues/201.json" in str(r.url) for r in httpx_mock.get_requests())
    # Курсор при этом двигается: приватную задачу мы видели и разбирать
    # её повторно каждый цикл незачем.
    assert cursor.last_check_at == NOW
