"""Тесты доменных моделей событий.

Проверяем:
- корректное конструирование каждого типа события;
- автоматическое заполнение поля event_type;
- иммутабельность (frozen=True);
- запрет неизвестных полей (extra="forbid");
- валидатор IssueUpdatedEvent: пустое событие (ни одного изменения) отклоняется;
- парсинг discriminated union через EventAdapter (по event_type
  выбирается правильный класс);
- отклонение невалидного/отсутствующего event_type.

Тестам события нужен объект Issue. Мы переиспользуем ту же
JSON-фикстуру, что и клиент — это гарантирует, что модель события
работает с реальной формой Issue, а не с искусственно уменьшенной.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from redmine_max_notifier.events import (
    DueDateApproachingEvent,
    DueDateChange,
    EventAdapter,
    IssueUpdatedEvent,
    NameChange,
    NewIssueEvent,
)
from redmine_max_notifier.redmine.models import Issue, NamedRef
from tests.conftest import load_fixture


@pytest.fixture
def sample_issue() -> Issue:
    """Разбираем реальный JSON-ответ Redmine в Issue.

    Формат {"issue": {...}} — ровно как /issues/{id}.json отдаёт.
    """
    data = load_fixture("issue_single.json")
    return Issue.model_validate(data["issue"])


# ─── Конструирование событий ────────────────────────────────────────────


class TestConstruction:
    """Каждое событие создаётся с валидными полями, event_type подставлен."""

    def test_new_issue_event(self, sample_issue: Issue) -> None:
        event = NewIssueEvent(
            occurred_at=sample_issue.created_on,
            issue=sample_issue,
        )
        # event_type подставляется автоматом (Literal со значением по умолчанию)
        assert event.event_type == "new_issue"
        assert event.issue.id == sample_issue.id

    def test_issue_updated_with_all_changes(self, sample_issue: Issue) -> None:
        """Одна запись журнала несёт разом статус, приоритет, срок и коммент."""
        event = IssueUpdatedEvent(
            occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
            issue=sample_issue,
            journal_id=42,
            author=NamedRef(id=7, name="Иван Петров"),
            status_change=NameChange(old="Новая", new="В работе"),
            priority_change=NameChange(old="Нормальный", new="Высокий"),
            due_date_change=DueDateChange(old=date(2026, 7, 20), new=date(2026, 7, 17)),
            notes="Взял в работу.",
            attachments=["схема.png"],
        )
        assert event.event_type == "issue_updated"
        assert event.journal_id == 42
        assert event.status_change is not None
        assert event.status_change.new == "В работе"
        assert event.priority_change is not None
        assert event.due_date_change is not None
        assert event.due_date_change.new == date(2026, 7, 17)
        assert event.notes == "Взял в работу."

    def test_issue_updated_partial_only_status(self, sample_issue: Issue) -> None:
        """Достаточно одного изменения — остальные поля остаются None."""
        event = IssueUpdatedEvent(
            occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
            issue=sample_issue,
            journal_id=42,
            author=NamedRef(id=7, name="Иван Петров"),
            status_change=NameChange(new="В работе"),  # без old — первая смена
        )
        assert event.status_change is not None
        assert event.status_change.old is None
        assert event.priority_change is None
        assert event.due_date_change is None
        assert event.notes == ""
        assert event.attachments == []

    def test_issue_updated_rejects_empty(self, sample_issue: Issue) -> None:
        """Ни одного изменения — событие собрать нельзя.

        Пустая запись журнала — это смена того, что мы не показываем
        (например assigned_to). Детектор такой journal фильтрует, модель
        страхует: лучше ValidationError, чем пустое «задача обновлена».
        """
        with pytest.raises(ValidationError, match="хотя бы одно изменение"):
            IssueUpdatedEvent(
                occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
                issue=sample_issue,
                journal_id=42,
                author=NamedRef(id=7, name="Иван Петров"),
            )

    def test_due_date_approaching_event(self, sample_issue: Issue) -> None:
        event = DueDateApproachingEvent(
            occurred_at=datetime(2025, 1, 15, 9, 0, tzinfo=UTC),
            issue=sample_issue,
            days_before=3,
        )
        assert event.event_type == "due_date_approaching"
        assert event.days_before == 3

    def test_due_date_allows_negative_days(self, sample_issue: Issue) -> None:
        """Отрицательное значение = просрочено. Явно допустимо."""
        event = DueDateApproachingEvent(
            occurred_at=datetime(2025, 1, 15, 9, 0, tzinfo=UTC),
            issue=sample_issue,
            days_before=-5,
        )
        assert event.days_before == -5


# ─── Иммутабельность и запрет лишних полей ──────────────────────────────


class TestFrozenAndForbid:
    """Событие — факт из прошлого. frozen + extra=forbid."""

    def test_event_is_frozen(self, sample_issue: Issue) -> None:
        event = NewIssueEvent(
            occurred_at=sample_issue.created_on,
            issue=sample_issue,
        )
        with pytest.raises(ValidationError):
            event.occurred_at = datetime(2099, 1, 1, tzinfo=UTC)

    def test_extra_fields_rejected(self, sample_issue: Issue) -> None:
        """Опечатка в имени поля должна поймать Pydantic, а не всплыть
        уже в шаблоне через AttributeError."""
        with pytest.raises(ValidationError):
            NewIssueEvent(
                occurred_at=sample_issue.created_on,
                issue=sample_issue,
                priorety="high",  # type: ignore[call-arg]
            )


# ─── Discriminated union через EventAdapter ─────────────────────────────


class TestDiscriminatedUnion:
    """EventAdapter парсит dict → правильный конкретный тип по event_type."""

    def _base_payload(self, sample_issue: Issue) -> dict[str, Any]:
        """Общая часть payload'а, куда конкретный тест дольёт специфику."""
        return {
            "occurred_at": "2025-01-15T12:00:00+00:00",
            "issue": sample_issue.model_dump(mode="json"),
        }

    def test_parses_new_issue(self, sample_issue: Issue) -> None:
        payload = {**self._base_payload(sample_issue), "event_type": "new_issue"}
        event = EventAdapter.validate_python(payload)
        # Именно тот тип, не абстрактный EventBase — mypy это тоже увидит
        assert isinstance(event, NewIssueEvent)

    def test_parses_issue_updated(self, sample_issue: Issue) -> None:
        payload = {
            **self._base_payload(sample_issue),
            "event_type": "issue_updated",
            "journal_id": 42,
            "author": {"id": 7, "name": "Иван Петров"},
            "status_change": {"old": "Новая", "new": "В работе"},
        }
        event = EventAdapter.validate_python(payload)
        assert isinstance(event, IssueUpdatedEvent)
        assert event.status_change is not None
        assert event.status_change.new == "В работе"

    def test_parses_due_date_approaching(self, sample_issue: Issue) -> None:
        payload = {
            **self._base_payload(sample_issue),
            "event_type": "due_date_approaching",
            "days_before": 3,
        }
        event = EventAdapter.validate_python(payload)
        assert isinstance(event, DueDateApproachingEvent)

    def test_rejects_unknown_event_type(self, sample_issue: Issue) -> None:
        payload = {
            **self._base_payload(sample_issue),
            "event_type": "issue_deleted",  # такого типа нет
        }
        with pytest.raises(ValidationError):
            EventAdapter.validate_python(payload)

    def test_rejects_missing_event_type(self, sample_issue: Issue) -> None:
        """Без event_type Pydantic не может выбрать тип и должен упасть."""
        payload = self._base_payload(sample_issue)
        with pytest.raises(ValidationError):
            EventAdapter.validate_python(payload)

    def test_roundtrip_json(self, sample_issue: Issue) -> None:
        """Событие сериализуется в JSON и парсится обратно в тот же тип.

        Пригодится при хранении в БД и в любых логах.
        """
        original = IssueUpdatedEvent(
            occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
            issue=sample_issue,
            journal_id=42,
            author=NamedRef(id=7, name="Иван Петров"),
            status_change=NameChange(old="Новая", new="В работе"),
            due_date_change=DueDateChange(old=None, new=date(2026, 7, 17)),
        )
        raw = original.model_dump(mode="json")
        restored = EventAdapter.validate_python(raw)
        assert isinstance(restored, IssueUpdatedEvent)
        assert restored == original
