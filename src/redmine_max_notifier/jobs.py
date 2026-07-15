"""Фоновые задания поллера — то, что реально дёргает APScheduler.

Здесь сходится всё, написанное на 7c-7e: курсор из БД → детектор
событий → рассылка → новый курсор в БД. Сама по себе логика тонкая,
это именно оркестрация.

Зависимости передаются через PollerDeps, а не берутся из глобалов:
job'у нужны шесть объектов, живущих столько же, сколько приложение
(клиенты, резолвер, рендерер, фабрика сессий), и собирает их lifespan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.dispatcher import dispatch_events
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.poller import poll_recent_changes
from redmine_max_notifier.polling_state import load_cursor, save_cursor
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.renderer import MessageRenderer
from redmine_max_notifier.status_resolver import StatusResolver

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollerDeps:
    """Долгоживущие зависимости поллер-job'а.

    Всё это создаётся один раз в lifespan и переиспользуется каждым
    тиком: пересоздавать HTTP-клиент раз в минуту — значит каждый раз
    заново поднимать TLS-сессию, а резолвер вдобавок терял бы кэш
    статусов, ради которого он и написан.
    """

    client: RedmineClient
    resolver: StatusResolver
    renderer: MessageRenderer
    max_client: MaxClient
    session_factory: async_sessionmaker[AsyncSession]
    lookback: timedelta


async def run_poll_cycle(deps: PollerDeps) -> None:
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
                deps.resolver,
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
