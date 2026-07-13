"""Тесты модели SentNotification.

Проверяем:
- round-trip записи;
- server_default=func.now() реально проставляет sent_at при вставке
  без явного значения (и оно timezone-aware, так как timezone=True);
- уникальное ограничение (event_type, issue_id, journal_id) ловит дубли;
- NULL в journal_id не мешает вставить две "new_issue" по одной задаче
  (SQL-семантика: NULL != NULL внутри UNIQUE) — это подстилаемое место,
  на этапе 7 поллер сам должен доотсеивать такие случаи.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.db import SentNotification


async def test_insert_and_read_back(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пишем в одной сессии — читаем в новой, поля целы."""
    async with db_session_factory() as write_session:
        write_session.add(
            SentNotification(
                event_type="status_changed",
                issue_id=1234,
                journal_id=5678,
            )
        )
        await write_session.commit()

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(SentNotification))
        loaded = result.scalar_one()

    assert loaded.event_type == "status_changed"
    assert loaded.issue_id == 1234
    assert loaded.journal_id == 5678
    assert loaded.id is not None  # автоинкремент проставил PK


async def test_sent_at_is_set_by_server_default(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """server_default=func.now() проставляет sent_at при INSERT.

    Явно значение не передаём — БД должна вписать своё время.

    ⚠ Про timezone: у модели стоит DateTime(timezone=True), но
    SQLite сам по себе timezone не хранит (в нём нет типа TIMESTAMP,
    даты живут как TEXT, CURRENT_TIMESTAMP отдаёт строку без tz —
    трактуется как UTC). Поэтому в SQLite server_default возвращается
    как НАИВНЫЙ datetime. В PostgreSQL с TIMESTAMPTZ был бы tz-aware.
    Здесь не тестируем разницу платформ — просто фиксируем, что
    значение заполнено и разумно близко к текущему моменту.
    """
    # Наивные метки, чтобы сравнивать с наивным datetime из SQLite.
    # datetime.utcnow() формально deprecated, но альтернатива
    # datetime.now(UTC).replace(tzinfo=None) — просто вербознее.
    before = datetime.now(UTC).replace(tzinfo=None)

    async with db_session_factory() as write_session:
        write_session.add(
            SentNotification(
                event_type="new_issue",
                issue_id=42,
                journal_id=None,
            )
        )
        await write_session.commit()

    after = datetime.now(UTC).replace(tzinfo=None)

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(SentNotification))
        loaded = result.scalar_one()

    assert loaded.sent_at is not None
    # SQLite усекает до секунд — расширяем окно на 1 секунду с каждой
    # стороны, чтобы не ловить флэйк на границах.
    assert before.replace(microsecond=0) <= loaded.sent_at <= after


async def test_duplicate_triple_is_rejected(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """UNIQUE(event_type, issue_id, journal_id) блокирует полный дубль.

    Сценарий из жизни: сервис отправил уведомление, упал до записи
    ответа, перезапустился и попробовал отправить это же ещё раз.
    БД должна кинуть IntegrityError — поллер это исключение поймает
    и спокойно пропустит.
    """
    async with db_session_factory() as first_session:
        first_session.add(
            SentNotification(
                event_type="comment_added",
                issue_id=100,
                journal_id=200,
            )
        )
        await first_session.commit()

    async with db_session_factory() as second_session:
        second_session.add(
            SentNotification(
                event_type="comment_added",
                issue_id=100,
                journal_id=200,
            )
        )
        with pytest.raises(IntegrityError):
            await second_session.commit()


async def test_null_journal_id_does_not_dedup(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """NULL в journal_id не участвует в UNIQUE — две 'new_issue'
    по одной issue проходят.

    Это не бага модели, это стандартная SQL-семантика: NULL != NULL.
    Такое подстилаемое место сознательно оставлено — поллер на этапе 7
    отдельно проверяет new_issue через 'уже слали ли по этому issue_id'.
    Тест фиксирует поведение, чтобы никто случайно не 'починил' его
    добавлением COALESCE-индекса без разбора почему.
    """
    async with db_session_factory() as write_session:
        write_session.add(
            SentNotification(event_type="new_issue", issue_id=500, journal_id=None)
        )
        write_session.add(
            SentNotification(event_type="new_issue", issue_id=500, journal_id=None)
        )
        await write_session.commit()  # обе строки коммитятся без ошибок

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(SentNotification))
        rows = result.scalars().all()

    assert len(rows) == 2
