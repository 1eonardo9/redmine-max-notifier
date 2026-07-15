"""Тесты сопоставления «пользователь Redmine → пользователь MAX».

Отношение многие-ко-многим — не абстракция впрок, а требование:
Петя в отпуске, его задачи должны пинговать Васю; на задачу Пети
хотим дёрнуть и Петю, и тимлида.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from redmine_max_notifier.user_mapping import (
    MappingAlreadyExistsError,
    add_mapping,
    list_all_mappings,
    list_max_users_for_redmine,
    remove_mapping,
)

REDMINE_PETYA = 10
REDMINE_VASYA = 11
MAX_PETYA = 252123521
MAX_VASYA = 359199650


async def test_add_and_resolve(db_session: AsyncSession) -> None:
    """Базовый путь: связали — резолвится."""
    await add_mapping(
        db_session,
        redmine_user_id=REDMINE_PETYA,
        max_user_id=MAX_PETYA,
        max_name="Петя",
    )

    users = await list_max_users_for_redmine(db_session, REDMINE_PETYA)

    assert len(users) == 1
    assert users[0].user_id == MAX_PETYA
    assert users[0].name == "Петя"


async def test_one_redmine_user_to_many_max_users(db_session: AsyncSession) -> None:
    """Задача на Петю — пингуем и Петю, и Васю (например, тимлида)."""
    await add_mapping(
        db_session,
        redmine_user_id=REDMINE_PETYA,
        max_user_id=MAX_PETYA,
        max_name="Петя",
    )
    await add_mapping(
        db_session,
        redmine_user_id=REDMINE_PETYA,
        max_user_id=MAX_VASYA,
        max_name="Вася",
    )

    users = await list_max_users_for_redmine(db_session, REDMINE_PETYA)

    assert [u.name for u in users] == ["Петя", "Вася"]


async def test_many_redmine_users_to_one_max_user(db_session: AsyncSession) -> None:
    """Петя в отпуске: и его задачи, и Васины пингуют Васю."""
    await add_mapping(
        db_session,
        redmine_user_id=REDMINE_PETYA,
        max_user_id=MAX_VASYA,
        max_name="Вася",
    )
    await add_mapping(
        db_session,
        redmine_user_id=REDMINE_VASYA,
        max_user_id=MAX_VASYA,
        max_name="Вася",
    )

    for redmine_id in (REDMINE_PETYA, REDMINE_VASYA):
        users = await list_max_users_for_redmine(db_session, redmine_id)
        assert [u.user_id for u in users] == [MAX_VASYA]


async def test_cross_mapping(db_session: AsyncSession) -> None:
    """Произвольная пара: Redmine-Петя упоминается как MAX-Вася.

    Имя в упоминании — MAX-пользователя, а не Redmine: в чате должен
    быть виден и дёрнут тот, кого реально пингуем.
    """
    await add_mapping(
        db_session,
        redmine_user_id=REDMINE_PETYA,
        max_user_id=MAX_VASYA,
        max_name="Вася",
    )

    users = await list_max_users_for_redmine(db_session, REDMINE_PETYA)

    assert users[0].name == "Вася"


async def test_duplicate_pair_rejected(db_session: AsyncSession) -> None:
    """Дубль пары — ошибка: иначе человек получил бы два пинга подряд."""
    await add_mapping(
        db_session,
        redmine_user_id=REDMINE_PETYA,
        max_user_id=MAX_PETYA,
        max_name="Петя",
    )

    with pytest.raises(MappingAlreadyExistsError):
        await add_mapping(
            db_session,
            redmine_user_id=REDMINE_PETYA,
            max_user_id=MAX_PETYA,
            max_name="Петя",
        )


async def test_unmapped_user_returns_empty(db_session: AsyncSession) -> None:
    """Человека не сопоставили — пустой список, а не ошибка.

    Подрядчик, уволившийся, робот-учётка: уведомление должно уйти
    без упоминания, а не упасть.
    """
    assert await list_max_users_for_redmine(db_session, 999) == []


async def test_remove_is_idempotent(db_session: AsyncSession) -> None:
    """Удаление несуществующей пары — False, без исключения."""
    await add_mapping(
        db_session,
        redmine_user_id=REDMINE_PETYA,
        max_user_id=MAX_PETYA,
        max_name="Петя",
    )

    assert (
        await remove_mapping(
            db_session, redmine_user_id=REDMINE_PETYA, max_user_id=MAX_PETYA
        )
        is True
    )
    assert (
        await remove_mapping(
            db_session, redmine_user_id=REDMINE_PETYA, max_user_id=MAX_PETYA
        )
        is False
    )
    assert await list_max_users_for_redmine(db_session, REDMINE_PETYA) == []


async def test_mapping_survives_roundtrip(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Данные реально доезжают до БД, а не живут в identity map сессии."""
    async with db_session_factory() as session:
        await add_mapping(
            session,
            redmine_user_id=REDMINE_PETYA,
            max_user_id=MAX_PETYA,
            max_name="Петя",
        )
        await session.commit()

    async with db_session_factory() as fresh:
        mappings = await list_all_mappings(fresh)

    assert len(mappings) == 1
    assert mappings[0].redmine_user_id == REDMINE_PETYA
    assert mappings[0].max_name == "Петя"
