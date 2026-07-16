"""Тесты MessageRenderer.

Тесты живут в корне tests/, а не в подкаталоге:
рендерер — плоский модуль без своих фикстур, отдельный
conftest.py ему не нужен.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from redmine_max_notifier.events.models import (
    DueDateApproachingEvent,
    DueDateChange,
    IssueUpdatedEvent,
    NameChange,
    NewIssueEvent,
)
from redmine_max_notifier.redmine.models import Issue, NamedRef
from redmine_max_notifier.renderer import (
    MessageRenderer,
    escape_markdown,
    format_datetime,
    priority_emoji,
    status_emoji,
)

# Дефолтные NamedRef для фабрики Issue вынесены на уровень модуля:
# 1) обходит Ruff B008 (call в default-аргументе);
# 2) NamedRef — frozen, шаринг одного экземпляра между тестами безопасен.
_DEFAULT_PROJECT = NamedRef(id=1, name="Тестовый проект")
_DEFAULT_TRACKER = NamedRef(id=1, name="Bug")
_DEFAULT_ASSIGNEE = NamedRef(id=7, name="Иван Иванов")
_AT = datetime(2026, 7, 14, 15, 30, tzinfo=UTC)


def _make_issue(
    *,
    issue_id: int = 42,
    subject: str = "Тестовая задача",
    description: str | None = "Описание задачи",
    assigned_to: NamedRef | None = _DEFAULT_ASSIGNEE,
    due_date: date | None = None,
    priority: str = "Нормальный",
) -> Issue:
    """Фабрика Issue для тестов. Именованные аргументы обязательны —
    так вызов теста читается сам по себе.

    Имена статуса и приоритета — реальные из нашего Redmine
    (GET /issue_statuses.json, /enumerations/issue_priorities.json):
    от них зависит маппинг эмодзи, и выдуманное "Обычный" вместо
    "Нормальный" молча дало бы фолбэк вместо цвета.
    """
    return Issue(
        id=issue_id,
        project=_DEFAULT_PROJECT,
        tracker=_DEFAULT_TRACKER,
        subject=subject,
        description=description,
        status=NamedRef(id=1, name="Новая"),
        priority=NamedRef(id=2, name=priority),
        author=NamedRef(id=5, name="Пётр Петров"),
        assigned_to=assigned_to,
        due_date=due_date,
        created_on=_AT,
        updated_on=_AT,
    )


def _updated(
    *,
    occurred_at: datetime = _AT,
    status_change: NameChange | None = None,
    priority_change: NameChange | None = None,
    due_date_change: DueDateChange | None = None,
    notes: str = "",
    attachments: list[str] | None = None,
    journal_id: int = 100,
) -> IssueUpdatedEvent:
    """Фабрика IssueUpdatedEvent — минимум обязательных полей."""
    return IssueUpdatedEvent(
        occurred_at=occurred_at,
        issue=_make_issue(),
        journal_id=journal_id,
        author=NamedRef(id=7, name="Иван Иванов"),
        status_change=status_change,
        priority_change=priority_change,
        due_date_change=due_date_change,
        notes=notes,
        attachments=attachments or [],
    )


@pytest.fixture
def renderer() -> MessageRenderer:
    """Один рендерер на тест — Environment собирается один раз,
    инстанс потокобезопасен для читающих операций.
    """
    return MessageRenderer(redmine_base_url="http://redmine.test/")


# ── NewIssueEvent ────────────────────────────────────────────────────────


def test_new_issue_renders_full_message(renderer: MessageRenderer) -> None:
    """Полный кейс: все поля заполнены, шаблон отдаёт связный текст."""
    event = NewIssueEvent(occurred_at=_AT, issue=_make_issue())

    result = renderer.render(event)

    assert "Новая задача #42" in result
    # Тема выводится с подписью "Тема:" (просьба Leo, этап 9).
    assert "*Тема:* Тестовая задача" in result
    assert "Иван Иванов" in result
    assert "Пётр Петров" in result
    # 15:30 UTC из Redmine показываем как 18:30 по Москве.
    assert "14.07.2026 18:30" in result


def test_new_issue_without_assignee_shows_fallback(renderer: MessageRenderer) -> None:
    """assigned_to=None должен превратиться в «не назначено»."""
    event = NewIssueEvent(occurred_at=_AT, issue=_make_issue(assigned_to=None))

    result = renderer.render(event)

    assert "не назначено" in result
    # И ни в коем случае не «None» — типичный баг забытого фолбэка
    assert "None" not in result


def test_new_issue_without_due_date_shows_fallback(renderer: MessageRenderer) -> None:
    """due_date=None должен превратиться в «не установлен»."""
    event = NewIssueEvent(occurred_at=_AT, issue=_make_issue(due_date=None))

    result = renderer.render(event)

    assert "не установлен" in result


def test_new_issue_long_description_truncated(renderer: MessageRenderer) -> None:
    """Длинное описание обрезается фильтром truncate(300)."""
    long_text = "Очень длинное описание. " * 100  # ~2400 символов
    event = NewIssueEvent(occurred_at=_AT, issue=_make_issue(description=long_text))

    result = renderer.render(event)

    # Многоточие U+2026 добавляется фильтром truncate
    assert "…" in result
    assert long_text not in result


def test_new_issue_without_description_no_empty_block(
    renderer: MessageRenderer,
) -> None:
    """Пустое описание не должно оставлять пустой строки/мусора."""
    event = NewIssueEvent(occurred_at=_AT, issue=_make_issue(description=None))

    result = renderer.render(event)

    # Не более одной пустой строки подряд (двух \n).
    assert "\n\n\n" not in result


# ── IssueUpdatedEvent: смена атрибутов ───────────────────────────────────


def test_issue_updated_status_full(renderer: MessageRenderer) -> None:
    """И старый, и новый статус известны — стрелка между ними."""
    event = _updated(status_change=NameChange(old="Новая", new="В работе"))

    result = renderer.render(event)

    assert "Задача обновлена · #42" in result
    assert "Новая" in result
    assert "В работе" in result
    assert "→" in result
    assert "Иван Иванов" in result


def test_issue_updated_status_without_old(renderer: MessageRenderer) -> None:
    """Первая смена статуса (нет old) — без стрелки."""
    event = _updated(status_change=NameChange(new="В работе"))

    result = renderer.render(event)

    assert "В работе" in result
    assert "→" not in result


def test_issue_updated_priority_change(renderer: MessageRenderer) -> None:
    """Смена приоритета: подпись, имена и цветное эмодзи."""
    event = _updated(priority_change=NameChange(old="Нормальный", new="Высокий"))

    result = renderer.render(event)

    assert "*Приоритет:*" in result
    assert "🟡 *Нормальный*" in result
    assert "🔴 *Высокий*" in result


def test_issue_updated_due_date_set(renderer: MessageRenderer) -> None:
    """Смена срока: обе даты в формате dd.mm.yyyy."""
    event = _updated(
        due_date_change=DueDateChange(old=date(2026, 7, 20), new=date(2026, 7, 17))
    )

    result = renderer.render(event)

    assert "*Срок:*" in result
    assert "20.07.2026 → 17.07.2026" in result


def test_issue_updated_due_date_cleared(renderer: MessageRenderer) -> None:
    """Срок сняли — new=None рендерится как «снят», а не «None»."""
    event = _updated(due_date_change=DueDateChange(old=date(2026, 7, 20), new=None))

    result = renderer.render(event)

    assert "20.07.2026 → снят" in result
    assert "None" not in result


def test_issue_updated_short_note(renderer: MessageRenderer) -> None:
    """Короткий комментарий выводится целиком, без обрезки."""
    event = _updated(notes="Взял в работу.")

    result = renderer.render(event)

    assert "Задача обновлена" in result
    assert "Иван Иванов" in result
    assert "Взял в работу." in result
    assert "…" not in result


def test_issue_updated_long_note_truncated(renderer: MessageRenderer) -> None:
    """Длинный комментарий обрезается фильтром truncate(500)."""
    long_note = "Разбираю проблему. " * 100  # ~1900 символов
    event = _updated(notes=long_note)

    result = renderer.render(event)

    assert "…" in result
    assert long_note not in result


def test_issue_updated_with_attachments(renderer: MessageRenderer) -> None:
    """Комментарий с файлами: и текст, и имена файлов."""
    event = _updated(
        notes="Приложил схему.",
        attachments=["ЗУ Штиль.JPG", "схема_трассы.pdf"],
    )

    result = renderer.render(event)

    assert "Приложил схему." in result
    assert "ЗУ Штиль.JPG" in result
    # Имена файлов — тоже текст от людей: подчёркивание экранируется.
    assert r"схема\_трассы.pdf" in result


def test_issue_updated_attachment_only(renderer: MessageRenderer) -> None:
    """Файл без единого слова — событие есть, шапка та же «Задача обновлена».

    Отдельного заголовка «Прикреплён файл» больше нет: одно событие на
    журнал — один заголовок.
    """
    event = _updated(attachments=["i.webp"])

    result = renderer.render(event)

    assert "Задача обновлена" in result
    assert "i.webp" in result
    assert "\n\n\n" not in result


def test_issue_updated_no_ping_even_if_mentions_passed(
    renderer: MessageRenderer,
) -> None:
    """На обновлении задачи исполнителя НЕ пингуем (решение Leo, 15.07).

    Даже если диспетчер передал mentions, шаблon issue_updated их
    не выводит — иначе каждая смена статуса/коммент частили бы пингами.
    """
    event = _updated(status_change=NameChange(old="Новая", new="В работе"))

    result = renderer.render(event, mentions=["[Кто-то](max://user/999)"])

    assert "max://user/" not in result
    assert "👉" not in result


def test_issue_updated_requires_content() -> None:
    """Ни изменений, ни текста, ни файлов — событие собрать нельзя."""
    with pytest.raises(ValidationError, match="хотя бы одно изменение"):
        _updated()


def test_issue_updated_notes_are_escaped(renderer: MessageRenderer) -> None:
    """Текст комментария пишут люди — экранируем спецсимволы markdown."""
    event = _updated(notes="Проверь _настройки_ и файл *config*")

    result = renderer.render(event)

    assert r"Проверь \_настройки\_ и файл \*config\*" in result


# ── DueDateApproachingEvent ──────────────────────────────────────────────


def test_due_date_approaching_future(renderer: MessageRenderer) -> None:
    """Положительный days_before — «Приближается дедлайн»."""
    event = DueDateApproachingEvent(
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        issue=_make_issue(due_date=date(2026, 7, 17)),
        days_before=3,
    )

    result = renderer.render(event)

    assert "Приближается дедлайн" in result
    assert "17.07.2026" in result
    assert "Осталось" in result
    assert "3 дн." in result


def test_due_date_approaching_today(renderer: MessageRenderer) -> None:
    """days_before == 0 — «Дедлайн сегодня»."""
    event = DueDateApproachingEvent(
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        issue=_make_issue(due_date=date(2026, 7, 14)),
        days_before=0,
    )

    result = renderer.render(event)

    assert "Дедлайн сегодня" in result
    assert "сегодня последний день" in result


def test_due_date_approaching_overdue(renderer: MessageRenderer) -> None:
    """Отрицательный days_before — «Задача просрочена»."""
    event = DueDateApproachingEvent(
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        issue=_make_issue(due_date=date(2026, 7, 9)),
        days_before=-5,
    )

    result = renderer.render(event)

    assert "просрочена" in result
    assert "Просрочено на" in result
    # Минус в шаблоне снимается — показываем «5 дн.», не «-5 дн.»
    assert "5 дн." in result
    assert "-5" not in result


# ── Экранирование markdown ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("*срочно*", r"\*срочно\*"),
        ("файл_с_подчёркиваниями", r"файл\_с\_подчёркиваниями"),
        ("[тег]", r"\[тег\]"),
        ("`код`", r"\`код\`"),
        (r"путь\сюда", r"путь\\сюда"),
        ("обычный текст", "обычный текст"),
    ],
)
def test_escape_markdown(raw: str, expected: str) -> None:
    """Спецсимволы markdown экранируются, обычный текст не трогаем."""
    assert escape_markdown(raw) == expected


def test_subject_with_markdown_does_not_break_layout(
    renderer: MessageRenderer,
) -> None:
    """Звёздочки в теме задачи не ломают вёрстку сообщения.

    Тему пишут люди. Без экранирования «Авария: *обрыв* ОК» открыла бы
    жирный не там и поехала бы вся разметка. Фильтр | md это глушит.
    """
    event = NewIssueEvent(
        occurred_at=_AT,
        issue=_make_issue(subject="Авария: *обрыв* ОК [срочно]"),
    )

    result = renderer.render(event)

    assert r"Авария: \*обрыв\* ОК \[срочно\]" in result


# ── Эмодзи статусов и приоритетов ───────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Новая", "🔵"),
        ("В работе", "⚙️"),
        ("Решена", "✅"),
        ("Нужен отклик", "❓"),
        ("Закрыта", "🔒"),
        ("Отклонена", "❌"),
        ("Ожидание", "⏸️"),
        # Регистр и пробелы из Redmine не должны ломать маппинг.
        ("  в РАБОТЕ  ", "⚙️"),
        # Админ волен завести свой статус — уведомление обязано доехать.
        ("Согласование с ГИП", "📌"),
        (None, "📌"),
    ],
)
def test_status_emoji(name: str | None, expected: str) -> None:
    """Все семь статусов нашего Redmine + фолбэк на незнакомый."""
    assert status_emoji(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Низкий", "🟢"),
        ("Нормальный", "🟡"),
        ("Высокий", "🔴"),
        ("Срочный", "🔴"),
        ("Немедленный", "🔴"),
        ("Свой приоритет", "⚪"),
        (None, "⚪"),
    ],
)
def test_priority_emoji(name: str | None, expected: str) -> None:
    """Трёхцветная шкала: зелёный, жёлтый, красный + фолбэк."""
    assert priority_emoji(name) == expected


def test_status_emoji_is_same_across_templates(renderer: MessageRenderer) -> None:
    """Один статус — одно эмодзи в любом шаблоне.

    Ради этого маппинг и живёт в одном словаре, а не ветками {% if %}
    по файлам: скопированные ветки разъезжаются при первой же правке.
    """
    new_issue = renderer.render(NewIssueEvent(occurred_at=_AT, issue=_make_issue()))
    updated = renderer.render(
        _updated(status_change=NameChange(old="Новая", new="В работе"))
    )

    # У задачи статус "Новая" — в обоих сообщениях он помечен одинаково.
    assert "🔵 Новая" in new_issue
    assert "🔵 *Новая*" in updated
    assert "⚙️ *В работе*" in updated


def test_priority_emoji_in_new_issue(renderer: MessageRenderer) -> None:
    """Приоритет в сообщении помечен цветом."""
    event = NewIssueEvent(occurred_at=_AT, issue=_make_issue(priority="Высокий"))

    result = renderer.render(event)

    assert "*Приоритет:* 🔴 Высокий" in result


# ── Время ───────────────────────────────────────────────────────────────


def test_time_is_rendered_in_business_timezone(renderer: MessageRenderer) -> None:
    """Время события показывается в таймзоне людей, а не в UTC.

    Redmine отдаёт время в UTC ("...Z"); голый strftime напечатал бы его
    как есть, и человек, закрывший задачу в 13:55, видел бы 10:55.
    """
    event = _updated(
        occurred_at=datetime(2026, 7, 15, 10, 55, tzinfo=UTC),
        status_change=NameChange(old="Новая", new="Закрыта"),
    )

    result = renderer.render(event)

    assert "15.07.2026 13:55" in result
    assert "10:55" not in result


def test_renderer_respects_configured_timezone() -> None:
    """Таймзона берётся из конфига, а не прибита к Москве."""
    renderer = MessageRenderer(
        redmine_base_url="http://redmine.test",
        tz=ZoneInfo("Asia/Yekaterinburg"),  # UTC+5
    )
    event = NewIssueEvent(occurred_at=_AT, issue=_make_issue())

    result = renderer.render(event)

    assert "14.07.2026 20:30" in result


def test_naive_datetime_treated_as_utc() -> None:
    """naive-время трактуем как UTC, а не как таймзону машины.

    Иначе результат рендера зависел бы от того, где запущен процесс.
    """
    assert (
        format_datetime(datetime(2026, 7, 15, 10, 55), ZoneInfo("Europe/Moscow"))
        == "15.07.2026 13:55"
    )
