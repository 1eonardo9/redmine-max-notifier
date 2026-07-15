"""Smoke-тест MAX Bot API клиента.

Запуск:
    MAX_TOKEN=<твой_токен> uv run python scripts/smoke_max.py

Или добавь MAX_TOKEN в .env файл и подгружай через любой удобный способ
(pydantic-settings подключим на Этапе 2g). Пока — просто export переменной.

Скрипт делает GET /me и печатает информацию о боте. Если токен рабочий —
увидишь user_id, имя бота и т.д. Если нет — httpx.HTTPStatusError с кодом 401.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from redmine_max_notifier.maxbot.client import MaxClient

load_dotenv()


async def main() -> int:
    # Настраиваем логирование ДО создания клиента — иначе debug-логи _request()
    # не увидим. Формат минимальный, для интерактивной отладки.
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token = os.environ.get("MAX_TOKEN")
    if not token:
        print("ERROR: переменная окружения MAX_TOKEN не задана.", file=sys.stderr)
        print(
            "Запуск: MAX_TOKEN=<токен> uv run python scripts/smoke_max.py",
            file=sys.stderr,
        )
        return 1

    ca_bundle = Path(__file__).resolve().parent.parent / "certs" / "ca_bundle.pem"
    if not ca_bundle.exists():
        print(f"ERROR: CA-bundle не найден: {ca_bundle}", file=sys.stderr)
        print(
            "Запусти сначала: uv run python scripts/build_ca_bundle.py", file=sys.stderr
        )
        return 1

    print("Подключаемся к MAX Bot API...")

    async with MaxClient(token=token, verify=str(ca_bundle)) as client:
        bot_info = await client.get_me()

    # Теперь bot_info — не сырой dict, а типизированный BotInfo.
    # IDE подсказывает поля, mypy проверяет типы, всё замечательно.
    print("\nИнформация о боте:")
    print(f"  Имя:                  {bot_info.first_name}")
    print(f"  Username:             @{bot_info.username}")
    print(f"  User ID:              {bot_info.user_id}")
    print(f"  Описание:             {bot_info.description or '<не задано>'}")
    print(f"  Последняя активность: {bot_info.last_activity_time.isoformat()}")

    # Если хочется полный дамп — model_dump_json из Pydantic.
    # Это удобно для отладки: получаешь чистый JSON без ручного
    # преобразования типов (datetime → строка сделается автоматически).
    # print("\nПолный дамп модели (JSON):")
    # print(bot_info.model_dump_json(indent=2))

    print("\nПробуем поймать входящие события (long polling, timeout=5s)...")
    print(
        "Пока ничего не делай — это разведочный вызов, событий скорее всего не будет.\n"
    )

    async with MaxClient(token=token, verify=str(ca_bundle)) as client:
        # Первый вызов — без marker, MAX вернёт свежие события из кэша (если есть).
        # Короткий timeout=5, чтобы smoke не подвисал на 30 секунд.
        updates = await client.get_updates(timeout=5)

    print(f"Получено событий: {len(updates.updates)}")
    print(f"Marker для следующего запроса: {updates.marker}")

    if updates.updates:
        print("\nПервое событие:")
        first = updates.updates[0]
        print(f"  Тип: {first.update_type}")
        print(f"  Время: {first.timestamp.isoformat()}")
        print(f"  chat_id: {first.chat_id}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
