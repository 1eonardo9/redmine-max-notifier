"""Тесты детектора изменений (poll_recent_changes).

Форма мок-ответов Redmine скопирована с реальных фикстур и живого API
(этап 9: priority_id и due_date в details подтверждены на боевом
Redmine), а не выдумана — якорь 4.12.

ВАЖНО про журналы. Redmine отдаёт include=journals только для одиночной
задачи. В списке (/issues.json) параметр молча игнорируется — поля
journals в ответе просто нет. Моки это воспроизводят: список отдаёт
задачи БЕЗ журналов, журналы живут только в ответах /issues/{id}.json.

ВАЖНО про модель. Одна запись журнала → одно IssueUpdatedEvent, даже
если в ней сменились и статус, и приоритет, и срок, и добавился коммент.
Это замена прежних раздельных StatusChanged/CommentAdded (решение Leo).

Мокаются четыре эндпоинта: /issues.json (список), /issues/{id}.json
(карточка с журналами), /issue_statuses.json и
/enumerations/issue_priorities.json (резолверы).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from redmine_max_notifier.events.models import IssueUpdatedEvent, NewIssueEvent
from redmine_max_notifier.name_resolver import NameResolver
from redmine_max_notifier.poller import PollCursor, poll_recent_changes
from redmine_max_notifier.redmine.client import RedmineClient
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


def _attr_detail(name: str, old: str | None, new: str | None) -> dict[str, Any]:
    """Смена стандартного атрибута: status_id, priority_id, due_date."""
    return {"property": "attr", "name": name, "old_value": old, "new_value": new}


def _attachment_detail(attachment_id: str, filename: str) -> dict[str, Any]:
    """Так Redmine пишет прикрепление файла — проверено на живом API."""
    return {
        "property": "attachment",
        "name": attachment_id,
        "old_value": None,
        "new_value": filename,
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
    Справочники статусов и приоритетов — is_optional: резолвер идёт в сеть
    только если в окне была смена соответствующего атрибута.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/issue_statuses.json",
        json=load_fixture("issue_statuses.json"),
        status_code=200,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{base_url}/enumerations/issue_priorities.json",
        json=load_fixture("issue_priorities.json"),
        status_code=200,
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
def status_resolver(client: RedmineClient) -> NameResolver:
    return NameResolver(
        client.list_issue_statuses, timedelta(hours=1), label="статусов"
    )


@pytest.fixture
def priority_resolver(client: RedmineClient) -> NameResolver:
    return NameResolver(
        client.list_issue_priorities, timedelta(hours=1), label="приоритетов"
    )


async def test_naive_now_is_rejected(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
) -> None:
    """naive datetime до сравнения с created_on из Redmine не доживёт —
    ловим на входе, а не TypeError'ом из глубины цикла."""
    with pytest.raises(ValueError, match="aware datetime"):
        await poll_recent_changes(
            client,
            status_resolver,
            priority_resolver,
            WARM_CURSOR,
            lookback=LOOKBACK,
            now=datetime(2026, 7, 15, 12, 0, 0),
        )


async def test_cold_start_sets_baseline_and_sends_nothing(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
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
        client,
        status_resolver,
        priority_resolver,
        PollCursor(),
        lookback=LOOKBACK,
        now=NOW,
    )

    assert events == []
    assert cursor.last_seen_issue_id == 202
    assert cursor.last_seen_journal_id == 501
    assert cursor.last_check_at == NOW


async def test_new_issue_detected_by_id(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
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
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, NewIssueEvent)
    assert event.issue.id == 201
    assert event.occurred_at == datetime.fromisoformat(_dt(2).replace("Z", "+00:00"))
    assert cursor.last_seen_issue_id == 201


async def test_known_issue_updated_is_not_new(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
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
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert events == []


async def test_status_change_resolves_names(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
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
                        details=[_attr_detail("status_id", "2", "3")],
                    )
                ],
            )
        ],
    )

    events, cursor = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, IssueUpdatedEvent)
    assert event.journal_id == 501
    assert event.status_change is not None
    assert event.status_change.old == "В работе"
    assert event.status_change.new == "Решена"
    assert event.priority_change is None
    assert event.author.name == "Leo Test"
    assert cursor.last_seen_journal_id == 501


async def test_priority_change_resolves_names(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Смена приоритета: priority_id резолвится в имя через свой резолвер."""
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
                        details=[_attr_detail("priority_id", "2", "3")],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, IssueUpdatedEvent)
    assert event.priority_change is not None
    assert event.priority_change.old == "Нормальный"
    assert event.priority_change.new == "Высокий"
    assert event.status_change is None


async def test_due_date_change_parsed(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Смена срока: даты из журнала (строки) парсятся в date, без резолва."""
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
                        details=[_attr_detail("due_date", "2026-07-20", "2026-07-17")],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, IssueUpdatedEvent)
    assert event.due_date_change is not None
    assert event.due_date_change.old == date(2026, 7, 20)
    assert event.due_date_change.new == date(2026, 7, 17)


