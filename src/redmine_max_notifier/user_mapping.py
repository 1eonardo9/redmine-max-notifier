"""CRUD-операции над таблицей user_mapping — «Redmine-юзер → MAX-юзер».

Нужно для @упоминаний: в событии лежит assigned_to.id из Redmine, а для
пинга в чате требуется user_id в MAX. Связи между системами нет,
сопоставляем руками (см. scripts/user_mapping_cli.py).

Стиль — как в routing.py: свободные функции, session первым аргументом,
транзакциями управляет вызывающий.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from redmine_max_notifier.db.models import UserMapping


@dataclass(frozen=True)
class MaxUser:
    """Пользователь MAX, которого надо упомянуть.

    Плоская пара вместо ORM-объекта: диспетчеру нужны только id и имя,
    и знать про модель UserMapping ему незачем (тот же приём, что
    в routing.list_chats_for_project — там наружу отдаются голые int).
    """

    user_id: int
    name: str


class MappingAlreadyExistsError(Exception):
    """Такая пара «Redmine-юзер → MAX-юзер» уже есть.

    Оборачивает IntegrityError: CLI не должен парсить строки
    sqlite3.IntegrityError вручную.
    """

    def __init__(self, redmine_user_id: int, max_user_id: int) -> None:
        self.redmine_user_id = redmine_user_id
        self.max_user_id = max_user_id
        super().__init__(
            f"Сопоставление redmine_user_id={redmine_user_id} -> "
            f"max_user_id={max_user_id} уже существует"
        )


async def add_mapping(
    session: AsyncSession,
    *,
    redmine_user_id: int,
    max_user_id: int,
    max_name: str,
) -> UserMapping:
    """Сопоставить пользователя Redmine пользователю MAX.

    Отношение многие-ко-многим: одного Redmine-юзера можно связать
    с несколькими MAX-юзерами и наоборот. Вызывай столько раз, сколько
    нужно пар.

    Идентификаторы — обязательные keyword-аргументы: два int подряд
    легко перепутать местами, а ошибка будет тихой (пинганём не того).
    """
    mapping = UserMapping(
        redmine_user_id=redmine_user_id,
        max_user_id=max_user_id,
        max_name=max_name,
    )
    session.add(mapping)
    try:
        # flush отправляет INSERT в БД без коммита — UniqueConstraint
        # проверится здесь, а не у вызывающего при commit().
        await session.flush()
    except IntegrityError as e:
        # Без rollback сессия останется в failed-состоянии, и следующий
        # запрос упадёт с InvalidRequestError.
        await session.rollback()
        raise MappingAlreadyExistsError(redmine_user_id, max_user_id) from e
    return mapping


async def remove_mapping(
    session: AsyncSession,
    *,
    redmine_user_id: int,
    max_user_id: int,
) -> bool:
    """Удалить пару. Идемпотентна: нет такой пары — вернёт False."""
    stmt = delete(UserMapping).where(
        UserMapping.redmine_user_id == redmine_user_id,
        UserMapping.max_user_id == max_user_id,
    )
    result = cast("CursorResult[Any]", await session.execute(stmt))
    return result.rowcount > 0


async def list_max_users_for_redmine(
    session: AsyncSession,
    redmine_user_id: int,
) -> list[MaxUser]:
    """Кого упоминать в MAX, когда задача на этом Redmine-юзере.

    Основная функция для диспетчера. Пустой список — валидный результат:
    человека просто не сопоставили, уведомление уйдёт без упоминания.

    Порядок стабильный (по id), чтобы упоминания в сообщении не
    прыгали местами от вызова к вызову.
    """
    stmt = (
        select(UserMapping.max_user_id, UserMapping.max_name)
        .where(UserMapping.redmine_user_id == redmine_user_id)
        .order_by(UserMapping.id)
    )
    result = await session.execute(stmt)
    return [MaxUser(user_id=row.max_user_id, name=row.max_name) for row in result]


async def list_all_mappings(session: AsyncSession) -> list[UserMapping]:
    """Все сопоставления целиком — для CLI."""
    stmt = select(UserMapping).order_by(
        UserMapping.redmine_user_id, UserMapping.max_user_id
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
