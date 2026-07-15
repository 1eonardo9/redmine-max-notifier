"""CLI-утилита для сопоставления пользователей Redmine и MAX.

Нужна для @упоминаний: в событии лежит assigned_to.id из Redmine, а для
пинга в чате требуется user_id в MAX. Связи между системами нет —
сопоставляем руками, людей немного.

Отношение многие-ко-многим: одного Redmine-юзера можно связать с
несколькими MAX-юзерами и наоборот (Петя в отпуске — пингуем Васю).

Типичный сценарий:

    # 1. Кто есть в MAX (берём max_user_id)
    uv run python scripts/user_mapping_cli.py max-members

    # 2. Кто есть в Redmine (берём redmine_user_id)
    uv run python scripts/user_mapping_cli.py redmine-users

    # 3. Связываем (имя подтянется из MAX само)
    uv run python scripts/user_mapping_cli.py add --redmine-user-id 10 \
        --max-user-id 252123521

    # 4. Проверяем
    uv run python scripts/user_mapping_cli.py list
    uv run python scripts/user_mapping_cli.py list-redmine --redmine-user-id 10

Exit codes:
    0 — операция прошла.
    1 — бизнес-причина (дубль при add, не найдено при remove,
        user_id не найден в MAX).
    Прочие исключения пробрасываем — трейсбек читаемее, чем
    'что-то пошло не так'.
"""

from __future__ import annotations

import argparse
import asyncio
import ssl
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

# Скрипт лежит в scripts/, пакет — в src/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from redmine_max_notifier.config import Settings, get_settings
from redmine_max_notifier.db.engine import (
    create_engine,
    create_session_factory,
)
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.user_mapping import (
    MappingAlreadyExistsError,
    add_mapping,
    list_all_mappings,
    list_max_users_for_redmine,
    remove_mapping,
)

MAX_API_BASE = "https://platform-api2.max.ru"


# ──────────────────────────────────────────────────────────────
# Хелперы: заглядываем в MAX и Redmine, чтобы было что сопоставлять
# ──────────────────────────────────────────────────────────────


async def _fetch_max_members(settings: Settings) -> list[dict[str, object]]:
    """Собрать участников всех чатов, где есть бот.

    MAX не даёт «всех пользователей» — только участников чатов, где
    состоит бот. Для нашей задачи этого достаточно: упомянуть можно
    лишь того, кто в чате.
    """
    ssl_context = ssl.create_default_context(cafile=settings.max_ca_bundle_path)
    members: list[dict[str, object]] = []
    seen: set[int] = set()

    async with httpx.AsyncClient(
        base_url=MAX_API_BASE,
        headers={"Authorization": settings.max_token},
        verify=ssl_context,
        trust_env=False,
        timeout=15.0,
    ) as client:
        chats_response = await client.get("/chats")
        chats_response.raise_for_status()

        for chat in chats_response.json().get("chats", []):
            chat_id = chat["chat_id"]
            members_response = await client.get(f"/chats/{chat_id}/members")
            if members_response.status_code != 200:
                continue
            for member in members_response.json().get("members", []):
                user_id = member["user_id"]
                if user_id in seen:
                    continue
                seen.add(user_id)
                members.append(
                    {
                        "user_id": user_id,
                        "name": member.get("name") or member.get("first_name") or "",
                        "username": member.get("username") or "",
                        "is_bot": member.get("is_bot", False),
                        "chat": chat.get("title") or str(chat_id),
                    }
                )
    return members


async def _fetch_redmine_users(settings: Settings) -> list[tuple[int, str]]:
    """Собрать пользователей Redmine, встречающихся в задачах.

    Не GET /users.json: он требует прав администратора, а сервисный
    юзер их намеренно не имеет. Зато авторов и исполнителей видно
    прямо в задачах — а сопоставлять нам нужны именно они.
    """
    users: dict[int, str] = {}
    async with RedmineClient(
        base_url=settings.redmine_url, api_key=settings.redmine_api_key
    ) as client:
        async for issue in client.list_issues(status_id="*", page_size=100):
            for ref in (issue.author, issue.assigned_to):
                if ref is not None and ref.name:
                    users[ref.id] = ref.name
    return sorted(users.items())


