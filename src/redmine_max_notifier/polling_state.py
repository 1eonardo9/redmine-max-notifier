"""Чтение и запись курсора поллера (таблица polling_state).

Мост между ORM-моделью PollingState и чистым PollCursor из poller.py:
детектор не знает про SQLAlchemy, БД не знает про детектор, а стыкует
их этот модуль.

Таблица логически синглтон — одна строка с id=1 на всё приложение.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from redmine_max_notifier.db.models import PollingState
from redmine_max_notifier.poller import PollCursor

# id единственной строки состояния. Не автоинкремент — задаём явно.
POLLING_STATE_ID = 1


def _ensure_aware(value: datetime | None) -> datetime | None:
    """Привести время из БД к timezone-aware UTC.

    Зачем. Колонка last_check_at объявлена без DateTime(timezone=True)
    (в отличие от sent_notifications.sent_at), да и SQLite всё равно
    не хранит смещение — DATETIME там обычная строка. Значит из БД
    время приезжает naive.

    А poll_recent_changes сравнивает окно с created_on из Redmine,
    который aware. Сравнение naive с aware — TypeError, причём первый
    цикл прошёл бы нормально (курсор пустой, время берётся из now),
    и упало бы только на втором. Такое ловить в проде — сомнительное
    удовольствие.

    Пишем мы туда всегда UTC (см. run_poll_cycle), поэтому naive-время
    из БД трактуем как UTC — это не догадка, а обратная сторона нашего
    же инварианта.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def load_cursor(session: AsyncSession) -> PollCursor:
    """Прочитать курсор из БД.

    Если строки ещё нет (первый запуск сервиса) — вернуть пустой курсор.
    Пустой last_check_at означает холодный старт: детектор поставит
    baseline и не отправит ничего (см. poller.PollCursor.is_cold_start).
    """
    state = await session.get(PollingState, POLLING_STATE_ID)
    if state is None:
        return PollCursor()

    return PollCursor(
        last_seen_issue_id=state.last_seen_issue_id,
        last_seen_journal_id=state.last_seen_journal_id,
        last_check_at=_ensure_aware(state.last_check_at),
    )


async def save_cursor(session: AsyncSession, cursor: PollCursor) -> None:
    """Сохранить курсор.

    Строку создаём при первом сохранении — на чистой БД её нет.
    Commit оставляем вызывающему: курсор обязан фиксироваться в той же
    транзакции, что и остальная работа цикла.
    """
    state = await session.get(PollingState, POLLING_STATE_ID)
    if state is None:
        state = PollingState(id=POLLING_STATE_ID)
        session.add(state)

    state.last_seen_issue_id = cursor.last_seen_issue_id
    state.last_seen_journal_id = cursor.last_seen_journal_id
    state.last_check_at = cursor.last_check_at
    await session.flush()
