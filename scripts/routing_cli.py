"""CLI-утилита для управления маршрутизацией 'проект Redmine -> чат MAX'.

Тонкая обёртка над функциями из redmine_max_notifier.routing:
add / remove / list / list-project.

Использует ту же связку Settings -> engine -> session_factory,
что и веб-приложение — то есть уважает DATABASE_URL из .env.

Примеры:
    uv run python scripts/routing_cli.py add --project-id 42 --chat-id -1001
    uv run python scripts/routing_cli.py list
    uv run python scripts/routing_cli.py list-project --project-id 42
    uv run python scripts/routing_cli.py remove --project-id 42 --chat-id -1001

Exit codes:
    0 — операция прошла.
    1 — операция не удалась по бизнес-причине (дубль при add,
        не найдено при remove).
    Прочие исключения (проблемы с БД, конфигом) пробрасываем —
    трейсбек читаемее, чем 'что-то пошло не так'.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from redmine_max_notifier.config import get_settings
from redmine_max_notifier.db.engine import create_engine, create_session_factory
from redmine_max_notifier.routing import (
    RouteAlreadyExistsError,
    add_route,
    list_all_routes,
    list_chats_for_project,
    remove_route,
)

# ──────────────────────────────────────────────────────────────
# Реализация подкоманд
#
# Каждая подкоманда — async-функция, принимающая открытую сессию
# и argparse.Namespace. Возвращает exit code:
#   0 — ок, 1 — бизнес-проблема (дубль / нет такого маршрута).
# Никакого commit'а внутри — им управляет вызывающий (main).
# ──────────────────────────────────────────────────────────────


async def cmd_add(session: AsyncSession, args: argparse.Namespace) -> int:
    """Добавляет маршрут. Дубль -> exit code 1 + сообщение в stderr."""
    try:
        route = await add_route(
            session,
            project_id=args.project_id,
            chat_id=args.chat_id,
        )
    except RouteAlreadyExistsError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1
    print(
        f"Добавлено: id={route.id} project_id={route.project_id} "
        f"chat_id={route.chat_id}"
    )
    return 0


async def cmd_remove(session: AsyncSession, args: argparse.Namespace) -> int:
    """Удаляет маршрут. 'Нечего удалять' -> exit code 1."""
    removed = await remove_route(
        session,
        project_id=args.project_id,
        chat_id=args.chat_id,
    )
    if not removed:
        print(
            f"ОШИБКА: маршрут project_id={args.project_id} -> "
            f"chat_id={args.chat_id} не найден",
            file=sys.stderr,
        )
        return 1
    print(f"Удалено: project_id={args.project_id} chat_id={args.chat_id}")
    return 0


async def cmd_list(session: AsyncSession, args: argparse.Namespace) -> int:
    """Показывает все маршруты в виде простой таблицы."""
    del args  # неиспользуемо, но сигнатура едина для всех cmd_*
    routes = await list_all_routes(session)
    if not routes:
        print("Маршрутов не настроено.")
        return 0
    # Простое выравнивание f-строк — без tabulate/rich.
    print(f"{'ID':<6} {'PROJECT_ID':<12} {'CHAT_ID':<15}")
    print(f"{'--':<6} {'----------':<12} {'-------':<15}")
    for r in routes:
        print(f"{r.id:<6} {r.project_id:<12} {r.chat_id:<15}")
    return 0


async def cmd_list_project(session: AsyncSession, args: argparse.Namespace) -> int:
    """Показывает чаты, подписанные на конкретный проект."""
    chats = await list_chats_for_project(session, project_id=args.project_id)
    if not chats:
        print(f"Проект {args.project_id}: нет подписанных чатов.")
        return 0
    print(f"Проект {args.project_id}, подписанные чаты:")
    for chat_id in sorted(chats):
        print(f"  chat_id={chat_id}")
    return 0


# ──────────────────────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────────────────────

# Тип хендлера подкоманды. Пригодится в build_parser: складываем
# конкретную функцию в parser.set_defaults(handler=...), потом в
# main вытаскиваем и вызываем — этот паттерн argparse рекомендует
# в своих примерах.
CommandHandler = Callable[[AsyncSession, argparse.Namespace], Awaitable[int]]


def build_parser() -> argparse.ArgumentParser:
    """Собирает CLI-парсер с четырьмя подкомандами.

    Каждая подкоманда через set_defaults(handler=...) уносит с собой
    ссылку на свой async-обработчик. main() вытащит его из Namespace
    и вызовет. Это идиоматический для argparse способ диспетчеризации.
    """
    parser = argparse.ArgumentParser(
        description="Управление маршрутизацией Redmine -> MAX",
    )
    # dest='command' — куда argparse положит имя выбранной подкоманды.
    # required=True — без подкоманды сам напечатает usage и упадёт.
    subparsers = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = subparsers.add_parser("add", help="Добавить маршрут")
    p_add.add_argument("--project-id", type=int, required=True)
    p_add.add_argument("--chat-id", type=int, required=True)
    p_add.set_defaults(handler=cmd_add)

    # remove
    p_remove = subparsers.add_parser("remove", help="Удалить маршрут")
    p_remove.add_argument("--project-id", type=int, required=True)
    p_remove.add_argument("--chat-id", type=int, required=True)
    p_remove.set_defaults(handler=cmd_remove)

    # list
    p_list = subparsers.add_parser("list", help="Показать все маршруты")
    p_list.set_defaults(handler=cmd_list)

    # list-project
    p_list_proj = subparsers.add_parser(
        "list-project", help="Показать чаты для одного проекта"
    )
    p_list_proj.add_argument("--project-id", type=int, required=True)
    p_list_proj.set_defaults(handler=cmd_list_project)

    return parser


async def main_async() -> int:
    """Главный async-цикл: парсит args, поднимает engine, вызывает
    выбранный обработчик в одной сессии/транзакции."""
    args = build_parser().parse_args()
    handler: CommandHandler = args.handler

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            exit_code = await handler(session, args)
            # commit только на успехе. При коде != 0 внутри
            # add_route уже мог быть rollback (при дубле);
            # remove_route на 'не найдено' физически ничего не
            # изменил — коммитить нечего, но и вреда не будет.
            if exit_code == 0:
                await session.commit()
    finally:
        # Закрыть пул соединений корректно, как в lifespan FastAPI.
        await engine.dispose()

    return exit_code


def main() -> None:
    """Sync-обёртка над async main — точка входа скрипта."""
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
