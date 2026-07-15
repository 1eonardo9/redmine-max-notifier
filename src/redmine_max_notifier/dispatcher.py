"""Диспетчер отправки: событие → routing → рендер → MAX → отметка.

Собирает воедино куски, написанные на этапах 5-7: доменное событие
превращается в markdown-сообщение и уезжает во все чаты, подписанные
на проект задачи, после чего факт отправки фиксируется в
sent_notifications.

Главный принцип — «одно упавшее событие не роняет цикл». Поллер крутится
раз в минуту в фоне, и любое исключение, вылетевшее отсюда наружу,
убило бы весь батч: остальные события этого цикла не уехали бы, курсор
не сдвинулся, а на следующем цикле всё повторилось бы с тем же
результатом. Поэтому ошибки MAX ловятся здесь и логируются.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from redmine_max_notifier.events.models import Event
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.maxbot.exceptions import MaxError
from redmine_max_notifier.maxbot.models import MessageFormat
from redmine_max_notifier.renderer import MessageRenderer, format_mention
from redmine_max_notifier.routing import list_chats_for_project
from redmine_max_notifier.sent_notifications import is_already_sent, mark_sent
from redmine_max_notifier.user_mapping import list_max_users_for_redmine

log = logging.getLogger(__name__)


async def dispatch_events(
    events: list[Event],
    *,
    session: AsyncSession,
    renderer: MessageRenderer,
    max_client: MaxClient,
) -> int:
    """Разослать события по чатам и отметить отправленные.

    Args:
        events: События из детектора (7d), уже отсортированные по времени.
        session: Сессия БД. Диспетчер коммитит в неё сам — см. ниже.
        renderer: Рендерер markdown-сообщений.
        max_client: Клиент MAX.

    Returns:
        Число реально отправленных событий (без пропущенных дублей и
        событий без роутинга).

    Про транзакции. В routing.py конвенция обратная — «commit делает
    вызывающий», и для CRUD она правильная. Здесь мы от неё осознанно
    отступаем: диспетчер коммитит отметку сразу после каждого события.
    Причина в том, что отправка в MAX необратима. Копи мы отметки до
    конца батча, падение на середине откатило бы их все — а сообщения-то
    уже в чате. Пользователь получил бы их по второму разу на следующем
    цикле. Коммит по событию сужает окно дубля до одного сообщения.
    """
    sent_count = 0

    for event in events:
        if await is_already_sent(session, event):
            log.debug(
                "событие %s (задача #%d) уже отправлено — пропуск",
                event.event_type,
                event.issue.id,
            )
            continue

        project_id = event.issue.project.id
        chat_ids = await list_chats_for_project(session, project_id)
        if not chat_ids:
            # Не ошибка приложения, но почти наверняка ошибка админа:
            # завели проект в Redmine, а routing прописать забыли.
            # Молчать нельзя — иначе уведомления просто не приходят,
            # и никто не понимает почему.
            log.warning(
                "проект #%d (%s): routing не настроен, событие %s "
                "по задаче #%d отправлять некуда",
                project_id,
                event.issue.project.name,
                event.event_type,
                event.issue.id,
            )
            continue

        mentions = await _resolve_mentions(session, event)

        if await _deliver(event, chat_ids, renderer, max_client, mentions):
            await mark_sent(session, event)
            await session.commit()
            sent_count += 1

    return sent_count


async def _resolve_mentions(session: AsyncSession, event: Event) -> list[str]:
    """Собрать @упоминания для исполнителя задачи.

    Кого пинговать — свойство доставки, а не факт из Redmine, поэтому
    резолвим здесь, а не в детекторе: маппинг лежит в БД, а детектор
    мы держим чистым от неё (7d).

    Упоминаем только исполнителя: это тот, кому задача «прилетела».
    Автор и так знает, что создал задачу.

    Пустой список — норма: человека не сопоставили с MAX (или не
    сопоставят никогда — подрядчик, уволился), уведомление уйдёт без
    пинга. Молчать про это в логах тоже правильно: иначе каждый цикл
    сыпал бы warning'ами про одних и тех же людей.
    """
    assignee = event.issue.assigned_to
    if assignee is None:
        return []

    max_users = await list_max_users_for_redmine(session, assignee.id)
    return [format_mention(u.user_id, u.name) for u in max_users]


async def _deliver(
    event: Event,
    chat_ids: list[int],
    renderer: MessageRenderer,
    max_client: MaxClient,
    mentions: Sequence[str] = (),
) -> bool:
    """Отправить событие во все чаты проекта.

    Returns:
        True, если сообщение ушло хотя бы в один чат. False — если
        ни в один: тогда отметку не ставим, и событие повторится
        на следующем цикле, что здесь и требуется.

    Падение отправки в один чат не мешает остальным: чат мог быть
    удалён или бота из него выкинули — это не повод лишать уведомления
    другие чаты того же проекта.
    """
    text = renderer.render(event, mentions=mentions)
    delivered = False

    for chat_id in chat_ids:
        try:
            await max_client.send_message(
                chat_id,
                text,
                format=MessageFormat.MARKDOWN,
            )
        except MaxError as exc:
            log.error(
                "не удалось отправить событие %s (задача #%d) в чат %d: %s",
                event.event_type,
                event.issue.id,
                chat_id,
                exc,
            )
        else:
            delivered = True

    return delivered