# ──────────────────────────────────────────────────────────────
# Подкоманды
# ──────────────────────────────────────────────────────────────


async def cmd_add(session: AsyncSession, args: argparse.Namespace) -> int:
    """Связать Redmine-юзера с MAX-юзером.

    Имя для упоминания подтягиваем из MAX, если не задано явно: руками
    его вбивать незачем, а опечатка в подписи всплыла бы только в чате.
    """
    max_name: str | None = args.max_name
    if max_name is None:
        settings = get_settings()
        members = await _fetch_max_members(settings)
        match = next(
            (m for m in members if m["user_id"] == args.max_user_id),
            None,
        )
        if match is None:
            print(
                f"ОШИБКА: max_user_id={args.max_user_id} не найден среди "
                f"участников чатов бота.\n"
                f"Посмотри доступных: user_mapping_cli.py max-members\n"
                f"Либо задай подпись вручную: --max-name 'Имя'",
                file=sys.stderr,
            )
            return 1
        max_name = str(match["name"])
        print(f"Имя из MAX: {max_name!r}")

    try:
        mapping = await add_mapping(
            session,
            redmine_user_id=args.redmine_user_id,
            max_user_id=args.max_user_id,
            max_name=max_name,
        )
    except MappingAlreadyExistsError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1

    print(
        f"Добавлено: id={mapping.id} redmine_user_id={mapping.redmine_user_id} "
        f"-> max_user_id={mapping.max_user_id} ({mapping.max_name})"
    )
    return 0


async def cmd_remove(session: AsyncSession, args: argparse.Namespace) -> int:
    """Удалить пару. 'Нечего удалять' -> exit code 1."""
    removed = await remove_mapping(
        session,
        redmine_user_id=args.redmine_user_id,
        max_user_id=args.max_user_id,
    )
    if not removed:
        print(
            f"ОШИБКА: сопоставление redmine_user_id={args.redmine_user_id} -> "
            f"max_user_id={args.max_user_id} не найдено",
            file=sys.stderr,
        )
        return 1
    print(
        f"Удалено: redmine_user_id={args.redmine_user_id} "
        f"max_user_id={args.max_user_id}"
    )
    return 0


async def cmd_list(session: AsyncSession, args: argparse.Namespace) -> int:
    """Все сопоставления таблицей."""
    del args  # сигнатура едина для всех cmd_*
    mappings = await list_all_mappings(session)
    if not mappings:
        print("Сопоставлений не настроено.")
        return 0
    print(f"{'ID':<5} {'REDMINE_USER':<14} {'MAX_USER':<14} {'ИМЯ В MAX'}")
    print(f"{'--':<5} {'------------':<14} {'--------':<14} {'---------'}")
    for m in mappings:
        print(f"{m.id:<5} {m.redmine_user_id:<14} {m.max_user_id:<14} {m.max_name}")
    return 0


async def cmd_list_redmine(session: AsyncSession, args: argparse.Namespace) -> int:
    """Кого упомянут, если задача на этом Redmine-юзере."""
    users = await list_max_users_for_redmine(session, args.redmine_user_id)
    if not users:
        print(f"Redmine-юзер {args.redmine_user_id}: сопоставлений нет.")
        print("Уведомления по его задачам уйдут без упоминания.")
        return 0
    print(f"Redmine-юзер {args.redmine_user_id} -> упоминаем в MAX:")
    for u in users:
        print(f"  {u.name} (max_user_id={u.user_id})")
    return 0


