"""Smoke-тест отправки сообщения через MAX Bot API.

Запуск (базовый — chat_id берётся из env MAX_TEST_CHAT_ID):
    uv run python smoke_send.py

С явным chat_id:
    uv run python smoke_send.py --chat-id 443542051

С кастомным текстом и markdown-разметкой:
    uv run python smoke_send.py --chat-id 443542051 --text "*Хеллоу*, MAX!" --markdown

Переменные окружения (можно положить в .env):
    MAX_TOKEN     — токен бота, обязательно.
    MAX_TEST_CHAT_ID  — дефолтный chat_id получателя (int).
                        Может быть перекрыт флагом --chat-id.

Скрипт делает один POST /messages и печатает mid + seq отправленного сообщения.
Если токен или chat_id некорректны — увидим MaxError с внятным сообщением,
без «сырого» трейсбека.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.maxbot.exceptions import MaxError
from redmine_max_notifier.maxbot.models import MessageFormat

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-отправка сообщения в MAX Bot API.",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        default=None,
        help="ID чата (перекрывает env MAX_TEST_CHAT_ID).",
    )
    parser.add_argument(
        "--text",
        default="Тест отправки сообщения в MAX из Redmine",
        help="Текст сообщения.",
    )
    # Взаимоисключающая группа: markdown ИЛИ html, но не оба.
    # Если не указан ни один — шлём без format, MAX покажет plain text.
    fmt_group = parser.add_mutually_exclusive_group()
    fmt_group.add_argument(
        "--markdown",
        action="store_const",
        dest="format",
        const=MessageFormat.MARKDOWN,
        help="Интерпретировать текст как markdown.",
    )
    fmt_group.add_argument(
        "--html",
        action="store_const",
        dest="format",
        const=MessageFormat.HTML,
        help="Интерпретировать текст как HTML.",
    )
    parser.set_defaults(format=None)
    return parser.parse_args()


def resolve_chat_id(args: argparse.Namespace) -> int | None:
    if args.chat_id is not None:
        return int(args.chat_id)
    env_value = os.environ.get("MAX_TEST_CHAT_ID")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            print(
                f"ERROR: MAX_TEST_CHAT_ID='{env_value}' не int.",
                file=sys.stderr,
            )
    return None


async def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = parse_args()

    token = os.environ.get("MAX_TOKEN")
    if not token:
        print("ERROR: переменная окружения MAX_TOKEN не задана.", file=sys.stderr)
        return 1

    chat_id = resolve_chat_id(args)
    if chat_id is None:
        print(
            "ERROR: не задан получатель. Укажи --chat-id <N> или MAX_TEST_CHAT_ID в .env.",  # noqa: E501
            file=sys.stderr,
        )
        return 1

    ca_bundle = Path(__file__).resolve().parent.parent / "certs" / "ca_bundle.pem"
    if not ca_bundle.exists():
        print(f"ERROR: CA-bundle не найден: {ca_bundle}", file=sys.stderr)
        print(
            "Запусти сначала: uv run python scripts/build_ca_bundle.py",
            file=sys.stderr,
        )
        return 1

    print(f"→ Отправляем в chat_id={chat_id}")
    print(f"  format={args.format or 'plain'}")
    print(f"  text={args.text!r}\n")

    try:
        async with MaxClient(token=token, verify=str(ca_bundle)) as client:
            sent = await client.send_message(
                chat_id=chat_id,
                text=args.text,
                format=args.format,
            )
    except MaxError as exc:
        # Не даём вылететь трейсбеку — в smoke-скрипте оно только шумит.
        # Класс исключения + сообщение уже несут всю нужную информацию:
        # тип ошибки (Auth / NotFound / Validation / ...) и её описание.
        print(f"\nERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    print("\n✓ Сообщение отправлено.")
    print(f"  mid: {sent.message.body.mid}")
    print(f"  seq: {sent.message.body.seq}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
