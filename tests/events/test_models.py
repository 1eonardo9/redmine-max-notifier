"""Тесты доменных моделей событий.

Проверяем:
- корректное конструирование каждого типа события;
- автоматическое заполнение поля event_type;
- иммутабельность (frozen=True);
- запрет неизвестных полей (extra="forbid");
- парсинг discriminated union через EventAdapter (по event_type
  выбирается правильный класс);
- отклонение невалидного/отсутствующего event_type.

Тестам события нужен объект Issue. Мы переиспользуем ту же
JSON-фикстуру, что и клиент — это гарантирует, что модель события
работает с реальной формой Issue, а не с искусственно уменьшенной.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from redmine_max_notifier.events import (
    CommentAddedEvent,
    DueDateApproachingEvent,
    EventAdapter,
    NewIssueEvent,
    StatusChangedEvent,
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

    def test_status_changed_event(self, sample_issue: Issue) -> None:
        event = StatusChangedEvent(
            occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
            issue=sample_issue,
            journal_id=42,
            old_status_id=1,
            old_status_name="Новая",
            new_status_id=2,
            new_status_name="В работе",
            changed_by=NamedRef(id=7, name="Иван Петров"),
        )
        assert event.event_type == "status_changed"
        assert event.journal_id == 42
        assert event.old_status_id == 1
        assert event.old_status_name == "Новая"
        assert event.new_status_id == 2
        assert event.new_status_name == "В работе"

    def test_status_changed_event_allows_no_old_status(
        self, sample_issue: Issue
    ) -> None:
        """Если у самой первой смены статуса прежнего значения нет — ок."""
        event = StatusChangedEvent(
            occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
            issue=sample_issue,
            journal_id=42,
            new_status_id=2,
            new_status_name="В работе",
            changed_by=NamedRef(id=7, name="Иван Петров"),
        )
        assert event.old_status_id is None
        assert event.old_status_name is None

    def test_comment_added_event(self, sample_issue: Issue) -> None:
        event = CommentAddedEvent(
            occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
            issue=sample_issue,
            journal_id=100,
            notes="Взял в работу.",
            author=NamedRef(id=7, name="Иван Петров"),
        )
        assert event.event_type == "comment_added"
        assert event.notes == "Взял в работу."

    def test_comment_added_rejects_empty_notes(self, sample_issue: Issue) -> None:
        """Пустой notes — это не комментарий, детектор такой journal
        должен фильтровать. Модель тоже страхует."""
        with pytest.raises(ValidationError):
            CommentAddedEvent(
                occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
                issue=sample_issue,
                journal_id=100,
                notes="",
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

    def test_parses_status_changed(self, sample_issue: Issue) -> None:
        payload = {
            **self._base_payload(sample_issue),
            "event_type": "status_changed",
            "journal_id": 42,
            "old_status_id": 1,
            "old_status_name": "Новая",
            "new_status_id": 2,
            "new_status_name": "В работе",
            "changed_by": {"id": 7, "name": "Иван Петров"},
        }
        event = EventAdapter.validate_python(payload)
        assert isinstance(event, StatusChangedEvent)
        assert event.new_status_id == 2
        assert event.new_status_name == "В работе"

    def test_parses_comment_added(self, sample_issue: Issue) -> None:
        payload = {
            **self._base_payload(sample_issue),
            "event_type": "comment_added",
            "journal_id": 100,
            "notes": "Взял в работу.",
            "author": {"id": 7, "name": "Иван Петров"},
        }
        event = EventAdapter.validate_python(payload)
        assert isinstance(event, CommentAddedEvent)

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

        Пригодится на Этапе 6 (хранение в БД) и в любых логах.
        """
        original = StatusChangedEvent(
            occurred_at=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
            issue=sample_issue,
            journal_id=42,
            old_status_id=1,
            old_status_name="Новая",
            new_status_id=2,
            new_status_name="В работе",
            changed_by=NamedRef(id=7, name="Иван Петров"),
        )
        raw = original.model_dump(mode="json")
        restored = EventAdapter.validate_python(raw)
        assert isinstance(restored, StatusChangedEvent)
        assert restored == original
