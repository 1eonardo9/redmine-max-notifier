"""CRUD-операции над таблицей routing.

Асинхронный слой доступа к маршрутизации 'проект Redmine -> чат MAX'.
Функциональный стиль: каждая операция — свободная функция, принимает
AsyncSession первым аргументом. Классов-репозиториев не заводим:
состояния между вызовами нет, сессия сама и есть контекст.

Транзакциями управляет ВЫЗЫВАЮЩИЙ. Функции этого модуля вызывают
session.add()/session.delete()/session.flush(), но НЕ commit().
Это позволяет объединять несколько операций в одну транзакцию
(например, seed-скрипт на 5d вставляет 10 маршрутов одним commit'ом).

Пример использования из внешнего кода:

    async with session_factory() as session:
        await add_route(session, project_id=42, chat_id=-1001)
        await session.commit()

    async with session_factory() as session:
        chats = await list_chats_for_project(session, project_id=42)
        for chat_id in chats:
            await max_client.send_message(chat_id, text)
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from redmine_max_notifier.db.models import Routing


class RouteAlreadyExistsError(Exception):
    """Попытка добавить маршрут, который уже есть в таблице.

    Оборачивает IntegrityError от SQLAlchemy: верхний слой (CLI,
    будущая админка) не должен знать про специфику драйвера БД
    и парсить строки sqlite3.IntegrityError вручную.
    """

    def __init__(self, project_id: int, chat_id: int) -> None:
        self.project_id = project_id
        self.chat_id = chat_id
        super().__init__(
            f"Маршрут project_id={project_id} -> chat_id={chat_id} уже существует"
        )


async def add_route(
    session: AsyncSession,
    *,
    project_id: int,
    chat_id: int,
) -> Routing:
    """Добавляет маршрут 'проект -> чат'.

    Возвращает созданную ORM-запись (с проставленным id — id
    доступен после flush, до commit).

    Дубль (project_id, chat_id) в БД защищён UniqueConstraint
    'uq_routing_project_chat'. При попытке вставки дубля БД
    поднимет IntegrityError на flush, а мы перевыбросим как
    RouteAlreadyExistsError с осмысленным сообщением.

    Идентификаторы — обязательные keyword-аргументы (звёздочка
    после session): в коде вызова видно, кто project_id а кто
    chat_id — два int-а подряд легко перепутать местами.
    """
    route = Routing(project_id=project_id, chat_id=chat_id)
    session.add(route)
    try:
        # flush отправляет INSERT в БД без коммита. UniqueConstraint
        # проверится ЗДЕСЬ. Без flush мы бы поймали IntegrityError
        # только при commit() у вызывающего — а он ждёт от нашей
        # функции чистое исключение сразу.
        await session.flush()
    except IntegrityError as e:
        # Откатываем изменения текущей транзакции — иначе сессия
        # останется в "failed transaction" состоянии и следующий
        # запрос упадёт с InvalidRequestError.
        await session.rollback()
        raise RouteAlreadyExistsError(project_id, chat_id) from e
    return route


async def remove_route(
    session: AsyncSession,
    *,
    project_id: int,
    chat_id: int,
) -> bool:
    """Удаляет маршрут 'проект -> чат'.

    Идемпотентна: если маршрута нет — возвращает False, исключения
    не поднимает. True — удалили ровно одну строку.

    Реализация — DELETE ... WHERE через Core-API SQLAlchemy 2.0,
    без предварительного SELECT: одна операция вместо двух.
    result.rowcount отдаёт число реально удалённых строк.

    session.execute() статически возвращает Result[Any], но
    для DML-операций (DELETE/UPDATE) реальный тип — CursorResult,
    у которого и живёт .rowcount. Cast сообщает mypy это сужение
    типа явно.
    """
    stmt = delete(Routing).where(
        Routing.project_id == project_id,
        Routing.chat_id == chat_id,
    )
    result = cast("CursorResult[Any]", await session.execute(stmt))
    return result.rowcount > 0


async def list_chats_for_project(
    session: AsyncSession,
    project_id: int,
) -> list[int]:
    """Возвращает chat_id всех чатов, подписанных на проект.

    Основная функция для поллера: 'кому слать событие по проекту N'.
    Возвращает голые int'ы, а не ORM-объекты — поллер не должен знать
    про модель Routing. Если однажды появятся доп. поля роутинга
    (фильтры, шаблоны-оверрайды) — переделаем интерфейс тогда,
    впрок не абстрагируем.

    Порядок результата не гарантирован — вызывающий сам сортирует,
    если ему это важно (обычно не важно: рассылка идёт всем).
    Пустой список — валидный результат: 'по этому проекту не
    настроено ни одного чата', поллер молча пропускает.
    """
    stmt = select(Routing.chat_id).where(Routing.project_id == project_id)
    result = await session.execute(stmt)
    # scalars() разворачивает Row -> первый (и единственный)
    # выбранный столбец. .all() -> list[int].
    return list(result.scalars().all())


async def list_all_routes(session: AsyncSession) -> list[Routing]:
    """Возвращает все маршруты целиком (для CLI и админки).

    В отличие от list_chats_for_project возвращает полные ORM-объекты:
    в CLI-выводе на 5d нужны и project_id, и chat_id, и id самого
    маршрута для точечного удаления.
    """
    stmt = select(Routing).order_by(Routing.project_id, Routing.chat_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())
