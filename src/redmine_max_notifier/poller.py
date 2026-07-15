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
from datetime import UTC, datetime, timedelta

from redmine_max_notifier.events.models import (
    CommentAddedEvent,
    Event,
    NewIssueEvent,
    StatusChangedEvent,
)
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.redmine.models import Issue, Journal, JournalDetail
from redmine_max_notifier.status_resolver import StatusResolver

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
    resolver: StatusResolver,
    cursor: PollCursor,
    *,
    lookback: timedelta,
    now: datetime,
) -> tuple[list[Event], PollCursor]:
    """Найти изменения в Redmine со времён курсора.

    Args:
        client: Клиент Redmine.
        resolver: Резолвер имён статусов (см. якорь 4.8 — имена статусов
            подставляет поллер, шаблонизатор в Redmine не ходит).
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

    events = await _detect_events(issues, cursor, resolver, window_start)
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
    resolver: StatusResolver,
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
            events.extend(await _events_from_journal(issue, journal, resolver))

    return events


async def _events_from_journal(
    issue: Issue,
    journal: Journal,
    resolver: StatusResolver,
) -> list[Event]:
    """Разобрать запись журнала в события.

    Одна запись может дать сразу два события: человек в Redmine меняет
    статус и пишет комментарий одним действием, а для чата это два разных
    факта («задача решена» и «вот что сделано»).
    """
    events: list[Event] = []

    status_detail = next(
        (d for d in journal.details if d.property == "attr" and d.name == "status_id"),
        None,
    )
    if status_detail is not None:
        status_event = await _build_status_event(
            issue, journal, status_detail, resolver
        )
        if status_event is not None:
            events.append(status_event)

    # private_notes прячет ТОЛЬКО текст заметки — изменения атрибутов
    # из той же записи Redmine показывает всем. Поэтому фильтр висит
    # на комментарии, а не на записи целиком: приватный текст в общий
    # чат проекта не уедет, а смена статуса рядом с ним — придёт.
    if journal.notes and not journal.private_notes:
        events.append(
            CommentAddedEvent(
                occurred_at=journal.created_on,
                issue=issue,
                journal_id=journal.id,
                notes=journal.notes,
                author=journal.user,
            )
        )

    return events


async def _build_status_event(
    issue: Issue,
    journal: Journal,
    detail: JournalDetail,
    resolver: StatusResolver,
) -> StatusChangedEvent | None:
    """Собрать StatusChangedEvent из detail'а журнала.

    Returns:
        None, если событие собрать нельзя (битый id, статус удалён из
        Redmine). Молчать про такое нельзя — пишем warning, — но и падать
        незачем: остальные события цикла должны уехать в чат.
    """
    new_status_id = _parse_status_id(detail.new_value)
    if new_status_id is None:
        log.warning(
            "journal #%d (задача #%d): нераспознанный new_value статуса %r — пропуск",
            journal.id,
            issue.id,
            detail.new_value,
        )
        return None

    new_status_name = await resolver.resolve(new_status_id)
    if new_status_name is None:
        # Статус удалили из Redmine уже после того, как он попал в журнал.
        # Слать в чат "статус изменён на #8" бессмысленно, а придумывать
        # заглушку — врать. Пропускаем, warning покажет рассинхрон.
        log.warning(
            "journal #%d (задача #%d): статус id=%d не найден в Redmine — пропуск",
            journal.id,
            issue.id,
            new_status_id,
        )
        return None

    # Старый статус опционален: у первой смены (например, при импорте)
    # прежнего значения может не быть, и это нормально — модель события
    # допускает None.
    old_status_id = _parse_status_id(detail.old_value)
    old_status_name = (
        await resolver.resolve(old_status_id) if old_status_id is not None else None
    )

    return StatusChangedEvent(
        occurred_at=journal.created_on,
        issue=issue,
        journal_id=journal.id,
        old_status_id=old_status_id,
        old_status_name=old_status_name,
        new_status_id=new_status_id,
        new_status_name=new_status_name,
        changed_by=journal.user,
    )


def _parse_status_id(raw: str | None) -> int | None:
    """Привести id статуса из журнала к int.

    Redmine отдаёт old_value/new_value строками ("2"), а иногда и null —
    поэтому не int(raw) в лоб.
    """
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
