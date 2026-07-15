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

from datetime import datetime
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


class StatusChangedEvent(EventBase):
    """Изменился статус задачи.

    Детектируется как journal с записью в details, у которой
    name == "status_id". old/new_status_id — сырые id из журнала
    (Redmine отдаёт их как строки, здесь уже сконвертированы в int).

    Имена статусов резолвит поллер через StatusResolver ДО создания
    события (якорь 4.8): событие — самодостаточный факт, шаблонизатор
    в Redmine не ходит. Если имя нового статуса не резолвится (статус
    удалили из Redmine), событие не создаётся вовсе — см. poller.py.

    old_status_id опционален: у самой первой смены статуса
    (например, при импорте) прежнего значения может не быть.
    old_status_name — тем более: старый статус мог быть удалён.
    """

    event_type: Literal["status_changed"] = "status_changed"
    journal_id: int
    old_status_id: int | None = None
    old_status_name: str | None = None
    new_status_id: int
    new_status_name: str
    changed_by: NamedRef


class CommentAddedEvent(EventBase):
    """К задаче добавили комментарий и/или прикрепили файл.

    Детектируется как journal с непустым notes ЛИБО с вложениями.

    Почему одно событие на два случая. Для Redmine прикрепление файла —
    это запись в details, а текст комментария — notes, и человек в UI
    делает это одним действием: пишет пару слов и цепляет схему. Для
    читателя чата это тоже один факт («к задаче добавили информацию»),
    поэтому разводить на два события и два сообщения незачем.
    """

    event_type: Literal["comment_added"] = "comment_added"
    journal_id: int

    notes: str = ""
    """Текст комментария. Может быть пустым: файл прикрепляют и молча."""

    attachments: list[str] = Field(default_factory=list)
    """Имена прикреплённых файлов.

    Только имена, без ссылок: content_url в Redmine требует авторизации,
    и в чате она у людей может не сработать. Имени достаточно, чтобы
    понять, что появилось.
    """

    author: NamedRef

    @model_validator(mode="after")
    def _require_content(self) -> CommentAddedEvent:
        """Событие обязано нести хоть что-то для людей.

        Пустой notes без вложений — это чистая смена атрибутов (статус,
        исполнитель), про которую есть свои события. Такой journal сюда
        доходить не должен, и лучше поймать это ValidationError'ом при
        сборке события, чем отправить в чат пустое сообщение.
        """
        if not self.notes and not self.attachments:
            raise ValueError(
                "comment_added требует либо notes, либо attachments: "
                "пустая запись журнала — это не комментарий"
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
# «Событие» с точки зрения обработчиков — это любой из четырёх типов.
type Event = (
    NewIssueEvent | StatusChangedEvent | CommentAddedEvent | DueDateApproachingEvent
)


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
