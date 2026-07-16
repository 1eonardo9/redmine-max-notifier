"""Доменные модели событий, детектируемых поллером Redmine.

Каждое событие — Pydantic-модель с полем event_type (дискриминатор).
Discriminated union позволяет функциям-обработчикам принимать «любое
событие» одним параметром: mypy и Pydantic по значению event_type
понимают, какие ещё поля доступны, без ручного if/elif isinstance.

Событие концептуально иммутабельно: это факт, который уже произошёл
в Redmine. Отсюда frozen=True в конфиге — случайно поменять поле
в цепочке обработки не получится, Pydantic бросит ValidationError.

Модели событий этого модуля описывают только «форму» события в памяти.
Логика детекции — на Этапе 7, шаблонизация — на Этапе 5, персистентность
и идемпотентность — на Этапе 6. Здесь только данные.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from redmine_max_notifier.redmine.models import Issue, NamedRef


class EventBase(BaseModel):
    """Общая база для всех типов событий.

    Конфигурация:
    - frozen=True — событие иммутабельно (это факт из прошлого,
      его нельзя изменить задним числом).
    - extra="forbid" — запрещаем неизвестные поля. В отличие от моделей
      Redmine (там extra="ignore", т.к. парсим внешний JSON), события мы
      создаём сами — опечатку в имени поля лучше поймать сразу.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    occurred_at: datetime
    """Момент времени, когда событие произошло в Redmine.

    Для событий из журнала — journal.created_on.
    Для NewIssue — issue.created_on.
    Для DueDateApproaching — момент детекции поллером.
    """

    issue: Issue
    """Задача Redmine, к которой относится событие.

    Храним задачу целиком, а не отдельные поля: шаблонизатору и
    роутингу «проект → чат MAX» нужен полный контекст (project,
    assigned_to, priority, custom_fields и т.д.), и лучше передать
    единый объект, чем копировать половину полей в каждое событие.
    """


class NewIssueEvent(EventBase):
    """Создана новая задача.

    Детектируется поллером как issue с id > last_seen_issue_id.
    occurred_at заполняется значением issue.created_on.
    """

    event_type: Literal["new_issue"] = "new_issue"


class NameChange(BaseModel):
    """Смена именованного атрибута (статус, приоритет): было → стало.

    Имена уже отрезолвлены поллером (якорь 4.8) — шаблон в Redmine
    не ходит. old опционален: у самой первой смены прежнего значения
    может не быть (импорт, только что созданная задача). new обязателен —
    менять всегда есть на что.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    old: str | None = None
    new: str


class DueDateChange(BaseModel):
    """Смена срока задачи: было → стало.

    Любое из полей может быть None: срок могли впервые поставить
    (old=None) или, наоборот, снять (new=None). Даты календарные, без
    времени и таймзоны — рендерятся как есть (в отличие от created_on,
    который живёт в UTC и требует | dt).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    old: date | None = None
    new: date | None = None


class IssueUpdatedEvent(EventBase):
    """Задачу обновили одной записью журнала.

    Одна journal-запись = одно событие = одно сообщение в чат. Человек
    в Redmine за один заход может сменить статус, приоритет и срок и тут же
    написать комментарий — для читателя это единый факт «задачу обновили»,
    а не четыре отдельных пинга (решение Leo, замена прежних двух событий
    status_changed + comment_added).

    Любое из изменений опционально; заполнены только те, что реально
    произошли. Валидатор требует хоть одно — пустая запись (сменили только
    то, что мы не показываем, например assigned_to) события не порождает.

    Приватность (private_notes) глушит notes и attachments, но НЕ смены
    атрибутов: Redmine показывает details приватной записи всем, скрыт
    только текст заметки. Поэтому приватная запись со сменой статуса
    доедет — просто без комментария.
    """

    event_type: Literal["issue_updated"] = "issue_updated"
    journal_id: int
    author: NamedRef

    status_change: NameChange | None = None
    priority_change: NameChange | None = None
    due_date_change: DueDateChange | None = None

    notes: str = ""
    """Текст комментария. Пустой — если комментария не было или он приватный."""

    attachments: list[str] = Field(default_factory=list)
    """Имена прикреплённых файлов. Только имена, без ссылок: content_url
    в Redmine требует авторизации, которая у людей в чате может не сработать."""

    @model_validator(mode="after")
    def _require_something(self) -> IssueUpdatedEvent:
        """Событие обязано нести хоть одно изменение.

        Иначе в чат уедет пустое «задача обновлена» без единой строки —
        это баг детектора, и поймать его ValidationError'ом при сборке
        лучше, чем отправить пустышку.
        """
        if not (
            self.status_change
            or self.priority_change
            or self.due_date_change
            or self.notes
            or self.attachments
        ):
            raise ValueError(
                "issue_updated требует хотя бы одно изменение "
                "(статус/приоритет/срок/комментарий/вложение)"
            )
        return self


class DueDateApproachingEvent(EventBase):
    """До due_date задачи осталось N дней (или срок уже прошёл).

    Вычисляемое событие, не привязано к записи журнала. Генерируется
    отдельным заданием APScheduler (Этап 7) раз в сутки.

    days_before может быть:
    - положительным: до срока осталось N дней (порог сработал);
    - 0: срок сегодня;
    - отрицательным: задача просрочена на |N| дней.
    """

    event_type: Literal["due_date_approaching"] = "due_date_approaching"
    days_before: int


# ── Discriminated union ─────────────────────────────────────────────────
# type X = ... — PEP 695, современный синтаксис type alias (Python 3.12+).
# «Событие» с точки зрения обработчиков — это любой из трёх типов.
type Event = NewIssueEvent | IssueUpdatedEvent | DueDateApproachingEvent


# TypeAdapter — «шлюз» для парсинга произвольных dict/JSON в Union-тип.
# Обычная BaseModel умеет валидировать себя (Model.model_validate(data)),
# но у Union нет метода model_validate — нужен TypeAdapter.
#
# Annotated[Event, Field(discriminator="event_type")] говорит Pydantic:
# «смотри на поле event_type входного словаря и по нему выбирай нужный
# класс, без перебора всех вариантов». Это и есть discriminated union.
#
# Использование:
#     event = EventAdapter.validate_python({"event_type": "new_issue", ...})
#     # тип event — Event; isinstance(event, NewIssueEvent) == True
EventAdapter: TypeAdapter[Event] = TypeAdapter(
    Annotated[Event, Field(discriminator="event_type")]
)
