"""CRUD-операции над таблицей sent_notifications — идемпотентность рассылки.

Поллер по построению видит одно и то же событие несколько раз: окно
updated_on берётся с запасом lookback, а цикл крутится чаще, чем это
окно, — соседние опросы перекрываются. Плюс рестарты сервиса, при
которых курсор мог не успеть сдвинуться. Эта таблица — журнал «что уже
улетело в чат», второй рубеж после id-курсора из 7d.

Стиль — как в routing.py: свободные функции, session первым аргументом,
никаких классов-репозиториев.

Транзакциями, в отличие от routing.py, управляет НЕ вызывающий, а
диспетчер, и коммитит он после каждого события — см. dispatcher.py,
там же обоснование.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from redmine_max_notifier.db.models import SentNotification
from redmine_max_notifier.events.models import (
    DueDateApproachingEvent,
    Event,
    IssueUpdatedEvent,
)


def journal_id_of(event: Event) -> int | None:
    """Достать journal_id события, если он у события вообще есть.

    Дискриминированный union: journal_id живёт только у событий из
    журнала. У new_issue и due_date_approaching записи журнала нет —
    для них ключ дедупликации содержит NULL, и это важно (см. ниже).

    isinstance по union'у, а не getattr(event, "journal_id", None):
    getattr вернул бы Any и молча проглотил бы опечатку в имени поля.
    """
    if isinstance(event, IssueUpdatedEvent):
        return event.journal_id
    return None


def notified_due_date_of(event: Event) -> date | None:
    """Срок, о котором напоминаем, — только для событий про дедлайн.

    Часть ключа дедупликации: без него напоминание уходило бы один раз
    за жизнь задачи, и сдвиг срока сервис бы проигнорировал.
    """
    if isinstance(event, DueDateApproachingEvent):
        return event.issue.due_date
    return None


async def is_already_sent(session: AsyncSession, event: Event) -> bool:
    """Мы про этот факт уже писали в чат?

    Ключ — (event_type, issue_id, journal_id), тот же, что в
    UniqueConstraint таблицы. Но на констрейнт здесь полагаться НЕЛЬЗЯ
    (якорь 4.10): для new_issue journal_id = NULL, а по SQL-стандарту
    NULL != NULL, поэтому UNIQUE пропустит хоть десять одинаковых
    строк. Отсюда этот SELECT — для событий без журнала он единственная
    защита от дубля, БД не подстрахует.

    Ветка .is_(None) написана явно, хотя SQLAlchemy и сам превращает
    `column == None` в IS NULL: полагаться на перегрузку оператора при
    None-значении переменной — значит требовать от читателя знания этой
    магии, чтобы понять, работает ли дедуп new_issue вообще.

    Для due_date_approaching в ключ добавляется ещё и сам срок, иначе
    напоминание ушло бы один раз за жизнь задачи и сдвиг срока остался
    бы незамеченным (см. notified_due_date в db/models.py).
    """
    journal_id = journal_id_of(event)

    stmt = select(SentNotification.id).where(
        SentNotification.event_type == event.event_type,
        SentNotification.issue_id == event.issue.id,
    )
    stmt = stmt.where(
        SentNotification.journal_id.is_(None)
        if journal_id is None
        else SentNotification.journal_id == journal_id
    )

    due_date = notified_due_date_of(event)
    if due_date is not None:
        stmt = stmt.where(SentNotification.notified_due_date == due_date)

    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none() is not None


async def mark_sent(session: AsyncSession, event: Event) -> SentNotification:
    """Записать факт «событие отправлено».

    Вызывается ПОСЛЕ успешной отправки в MAX, а не до (at-least-once):
    упасть между отправкой и отметкой — значит получить дубль в чате
    при рестарте. Отметить до отправки и упасть — значит потерять
    уведомление навсегда, о чём никто никогда не узнает. Дубль
    раздражает, потеря вредит.

    sent_at не проставляем — за него отвечает server_default=func.now()
    на уровне БД.
    """
    notification = SentNotification(
        event_type=event.event_type,
        issue_id=event.issue.id,
        journal_id=journal_id_of(event),
        notified_due_date=notified_due_date_of(event),
    )
    session.add(notification)
    # flush отправляет INSERT в БД, но не коммитит: id становится
    # доступен сразу, а решение "фиксировать или откатить" остаётся
    # за диспетчером.
    await session.flush()
    return notification
