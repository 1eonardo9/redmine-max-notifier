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
from redmine_max_notifier.redmine.models import Issue
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
    coexecutors_field_id: int,
) -> int:
    """Разослать события по чатам и отметить отправленные.

    Args:
        events: События из детектора (7d), уже отсортированные по времени.
        session: Сессия БД. Диспетчер коммитит в неё сам — см. ниже.
        renderer: Рендерер markdown-сообщений.
        max_client: Клиент MAX.
        coexecutors_field_id: Id кастомного поля «Соисполнители»
            (Settings.coexecutors_field_id) — по нему из issue.custom_fields
            достаются Redmine-id для @упоминаний.

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

        mentions, coexecutors = await _resolve_mentions(
            session, event, coexecutors_field_id=coexecutors_field_id
        )

        if await _deliver(event, chat_ids, renderer, max_client, mentions, coexecutors):
            await mark_sent(session, event)
            await session.commit()
            sent_count += 1

    return sent_count


def _coexecutor_ids(issue: Issue, field_id: int) -> list[int]:
    """Redmine-id соисполнителей из кастомного поля задачи.

    Поле формата user с multiple=true отдаёт value списком id-строк
    (["5", "11"]), но CustomField.value допускает и одиночную строку,
    и None — нормализуем всё к списку. Нечисловое значение пропускаем
    с warning'ом: это не наш кейс (формат user хранит только id), и
    если такое пришло — поменялся сам Redmine, надо знать.

    Нет поля с таким id или оно пусто — соисполнителей нет, [].
    """
    for field in issue.custom_fields:
        if field.id != field_id:
            continue
        if field.value is None:
            return []
        values = field.value if isinstance(field.value, list) else [field.value]
        ids: list[int] = []
        for raw in values:
            try:
                ids.append(int(raw))
            except ValueError:
                log.warning(
                    "задача #%d: значение %r в поле соисполнителей (id=%d) "
                    "не похоже на Redmine-id — пропуск",
                    issue.id,
                    raw,
                    field_id,
                )
        return ids
    return []


async def _resolve_mentions(
    session: AsyncSession,
    event: Event,
    *,
    coexecutors_field_id: int,
) -> tuple[list[str], list[str]]:
    """Собрать @упоминания: (исполнитель, соисполнители).

    Кого пинговать — свойство доставки, а не факт из Redmine, поэтому
    резолвим здесь, а не в детекторе: маппинг лежит в БД, а детектор
    мы держим чистым от неё (7d).

    Два списка, а не один: шаблон показывает их разными строками
    (пинг исполнителя против «👥 Соисполнители:»), сливать их значит
    лишить рендерер возможности различить.

    Дедуп — по MAX user_id: один человек упоминается один раз, каким бы
    путём он ни пришёл. Ловит и «исполнитель сам в соисполнителях», и
    «два Redmine-юзера смаплены на одного MAX-юзера». Приоритет у роли
    исполнителя — дубль выпадает из списка соисполнителей.

    Пустые списки — норма: человека не сопоставили с MAX (или не
    сопоставят никогда — подрядчик, уволился), уведомление уйдёт без
    пинга. Молчать про это в логах тоже правильно: иначе каждый цикл
    сыпал бы warning'ами про одних и тех же людей.
    """
    assignee = event.issue.assigned_to
    assignee_users = (
        await list_max_users_for_redmine(session, assignee.id) if assignee else []
    )
    seen = {u.user_id for u in assignee_users}

    coexecutor_mentions: list[str] = []
    for redmine_id in _coexecutor_ids(event.issue, coexecutors_field_id):
        for user in await list_max_users_for_redmine(session, redmine_id):
            if user.user_id in seen:
                continue
            seen.add(user.user_id)
            coexecutor_mentions.append(format_mention(user.user_id, user.name))

    return (
        [format_mention(u.user_id, u.name) for u in assignee_users],
        coexecutor_mentions,
    )


async def _deliver(
    event: Event,
    chat_ids: list[int],
    renderer: MessageRenderer,
    max_client: MaxClient,
    mentions: Sequence[str] = (),
    coexecutors: Sequence[str] = (),
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
    text = renderer.render(event, mentions=mentions, coexecutors=coexecutors)
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