async def cmd_max_members(session: AsyncSession, args: argparse.Namespace) -> int:
    """Участники чатов, где есть бот — источник max_user_id."""
    del session, args  # ходим в API, не в БД
    members = await _fetch_max_members(get_settings())
    if not members:
        print("Участников не найдено. Бот добавлен хоть в один чат?")
        return 0

    print(f"{'MAX_USER_ID':<14} {'ИМЯ':<28} {'USERNAME':<24} {'ЧАТ'}")
    print(f"{'-----------':<14} {'---':<28} {'--------':<24} {'---'}")
    for m in members:
        # Ботов помечаем: сопоставлять их с людьми Redmine незачем.
        name = f"{m['name']}{' [BOT]' if m['is_bot'] else ''}"
        print(f"{m['user_id']!s:<14} {name:<28} {m['username']!s:<24} {m['chat']}")
    return 0


async def cmd_redmine_users(session: AsyncSession, args: argparse.Namespace) -> int:
    """Пользователи Redmine из задач — источник redmine_user_id."""
    del session, args  # ходим в API, не в БД
    users = await _fetch_redmine_users(get_settings())
    if not users:
        print("Пользователей не найдено. Сервисный юзер видит задачи?")
        return 0

    print(f"{'REDMINE_USER_ID':<18} {'ИМЯ'}")
    print(f"{'---------------':<18} {'---'}")
    for user_id, name in users:
        print(f"{user_id:<18} {name}")
    return 0


# ──────────────────────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────────────────────

CommandHandler = Callable[[AsyncSession, argparse.Namespace], Awaitable[int]]


def build_parser() -> argparse.ArgumentParser:
    """Собирает CLI-парсер. Каждая подкоманда уносит свой обработчик
    через set_defaults(handler=...) — идиоматично для argparse."""
    parser = argparse.ArgumentParser(
        description="Сопоставление пользователей Redmine и MAX для @упоминаний",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_add = subparsers.add_parser("add", help="Связать Redmine-юзера с MAX-юзером")
    p_add.add_argument("--redmine-user-id", type=int, required=True)
    p_add.add_argument("--max-user-id", type=int, required=True)
    p_add.add_argument(
        "--max-name",
        help="Подпись в упоминании. Если не задать — подтянется из MAX.",
    )
    p_add.set_defaults(handler=cmd_add)

    p_remove = subparsers.add_parser("remove", help="Удалить сопоставление")
    p_remove.add_argument("--redmine-user-id", type=int, required=True)
    p_remove.add_argument("--max-user-id", type=int, required=True)
    p_remove.set_defaults(handler=cmd_remove)

    p_list = subparsers.add_parser("list", help="Показать все сопоставления")
    p_list.set_defaults(handler=cmd_list)

    p_list_redmine = subparsers.add_parser(
        "list-redmine", help="Кого упомянут для одного Redmine-юзера"
    )
    p_list_redmine.add_argument("--redmine-user-id", type=int, required=True)
    p_list_redmine.set_defaults(handler=cmd_list_redmine)

    p_members = subparsers.add_parser(
        "max-members", help="Участники чатов MAX (источник max_user_id)"
    )
    p_members.set_defaults(handler=cmd_max_members)

    p_users = subparsers.add_parser(
        "redmine-users", help="Пользователи Redmine из задач (источник redmine_user_id)"
    )
    p_users.set_defaults(handler=cmd_redmine_users)

    return parser


async def main_async() -> int:
    """Парсит args, поднимает engine, вызывает обработчик в одной сессии."""
    args = build_parser().parse_args()
    handler: CommandHandler = args.handler

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            exit_code = await handler(session, args)
            # commit только на успехе: add_mapping при дубле уже сделал
            # rollback, remove на 'не найдено' ничего не менял.
            if exit_code == 0:
                await session.commit()
    finally:
        await engine.dispose()

    return exit_code


def main() -> None:
    """Sync-обёртка над async main — точка входа скрипта."""
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
