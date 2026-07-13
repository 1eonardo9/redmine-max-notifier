"""Тесты модели Routing.

Проверяем:
- round-trip записи;
- UNIQUE(project_id, chat_id) блокирует полный дубль пары;
- один проект может слать в несколько чатов;
- один чат может получать из нескольких проектов.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.db import Routing


async def test_insert_and_read_back(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пишем маршрут — читаем в новой сессии, поля целы."""
    async with db_session_factory() as write_session:
        write_session.add(Routing(project_id=10, chat_id=-1001))
        await write_session.commit()

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(Routing))
        loaded = result.scalar_one()

    assert loaded.project_id == 10
    assert loaded.chat_id == -1001
    assert loaded.id is not None


async def test_duplicate_pair_is_rejected(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """UNIQUE(project_id, chat_id) блокирует полный дубль пары.

    Сценарий: админ через будущую CLI/UI пытается второй раз
    добавить тот же маршрут — БД аккуратно отказывает.
    """
    async with db_session_factory() as first_session:
        first_session.add(Routing(project_id=10, chat_id=-1001))
        await first_session.commit()

    async with db_session_factory() as second_session:
        second_session.add(Routing(project_id=10, chat_id=-1001))
        with pytest.raises(IntegrityError):
            await second_session.commit()


async def test_one_project_to_many_chats(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один проект может слать сразу в несколько чатов.

    Реальный кейс: изменения в проекте 'разработка' идут
    и в чат разработчиков, и в чат менеджеров.
    """
    async with db_session_factory() as write_session:
        write_session.add(Routing(project_id=10, chat_id=-1001))
        write_session.add(Routing(project_id=10, chat_id=-2002))
        await write_session.commit()

    async with db_session_factory() as read_session:
        result = await read_session.execute(
            select(Routing).where(Routing.project_id == 10)
        )
        rows = result.scalars().all()

    assert len(rows) == 2
    assert {r.chat_id for r in rows} == {-1001, -2002}


async def test_one_chat_from_many_projects(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """В один чат могут литься события из разных проектов.

    Реальный кейс: сводный чат для тимлида,
    куда падают уведомления по всем его проектам.
    """
    async with db_session_factory() as write_session:
        write_session.add(Routing(project_id=10, chat_id=-1001))
        write_session.add(Routing(project_id=20, chat_id=-1001))
        await write_session.commit()

    async with db_session_factory() as read_session:
        result = await read_session.execute(
            select(Routing).where(Routing.chat_id == -1001)
        )
        rows = result.scalars().all()

    assert len(rows) == 2
    assert {r.project_id for r in rows} == {10, 20}
