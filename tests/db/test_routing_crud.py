"""Тесты CRUD-операций над таблицей routing.

Тесты именно публичного API из routing.py, не самой ORM-модели —
модельные тесты (уникальность, отношения многие-ко-многим) лежат
в test_routing.py и остаются как есть.

Все тесты round-trip'ят через две сессии из db_session_factory:
пишем в первой, коммитим, читаем во второй. Одна сессия здесь
не годится — identity map при expire_on_commit=False вернёт нам
тот же объект из памяти, а не из БД, и мы будем тестировать
identity map, а не CRUD.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.db.models import Routing
from redmine_max_notifier.routing import (
    RouteAlreadyExistsError,
    add_route,
    list_all_routes,
    list_chats_for_project,
    remove_route,
)

# ──────────────────────────────────────────────────────────────
# add_route
# ──────────────────────────────────────────────────────────────


async def test_add_route_inserts_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Базовый случай: одна вставка, читаем во второй сессии."""
    async with db_session_factory() as write_session:
        route = await add_route(write_session, project_id=42, chat_id=-1001)
        await write_session.commit()
        # id проставлен flush'ем внутри add_route
        assert route.id is not None

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(Routing))
        rows = list(result.scalars().all())
        assert len(rows) == 1
        assert rows[0].project_id == 42
        assert rows[0].chat_id == -1001


async def test_add_route_negative_chat_id_ok(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """chat_id в MAX для групп — отрицательный. БД не должна возражать.

    Тест против регресса: если кто-то однажды поставит CheckConstraint
    'chat_id > 0' или Pydantic-валидатор PositiveInt на слое выше —
    групповая рассылка сломается тихо. Этот тест шумно упадёт.
    """
    async with db_session_factory() as write_session:
        await add_route(write_session, project_id=1, chat_id=-1234567890)
        await write_session.commit()

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(Routing))
        rows = list(result.scalars().all())
        assert rows[0].chat_id == -1234567890


async def test_add_route_duplicate_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повтор (project_id, chat_id) -> RouteAlreadyExistsError,
    не голый IntegrityError."""
    async with db_session_factory() as session:
        await add_route(session, project_id=42, chat_id=-1001)
        await session.commit()

    async with db_session_factory() as session:
        with pytest.raises(RouteAlreadyExistsError) as exc_info:
            await add_route(session, project_id=42, chat_id=-1001)
        # Атрибуты исключения — для CLI/логов пригодятся
        assert exc_info.value.project_id == 42
        assert exc_info.value.chat_id == -1001


async def test_add_route_session_usable_after_duplicate(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """После RouteAlreadyExistsError сессия остаётся рабочей.

    add_route на дубле делает rollback внутри. Без него сессия
    остаётся в failed-transaction, и следующий запрос падает с
    PendingRollbackError. Тест проверяет именно rollback:
    в одной сессии ловим дубль, затем вставляем другой маршрут,
    он должен пройти.
    """
    async with db_session_factory() as session:
        await add_route(session, project_id=42, chat_id=-1001)
        await session.commit()

    async with db_session_factory() as session:
        with pytest.raises(RouteAlreadyExistsError):
            await add_route(session, project_id=42, chat_id=-1001)
        # Сессия жива — вставляем ДРУГУЮ пару
        await add_route(session, project_id=42, chat_id=-2002)
        await session.commit()

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(Routing))
        rows = list(result.scalars().all())
        assert len(rows) == 2


# ──────────────────────────────────────────────────────────────
# remove_route
# ──────────────────────────────────────────────────────────────


async def test_remove_existing_route_returns_true(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await add_route(session, project_id=42, chat_id=-1001)
        await session.commit()

    async with db_session_factory() as session:
        removed = await remove_route(session, project_id=42, chat_id=-1001)
        await session.commit()
        assert removed is True

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(Routing))
        assert list(result.scalars().all()) == []


async def test_remove_missing_route_returns_false(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Идемпотентность: удаление несуществующей связки -> False, без исключений."""
    async with db_session_factory() as session:
        removed = await remove_route(session, project_id=999, chat_id=-9999)
        await session.commit()
        assert removed is False


async def test_remove_route_touches_only_matching_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Удаляем один маршрут — соседние по project_id/chat_id не задеты."""
    async with db_session_factory() as session:
        await add_route(session, project_id=42, chat_id=-1001)
        await add_route(session, project_id=42, chat_id=-2002)  # тот же проект
        await add_route(session, project_id=99, chat_id=-1001)  # тот же чат
        await session.commit()

    async with db_session_factory() as session:
        removed = await remove_route(session, project_id=42, chat_id=-1001)
        await session.commit()
        assert removed is True

    async with db_session_factory() as read_session:
        result = await read_session.execute(select(Routing))
        rows = list(result.scalars().all())
        pairs = {(r.project_id, r.chat_id) for r in rows}
        assert pairs == {(42, -2002), (99, -1001)}


# ──────────────────────────────────────────────────────────────
# list_chats_for_project
# ──────────────────────────────────────────────────────────────


async def test_list_chats_for_project_returns_all_chats(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один проект -> несколько чатов, возвращаются все."""
    async with db_session_factory() as session:
        await add_route(session, project_id=42, chat_id=-1001)
        await add_route(session, project_id=42, chat_id=-2002)
        await add_route(session, project_id=99, chat_id=-3003)  # чужой проект
        await session.commit()

    async with db_session_factory() as read_session:
        chats = await list_chats_for_project(read_session, project_id=42)

    # set-сравнение — порядок SELECT без ORDER BY не гарантирован
    assert set(chats) == {-1001, -2002}


async def test_list_chats_for_project_returns_ints(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Функция возвращает list[int], а не list[Routing].

    Регрессионный тест: если кто-то однажды перепишет реализацию
    на `select(Routing)` и забудет извлечь chat_id — поллер начнёт
    отправлять объекты Routing в MaxClient.send_message() как chat_id.
    """
    async with db_session_factory() as session:
        await add_route(session, project_id=42, chat_id=-1001)
        await session.commit()

    async with db_session_factory() as read_session:
        chats = await list_chats_for_project(read_session, project_id=42)

    assert chats == [-1001]
    assert isinstance(chats[0], int)


async def test_list_chats_for_project_empty(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проект без маршрутов -> пустой список, не None и не исключение."""
    async with db_session_factory() as read_session:
        chats = await list_chats_for_project(read_session, project_id=42)
    assert chats == []


# ──────────────────────────────────────────────────────────────
# list_all_routes
# ──────────────────────────────────────────────────────────────


async def test_list_all_routes_returns_everything_sorted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """list_all_routes отдаёт все маршруты, отсортированные по (project_id, chat_id)."""
    async with db_session_factory() as session:
        await add_route(session, project_id=99, chat_id=-3003)
        await add_route(session, project_id=42, chat_id=-2002)
        await add_route(session, project_id=42, chat_id=-1001)
        await session.commit()

    async with db_session_factory() as read_session:
        routes = await list_all_routes(read_session)

    # Тут порядок ВАЖЕН — функция обещает order_by(project_id, chat_id)
    pairs = [(r.project_id, r.chat_id) for r in routes]
    assert pairs == [(42, -2002), (42, -1001), (99, -3003)]
