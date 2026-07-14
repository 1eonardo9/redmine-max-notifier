"""Тесты MessageRenderer.

Тесты живут в корне tests/, а не в подкаталоге:
рендерер — плоский модуль без своих фикстур, отдельный
conftest.py ему не нужен.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from redmine_max_notifier.events.models import NewIssueEvent
from redmine_max_notifier.redmine.models import Issue, NamedRef
from redmine_max_notifier.renderer import MessageRenderer

# Дефолтные NamedRef для фабрики Issue вынесены на уровень модуля:
# 1) обходит Ruff B008 (call в default-аргументе);
# 2) NamedRef — frozen, шаринг одного экземпляра между тестами безопасен.
_DEFAULT_PROJECT = NamedRef(id=1, name="Тестовый проект")
_DEFAULT_TRACKER = NamedRef(id=1, name="Bug")
_DEFAULT_ASSIGNEE = NamedRef(id=7, name="Иван Иванов")


def _make_issue(
    *,
    issue_id: int = 42,
    subject: str = "Тестовая задача",
    description: str | None = "Описание задачи",
    assigned_to: NamedRef | None = _DEFAULT_ASSIGNEE,
    due_date: date | None = None,
) -> Issue:
    """Фабрика Issue для тестов. Именованные аргументы обязательны —
    так вызов теста читается сам по себе."""
    return Issue(
        id=issue_id,
        project=_DEFAULT_PROJECT,
        tracker=_DEFAULT_TRACKER,
        subject=subject,
        description=description,
        status=NamedRef(id=1, name="Новая"),
        priority=NamedRef(id=2, name="Обычный"),
        author=NamedRef(id=5, name="Пётр Петров"),
        assigned_to=assigned_to,
        due_date=due_date,
        created_on=datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
        updated_on=datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
    )


@pytest.fixture
def renderer() -> MessageRenderer:
    """Один рендерер на тест — Environment собирается один раз,
    инстанс потокобезопасен для читающих операций.
    Trailing slash в base_url — специально, чтобы проверить,
    что rstrip('/') в конструкторе реально работает."""
    return MessageRenderer(redmine_base_url="http://redmine.test/")


def test_new_issue_renders_full_message(renderer: MessageRenderer) -> None:
    """Полный кейс: все поля заполнены, шаблон отдаёт связный текст."""
    event = NewIssueEvent(
        occurred_at=datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
        issue=_make_issue(),
    )

    result = renderer.render(event)

    # Ключевые куски текста — без завязки на точное форматирование
    # (перекомпоновать шаблон легко, ронять тесты каждый раз — нет).
    assert "Новая задача #42" in result
    assert "Тестовая задача" in result
    assert "Иван Иванов" in result
    assert "Пётр Петров" in result
    assert "14.07.2026 15:30" in result
    # rstrip('/') сработал — в ссылке нет двойного слеша перед issues
    assert "http://redmine.test/issues/42" in result
    assert "//issues" not in result


def test_new_issue_without_assignee_shows_fallback(
    renderer: MessageRenderer,
) -> None:
    """assigned_to=None должен превратиться в «не назначено»."""
    event = NewIssueEvent(
        occurred_at=datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
        issue=_make_issue(assigned_to=None),
    )

    result = renderer.render(event)

    assert "не назначено" in result
    # И ни в коем случае не «None» — типичный баг забытого фолбэка
    assert "None" not in result


def test_new_issue_without_due_date_shows_fallback(
    renderer: MessageRenderer,
) -> None:
    """due_date=None должен превратиться в «не установлен»."""
    event = NewIssueEvent(
        occurred_at=datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
        issue=_make_issue(due_date=None),
    )

    result = renderer.render(event)

    assert "не установлен" in result


def test_new_issue_long_description_truncated(
    renderer: MessageRenderer,
) -> None:
    """Длинное описание обрезается фильтром truncate(300)."""
    long_text = "Очень длинное описание. " * 100  # ~2400 символов
    event = NewIssueEvent(
        occurred_at=datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
        issue=_make_issue(description=long_text),
    )

    result = renderer.render(event)

    # Многоточие U+2026 добавляется фильтром truncate
    assert "…" in result
    # Оригинала целиком в результате быть не должно
    assert long_text not in result


def test_new_issue_without_description_no_empty_block(
    renderer: MessageRenderer,
) -> None:
    """Пустое описание не должно оставлять пустой строки/мусора."""
    event = NewIssueEvent(
        occurred_at=datetime(2026, 7, 14, 15, 30, tzinfo=UTC),
        issue=_make_issue(description=None),
    )

    result = renderer.render(event)

    # Между дедлайном/датой и ссылкой на Redmine — не более
    # одной пустой строки (двух подряд \n).
    assert "\n\n\n" not in result
