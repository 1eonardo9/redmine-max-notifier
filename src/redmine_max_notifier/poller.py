"""Детектор изменений в Redmine — чистое ядро поллера.

Функция poll_recent_changes принимает курсор («где мы остановились») и
возвращает список доменных событий плюс новый курсор. Ни БД, ни отправки
в MAX, ни APScheduler здесь нет: состояние приходит аргументом и уходит
результатом. Такую функцию можно тестировать как чистую — вход/выход,
без поднятия половины приложения. Связка с PollingState и рассылкой —
подэтапы 7e/7f.

Как детектируем (решение подэтапа 7d):

- Окно updated_on нужно только чтобы ВЫБРАТЬ задачи. Иначе никак:
  эндпоинта /journals.json в Redmine нет, записи журнала достаются
  только через карточку конкретной задачи (см. _fetch_updated_issues).
- Что из выбранного действительно новое, решают id: issue.id и
  journal.id в Redmine глобально монотонны, поэтому "id больше
  виденного" не зависит от часов, репликации и часовых поясов.

Окно поэтому можно делать сколь угодно щедрым (polling_lookback_seconds),
а дубли режутся id'шниками и, вторым рубежом, идемпотентностью на 7e.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from redmine_max_notifier.events.models import (
    DueDateChange,
    Event,
    IssueUpdatedEvent,
    NameChange,
    NewIssueEvent,
)
from redmine_max_notifier.name_resolver import NameResolver
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.redmine.models import Issue, Journal

log = logging.getLogger(__name__)

# Формат фильтра дат Redmine: updated_on=">=2026-07-15T10:00:00Z".
_REDMINE_DT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class PollCursor:
    """Отметка «до какого места мы уже разобрали Redmine».

    Зеркалит ORM-модель PollingState, но намеренно от неё отвязан:
    детектор не должен зависеть от SQLAlchemy, а тест — поднимать БД
    ради проверки классификации. Маппинг PollingState <-> PollCursor
    делает job на 7f.

    Обычный dataclass, а не Pydantic-модель: курсор не парсится из
    внешнего JSON, валидировать нечего — Pydantic был бы лишним весом.
    frozen=True по той же причине, что и у событий: случайная мутация
    курсора в середине цикла — это тихо потерянные уведомления.

    Все поля опциональны: на первом запуске сервиса строки в БД нет.
    """

    last_seen_issue_id: int | None = None
    last_seen_journal_id: int | None = None
    last_check_at: datetime | None = None

    @property
    def is_cold_start(self) -> bool:
        """Сервис ещё ни разу не отработал цикл поллинга?

        Признак — именно last_check_at, а не пустые last_seen_*.
        Если Redmine тихий и в окно не попало ни одной задачи, максимумы
        id вычислить не из чего и они останутся None — но цикл-то был.
        Определяй мы холодный старт по ним, сервис навсегда залип бы
        в режиме «первый запуск» и не отправил бы ни одного уведомления.
        last_check_at проставляется всегда.
        """
        return self.last_check_at is None


async def poll_recent_changes(
    client: RedmineClient,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    cursor: PollCursor,
    *,
    lookback: timedelta,
    now: datetime,
) -> tuple[list[Event], PollCursor]:
    """Найти изменения в Redmine со времён курсора.

    Args:
        client: Клиент Redmine.
        status_resolver: Резолвер имён статусов (id → имя).
        priority_resolver: Резолвер имён приоритетов (id → имя). Оба —
            якорь 4.8: имена подставляет поллер, шаблон в Redmine не ходит.
        cursor: Где остановились в прошлый раз.
        lookback: На сколько расширить окно назад от last_check_at.
            Страхует от рассинхрона часов и задержек БД Redmine:
            пропущенное событие не вернуть, а лишнее отсечёт фильтр по id.
        now: Текущее время (aware, UTC). Передаётся аргументом, а не
            берётся внутри, чтобы функция осталась чистой и тестируемой.

    Returns:
        (события, новый курсор). События отсортированы по времени —
        в чат они должны попадать в том порядке, в котором происходили.

    Raises:
        ValueError: если now — naive datetime.
        RedmineError: ошибки клиента прокидываются наружу.
    """
    if now.tzinfo is None:
        raise ValueError("now должен быть aware datetime (UTC), получен naive")

    # Окно: от прошлой проверки минус запас. На холодном старте прошлой
    # проверки нет — берём то же окно от now, оно нужно лишь чтобы
    # нащупать максимумы id для baseline.
    window_start = (cursor.last_check_at or now) - lookback

    issues = await _fetch_updated_issues(client, window_start)

    # Максимумы считаем ДО фильтрации: baseline холодного старта должен
    # учесть всё, что мы видели, иначе первое же уведомление уедет
    # в повтор на следующем цикле.
    new_cursor = _advance_cursor(cursor, issues, now)

    if cursor.is_cold_start:
        # Первый запуск: только ставим baseline, событий не шлём.
        # Иначе сервис на старте вывалит в чат всё, что накопилось
        # за окно, — а после долгого простоя это может быть сотня задач.
        log.info(
            "холодный старт: baseline issue_id=%s journal_id=%s, "
            "уведомления не отправляются",
            new_cursor.last_seen_issue_id,
            new_cursor.last_seen_journal_id,
        )
        return [], new_cursor

    events = await _detect_events(
        issues, cursor, status_resolver, priority_resolver, window_start
    )
    events.sort(key=lambda e: e.occurred_at)

    log.info("цикл поллинга: задач в окне %d, событий %d", len(issues), len(events))
    return events, new_cursor


async def _fetch_updated_issues(
    client: RedmineClient,
    window_start: datetime,
) -> list[Issue]:
    """Забрать задачи, обновлённые начиная с window_start, вместе с журналами.

    Два запроса вместо одного, и это не расточительность, а единственный
    рабочий способ. Redmine поддерживает include=journals ТОЛЬКО для
    одиночной задачи (/issues/{id}.json). В списке (/issues.json) параметр
    молча игнорируется: ни ошибки, ни предупреждения — просто в ответе нет
    поля journals. Проверено на живом Redmine 6.x (этап 7h).

    Отсюда N+1: сначала узнаём, КАКИЕ задачи менялись, потом забираем
    журналы поштучно. Эндпоинта "журналы пачкой" в API нет, обойти нельзя.
    На практике N мал: за минутное окно меняется 0-3 задачи.
    """
    since = window_start.astimezone(UTC).strftime(_REDMINE_DT_FORMAT)

    # Шаг 1: какие задачи трогали. include не просим — бесполезен (см. выше).
    changed = [
        issue
        async for issue in client.list_issues(
            updated_on=f">={since}",
            sort="updated_on:asc",
            status_id="*",  # иначе Redmine молча отдаст только открытые
        )
    ]

    # Шаг 2: журналы поштучно.
    issues: list[Issue] = []
    for issue in changed:
        if issue.is_private:
            # Приватную задачу спрятали от посторонних — значит и факт
            # её существования не для общего чата проекта. Пропускаем
            # целиком: ни new_issue, ни события из её журнала.
            # Вторая линия обороны: сервисный юзер Redmine не должен
            # иметь прав на приватные задачи (см. README).
            log.debug("задача #%d приватная — пропуск", issue.id)
            continue

        issues.append(await client.get_issue(issue.id, include=["journals"]))

    return issues


def _advance_cursor(
    cursor: PollCursor,
    issues: list[Issue],
    now: datetime,
) -> PollCursor:
    """Сдвинуть курсор по максимумам id среди увиденных задач.

    Если окно оказалось пустым — прежние максимумы сохраняем: пустой
    ответ означает «ничего не изменилось», а не «всё пропало».
    """
    issue_ids = [i.id for i in issues]
    journal_ids = [j.id for i in issues for j in i.journals]

    return PollCursor(
        last_seen_issue_id=max(issue_ids, default=cursor.last_seen_issue_id),
        last_seen_journal_id=max(journal_ids, default=cursor.last_seen_journal_id),
        last_check_at=now,
    )


def _is_new(
    entity_id: int,
    created_on: datetime,
    last_seen_id: int | None,
    window_start: datetime,
) -> bool:
    """Сущность (задача или запись журнала) новая для нас?

    Основной критерий — id: они монотонны, часы ни при чём.

    Фолбэк на created_on нужен для случая «курсор без максимумов»:
    холодный старт при пустом окне выставил last_check_at, но
    last_seen_* оставил None. Тогда «больше виденного» сравнивать не с
    чем, а брать всё подряд нельзя — задача могла быть создана год назад
    и попасть в окно из-за свежего обновления.
    """
    if last_seen_id is not None:
        return entity_id > last_seen_id
    return created_on >= window_start


async def _detect_events(
    issues: list[Issue],
    cursor: PollCursor,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
    window_start: datetime,
) -> list[Event]:
    """Классифицировать выбранные задачи в доменные события."""
    events: list[Event] = []

    for issue in issues:
        if _is_new(issue.id, issue.created_on, cursor.last_seen_issue_id, window_start):
            events.append(NewIssueEvent(occurred_at=issue.created_on, issue=issue))

        for journal in issue.journals:
            if not _is_new(
                journal.id,
                journal.created_on,
                cursor.last_seen_journal_id,
                window_start,
            ):
                continue
            event = await _events_from_journal(
                issue, journal, status_resolver, priority_resolver
            )
            if event is not None:
                events.append(event)

    return events


async def _events_from_journal(
    issue: Issue,
    journal: Journal,
    status_resolver: NameResolver,
    priority_resolver: NameResolver,
) -> IssueUpdatedEvent | None:
    """Разобрать одну запись журнала в одно событие «задача обновлена».

    Одна journal-запись = одно событие = одно сообщение. Статус, приоритет,
    срок и комментарий, сделанные человеком одним действием, — единый факт
    для читателя чата, а не несколько отдельных пингов (решение Leo).

    Returns:
        IssueUpdatedEvent, либо None, если показывать нечего: запись сменила
        только то, что мы не отображаем (например assigned_to), либо несла
        лишь приватную заметку.
    """
    status_change = await _attr_name_change(
        journal, status_resolver, attr="status_id", label="статус"
    )
    priority_change = await _attr_name_change(
        journal, priority_resolver, attr="priority_id", label="приоритет"
    )
    due_date_change = _due_date_change(journal)

    # private_notes прячет заметку целиком — и текст, и приложенные к ней
    # файлы (имя файла выдаёт содержание не хуже текста, "договор_с_ценами.pdf").
    # Но смены атрибутов Redmine показывает в details приватной записи всем —
    # их не глушим: приватен именно комментарий.
    if journal.private_notes:
        notes = ""
        attachments: list[str] = []
    else:
        notes = journal.notes or ""
        # Прикрепление файла — details, а не notes: property="attachment",
        # new_value=<имя>. Удаление выглядит так же, но имя в old_value —
        # фильтр по new_value, иначе "удалил схему" приехало бы как "приложил".
        attachments = [
            d.new_value
            for d in journal.details
            if d.property == "attachment" and d.new_value
        ]

    # Ничего показываемого — события нет. Дублирует валидатор
    # IssueUpdatedEvent, но здесь мы решаем «слать или нет», а не «падать».
    if not (
        status_change or priority_change or due_date_change or notes or attachments
    ):
        return None

    return IssueUpdatedEvent(
        occurred_at=journal.created_on,
        issue=issue,
        journal_id=journal.id,
        author=journal.user,
        status_change=status_change,
        priority_change=priority_change,
        due_date_change=due_date_change,
        notes=notes,
        attachments=attachments,
    )


async def _attr_name_change(
    journal: Journal,
    resolver: NameResolver,
    *,
    attr: str,
    label: str,
) -> NameChange | None:
    """Собрать NameChange из details журнала для атрибута attr (id → имя).

    Общая логика для статуса и приоритета: Redmine пишет оба как
    property="attr" с old_value/new_value в виде id-строк, а имена
    подставляет резолвер (якорь 4.8).

    Returns:
        None, если атрибут в этой записи не менялся, либо если change
        собрать нельзя (битый id, элемент удалён из Redmine). Про удалённый
        элемент пишем warning, но не падаем — прочие изменения должны уехать.
    """
    detail = next(
        (d for d in journal.details if d.property == "attr" and d.name == attr),
        None,
    )
    if detail is None:
        return None

    new_id = _parse_int(detail.new_value)
    if new_id is None:
        log.warning(
            "journal #%d: нераспознанный new_value (%s) %r — пропуск",
            journal.id,
            label,
            detail.new_value,
        )
        return None

    new_name = await resolver.resolve(new_id)
    if new_name is None:
        # Элемент удалили из Redmine уже после того, как он попал в журнал.
        # Слать "приоритет изменён на #3" бессмысленно, заглушка — враньё.
        log.warning(
            "journal #%d: %s id=%d не найден в Redmine — пропуск",
            journal.id,
            label,
            new_id,
        )
        return None

    # Старое значение опционально: у первой смены прежнего может не быть.
    old_id = _parse_int(detail.old_value)
    old_name = await resolver.resolve(old_id) if old_id is not None else None

    return NameChange(old=old_name, new=new_name)


def _due_date_change(journal: Journal) -> DueDateChange | None:
    """Собрать DueDateChange из details журнала.

    due_date Redmine пишет как property="attr" name="due_date",
    old_value/new_value — календарные даты строками ("2026-07-17") либо
    null (срок сняли или впервые поставили). Резолвить нечего — даты как есть.
    """
    detail = next(
        (d for d in journal.details if d.property == "attr" and d.name == "due_date"),
        None,
    )
    if detail is None:
        return None

    old = _parse_date(detail.old_value)
    new = _parse_date(detail.new_value)
    if old is None and new is None:
        # Оба пустых Redmine для смены срока не отдаёт, но подстрахуемся.
        return None

    return DueDateChange(old=old, new=new)


def _parse_int(raw: str | None) -> int | None:
    """Привести id из журнала к int.

    Redmine отдаёт old_value/new_value строками ("2"), иногда null —
    поэтому не int(raw) в лоб.
    """
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_date(raw: str | None) -> date | None:
    """Привести дату из журнала к date. Пустое/битое → None."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None
