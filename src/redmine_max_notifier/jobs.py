"""Фоновые задания — то, что реально дёргает APScheduler.

Здесь сходится всё, написанное на 7c-7e:

- run_poll_cycle — раз в минуту: курсор из БД → детектор событий →
  рассылка → новый курсор в БД;
- run_due_date_cycle — раз в сутки: задачи с подходящим дедлайном →
  рассылка.

Своей логики тут почти нет, это оркестрация.

Зависимости передаются через JobDeps, а не берутся из глобалов:
job'ам нужны объекты, живущие столько же, сколько приложение
(клиенты, резолвер, рендерер, фабрика сессий), и собирает их lifespan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.dispatcher import dispatch_events
from redmine_max_notifier.events.models import DueDateApproachingEvent, Event
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.name_resolver import NameResolver
from redmine_max_notifier.poller import poll_recent_changes
from redmine_max_notifier.polling_state import load_cursor, save_cursor
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.renderer import MessageRenderer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobDeps:
    """Долгоживущие зависимости фоновых заданий.

    Всё это создаётся один раз в lifespan и переиспользуется каждым
    тиком: пересоздавать HTTP-клиент раз в минуту — значит каждый раз
    заново поднимать TLS-сессию, а резолвер вдобавок терял бы кэш
    статусов, ради которого он и написан.

    Один контейнер на оба job'а, хотя каждому нужно не всё (дедлайнам
    не нужен резолвер статусов, поллингу — порог дедлайна): это
    зависимости приложения, а не аргументы функции. Два почти
    одинаковых dataclass'а разъехались бы при первой же правке.
    """

    client: RedmineClient
    status_resolver: NameResolver
    priority_resolver: NameResolver
    renderer: MessageRenderer
    max_client: MaxClient
    session_factory: async_sessionmaker[AsyncSession]
    lookback: timedelta
    due_date_threshold_days: int
    tz: ZoneInfo
    """Бизнес-таймзона: в ней решается, какой сегодня день при сравнении
    с due_date. Явная, а не из ОС, — см. Settings.timezone."""


async def run_poll_cycle(deps: JobDeps) -> None:
    """Один цикл поллинга: найти изменения и разослать их.

    Исключения наружу не выпускает. APScheduler переживёт упавший job
    и вызовет следующий по расписанию, но в логе это выглядит как
    «Job raised an exception» без внятного контекста. Redmine лежит,
    сеть моргнула, MAX отдал 500 — для фонового цикла это ожидаемые
    события, а не повод шуметь трейсбеком: следующий тик через минуту
    попробует снова, а курсор не сдвинулся, поэтому ничего не потеряно.

    Порядок внутри важен:
      1. курсор читаем ДО детектора — он определяет окно;
      2. рассылаем события (dispatch коммитит каждое сам);
      3. курсор сохраняем ПОСЛЕ рассылки. Упади мы в середине —
         курсор останется старым, события придут снова, и их отсечёт
         идемпотентность sent_notifications. Сохрани мы курсор первым,
         неотправленные события пропали бы навсегда.
    """
    try:
        async with deps.session_factory() as session:
            cursor = await load_cursor(session)

            events, new_cursor = await poll_recent_changes(
                deps.client,
                deps.status_resolver,
                deps.priority_resolver,
                cursor,
                lookback=deps.lookback,
                now=datetime.now(UTC),
            )

            if events:
                await dispatch_events(
                    events,
                    session=session,
                    renderer=deps.renderer,
                    max_client=deps.max_client,
                )

            await save_cursor(session, new_cursor)
            await session.commit()
    except Exception:
        # Голый Exception — осознанно. Это верхний фрейм фоновой задачи:
        # любое исключение, вылетевшее отсюда, всё равно будет проглочено
        # APScheduler'ом, только уже без нашего контекста.
        log.exception("цикл поллинга завершился ошибкой")


async def run_due_date_cycle(deps: JobDeps) -> None:
    """Напомнить о задачах, у которых поджимает дедлайн.

    Запускается раз в сутки (см. due_date_job_hour). В отличие от
    поллинга здесь нет курсора: событие вычисляемое, а не найденное
    в журнале, — «до дедлайна осталось N дней» истинно каждый день,
    пока задача открыта.

    От ежедневного спама защищает идемпотентность: ключ дедупликации
    включает сам срок (см. notified_due_date), поэтому напоминание
    уходит один раз на КАЖДОЕ значение due_date. Пока срок не меняется —
    одно напоминание; сдвинули срок — про новый напомним отдельно.

    Просроченные задачи тоже забираем (фильтр <=, а не диапазон):
    шаблон умеет days_before < 0, а дедуп не даст напоминать о них
    вечно.
    """
    try:
        # Дата в бизнес-таймзоне, а не в UTC и не в таймзоне ОС.
        # due_date в Redmine — календарная дата без времени, живущая
        # в часовом поясе людей, которые её ставили. В UTC+3 около
        # полуночи now(UTC).date() отстал бы на сутки, и напоминание
        # уехало бы не в тот день.
        today = datetime.now(deps.tz).date()
        threshold = today + timedelta(days=deps.due_date_threshold_days)
        now = datetime.now(UTC)

        events: list[Event] = []
        async for issue in deps.client.list_issues(
            status_id="open",  # закрытая задача о дедлайне не напоминает
            due_date=f"<={threshold.isoformat()}",
        ):
            if issue.due_date is None:
                # Фильтр Redmine такого не отдаёт, но шаблон зовёт
                # due_date.strftime — пусть лучше пропустим, чем уроним
                # весь цикл на AttributeError.
                continue

            events.append(
                DueDateApproachingEvent(
                    occurred_at=now,
                    issue=issue,
                    days_before=(issue.due_date - today).days,
                )
            )

        if not events:
            log.info("проверка дедлайнов: подходящих задач нет")
            return

        async with deps.session_factory() as session:
            sent = await dispatch_events(
                events,
                session=session,
                renderer=deps.renderer,
                max_client=deps.max_client,
            )

        log.info("проверка дедлайнов: задач %d, напоминаний %d", len(events), sent)
    except Exception:
        log.exception("проверка дедлайнов завершилась ошибкой")
