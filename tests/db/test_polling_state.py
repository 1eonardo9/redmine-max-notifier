"""Тесты модели PollingState.

Проверяем round-trip через БД: пишем в одной сессии, читаем в
другой. Использовать одну сессию для write+read опасно — при
expire_on_commit=False (наш прод-дефолт) второе чтение вернёт
тот же объект из identity map, БД по факту не трогается.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.db import PollingState


async def test_insert_and_read_back(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вставляем строку в одной сессии — читаем её в другой."""
    now = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)

    # Пишущая сессия.
    async with db_session_factory() as write_session:
        state = PollingState(
            id=1,
            last_seen_issue_id=42,
            last_seen_journal_id=101,
            last_check_at=now,
        )
        write_session.add(state)
        await write_session.commit()

    # Читающая сессия — свежая, без общей identity map с write_session.
    async with db_session_factory() as read_session:
        result = await read_session.execute(
            select(PollingState).where(PollingState.id == 1)
        )
        loaded = result.scalar_one()

    assert loaded.id == 1
    assert loaded.last_seen_issue_id == 42
    assert loaded.last_seen_journal_id == 101
    # DateTime() без timezone=True: SQLite возвращает наивный datetime.
    # Сравниваем с наивной версией того же момента.
    assert loaded.last_check_at == now.replace(tzinfo=None)


async def test_defaults_are_none(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Опциональные поля дефолтятся в None, если не заданы при вставке."""
    async with db_session_factory() as write_session:
        write_session.add(PollingState(id=1))
        await write_session.commit()

    async with db_session_factory() as read_session:
        result = await read_session.execute(
            select(PollingState).where(PollingState.id == 1)
        )
        loaded = result.scalar_one()

    assert loaded.last_seen_issue_id is None
    assert loaded.last_seen_journal_id is None
    assert loaded.last_check_at is None