async def test_one_journal_yields_single_event(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Статус, приоритет, срок и комментарий одной записью — ОДНО событие.

    Раньше это давало два уведомления (статус и коммент отдельно);
    теперь единый факт «задача обновлена» (решение Leo).
    """
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
                        details=[
                            _attr_detail("status_id", "2", "3"),
                            _attr_detail("priority_id", "2", "3"),
                            _attr_detail("due_date", "2026-07-20", "2026-07-17"),
                        ],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, IssueUpdatedEvent)
    assert event.status_change is not None and event.status_change.new == "Решена"
    assert event.priority_change is not None and event.priority_change.new == "Высокий"
    assert event.due_date_change is not None
    assert event.notes == "Починил через nmcli."
    assert event.author.id == 42


async def test_attachment_without_notes_yields_event(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Файл прикрепили молча — событие всё равно есть.

    Redmine пишет вложение в details, а не в notes. Пока событие
    требовало непустой notes, такой journal пропадал (поймано на 7h).
    """
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
                        notes="",
                        details=[_attachment_detail("5", "i.webp")],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, IssueUpdatedEvent)
    assert event.notes == ""
    assert event.attachments == ["i.webp"]


async def test_removed_attachment_is_not_reported_as_added(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Удаление файла Redmine пишет теми же details, но имя в old_value.

    Без фильтра по new_value «удалил схему» приехало бы в чат как
    «приложил схему». А раз больше в записи ничего нет — события нет.
    """
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
                        notes="",
                        details=[
                            {
                                "property": "attachment",
                                "name": "5",
                                "old_value": "i.webp",
                                "new_value": None,
                            }
                        ],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert events == []


async def test_private_note_hides_attachments_too(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Приватная заметка прячет и файлы, приложенные к ней.

    Имя файла выдаёт содержание не хуже текста ("договор_с_ценами.pdf").
    Раз кроме приватного контента в записи ничего нет — события нет.
    """
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
                        notes="только для своих",
                        private_notes=True,
                        details=[_attachment_detail("5", "договор_с_ценами.pdf")],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert events == []


async def test_private_notes_skip_comment_but_keep_status(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """private_notes прячет текст заметки, но не изменение атрибутов:
    в событии остаётся смена статуса, а notes пустой."""
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
                        details=[_attr_detail("status_id", "2", "3")],
                    )
                ],
            )
        ],
    )

    events, _ = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, IssueUpdatedEvent)
    assert event.status_change is not None
    assert event.notes == ""
    assert event.attachments == []


async def test_deleted_status_is_skipped_with_warning(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Статуса нет в Redmine (удалили после того, как он попал в журнал) —
    смену статуса не собираем, но и молчать не имеем права. Раз других
    изменений в записи нет — события не будет вовсе."""
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
                        details=[_attr_detail("status_id", "2", "99")],
                    )
                ],
            )
        ],
    )

    with caplog.at_level(logging.WARNING):
        events, _ = await poll_recent_changes(
            client,
            status_resolver,
            priority_resolver,
            WARM_CURSOR,
            lookback=LOOKBACK,
            now=NOW,
        )

    assert events == []
    assert "статус id=99 не найден" in caplog.text


async def test_empty_window_keeps_cursor_ids(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Пустое окно означает «ничего не изменилось», а не «забудь всё»:
    максимумы id должны пережить тихий цикл, иначе следующий же цикл
    переотправит всё заново."""
    _mock_redmine(httpx_mock, base_url, [])

    events, cursor = await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert events == []
    assert cursor.last_seen_issue_id == WARM_CURSOR.last_seen_issue_id
    assert cursor.last_seen_journal_id == WARM_CURSOR.last_seen_journal_id
    assert cursor.last_check_at == NOW


async def test_events_sorted_by_occurred_at(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
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
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert len(events) == 2
    assert [e.occurred_at for e in events] == sorted(e.occurred_at for e in events)
    assert isinstance(events[0], IssueUpdatedEvent)
    assert isinstance(events[1], NewIssueEvent)


async def test_list_request_uses_window_and_all_statuses(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Запрос списка: окно updated_on и все статусы.

    status_id=* — не косметика: по умолчанию /issues.json отдаёт только
    открытые задачи, и уведомление «задачу закрыли» не пришло бы никогда.

    include здесь НЕ просим: Redmine его для списка игнорирует.
    """
    _mock_redmine(httpx_mock, base_url, [])

    await poll_recent_changes(
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    params = httpx_mock.get_requests()[0].url.params
    assert params["status_id"] == "*"
    assert "include" not in params
    # Окно = last_check_at (NOW - 1мин) - lookback (5мин) = NOW - 6мин.
    assert params["updated_on"] == f">={_dt(6)}"


async def test_journals_are_fetched_per_issue(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Журналы тянутся карточкой каждой задачи, а не из списка.

    Redmine отдаёт include=journals только для /issues/{id}.json — в списке
    молча игнорирует. Без второго запроса детектор не увидел бы ни смен
    статуса, ни комментариев.
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
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    # Событие из журнала доехало — значит карточку реально запросили.
    assert any(isinstance(e, IssueUpdatedEvent) for e in events)

    requests = httpx_mock.get_requests()
    card = next(r for r in requests if "/issues/201.json" in str(r.url))
    assert card.url.params["include"] == "journals"


async def test_private_issue_is_skipped_entirely(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    base_url: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Приватная задача не даёт никаких событий.

    Задачу спрятали от посторонних — значит и факт её существования
    не для общего чата проекта. Карточку тоже не запрашиваем.
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
        client,
        status_resolver,
        priority_resolver,
        WARM_CURSOR,
        lookback=LOOKBACK,
        now=NOW,
    )

    assert events == []
    assert not any("/issues/201.json" in str(r.url) for r in httpx_mock.get_requests())
    # Курсор при этом двигается: приватную задачу мы видели и разбирать
    # её повторно каждый цикл незачем.
    assert cursor.last_check_at == NOW
