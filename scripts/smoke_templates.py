"""Smoke-тест шаблонов сообщений: рендерит события и шлёт их в MAX.

Зачем. Гонять полный цикл поллинга ради того, чтобы посмотреть на вёрстку
сообщения, — долго и требует живых изменений в Redmine. Этот скрипт берёт
заранее собранные события и прогоняет их через настоящий MessageRenderer,
так что видно ровно то, что увидят люди в чате.

Запуск (по умолчанию — личный чат из MAX_TEST_CHAT_ID):
    uv run python scripts/smoke_templates.py --dry-run     # только в консоль
    uv run python scripts/smoke_templates.py               # отправить всё
    uv run python scripts/smoke_templates.py -e new_issue  # один тип
    uv run python scripts/smoke_templates.py --chat-id -76702338811265

Правишь шаблон в templates/*.md.j2 -> запускаешь -> смотришь чат.
Redmine при этом не нужен: события синтетические.

Данные намеренно неудобные: спецсимволы markdown в тексте, длинная тема,
задача без исполнителя, многострочный комментарий. Красиво отрендерить
идеальные данные умеет любой шаблон.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import ssl
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# Скрипт лежит в scripts/, пакет — в src/. Без этого import не найдёт модуль
# при запуске "python scripts/smoke_templates.py" из корня репозитория.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from redmine_max_notifier.config import get_settings
from redmine_max_notifier.events.models import (
    CommentAddedEvent,
    DueDateApproachingEvent,
    Event,
    NewIssueEvent,
    StatusChangedEvent,
)
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.maxbot.exceptions import MaxError
from redmine_max_notifier.maxbot.models import MessageFormat
from redmine_max_notifier.redmine.models import Issue, NamedRef
from redmine_max_notifier.renderer import MessageRenderer, format_mention

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

NOW = datetime.now(UTC)

LEO = NamedRef(id=42, name="Leo")
MAKSIM = NamedRef(id=10, name="Максим Мерзляков")
PROJECT = NamedRef(id=1, name="D-TELEKOM")


def _issue(
    *,
    issue_id: int = 34,
    subject: str = "Не поднимается линк на порту gi0/1 после грозы",
    assigned_to: NamedRef | None = MAKSIM,
    due_date: date | None = None,
    status: str = "В работе",
) -> Issue:
    """Задача с реалистичными полями."""
    return Issue(
        id=issue_id,
        project=PROJECT,
        tracker=NamedRef(id=1, name="Ошибка"),
        status=NamedRef(id=2, name=status),
        priority=NamedRef(id=4, name="Высокий"),
        author=LEO,
        assigned_to=assigned_to,
        subject=subject,
        description="Порт мигает, линк не встаёт. Проверить SFP и патч-корд.",
        due_date=due_date,
        created_on=NOW - timedelta(days=2),
        updated_on=NOW,
    )


def build_events() -> dict[str, Event]:
    """Набор событий: по одному на каждый шаблон, с неудобными данными."""
    return {
        "new_issue": NewIssueEvent(
            occurred_at=NOW,
            # Длинная тема со спецсимволами markdown — проверяем перенос
            # и то, что звёздочки не ломают разметку.
            issue=_issue(
                subject=(
                    "Авария на магистрали: *обрыв* ОК на участке "
                    "Самолёт-1 — Самолёт-5, затухание > 30 dB [срочно]"
                ),
                status="Новая",
            ),
        ),
        "status_changed": StatusChangedEvent(
            occurred_at=NOW,
            issue=_issue(),
            journal_id=60,
            old_status_id=1,
            old_status_name="Новая",
            new_status_id=5,
            new_status_name="Закрыта",
            changed_by=MAKSIM,
        ),
        "comment_added": CommentAddedEvent(
            occurred_at=NOW,
            # Без исполнителя — шаблон должен написать "не назначено".
            issue=_issue(assigned_to=None),
            journal_id=59,
            notes=(
                "Заменил SFP-модуль, линк поднялся.\n"
                "Проверил потери: 0.3 dB, в норме.\n\n"
                "Осталось: обжать патч-корд в кроссе и закрыть задачу."
            ),
            author=MAKSIM,
        ),
        # Комментарий с файлами: имена намеренно с пробелами и
        # подчёркиванием — подчёркивание в markdown это разметка.
        "comment_with_files": CommentAddedEvent(
            occurred_at=NOW,
            issue=_issue(),
            journal_id=62,
            notes="Приложил схему трассы и фото кросса.",
            attachments=["ЗУ Штиль.JPG", "схема_трассы_v2.pdf"],
            author=MAKSIM,
        ),
        # Файл без единого слова — до 7h такое молча не доезжало.
        "file_only": CommentAddedEvent(
            occurred_at=NOW,
            issue=_issue(),
            journal_id=61,
            attachments=["i.webp"],
            author=MAKSIM,
        ),
        # Таблица в комментарии: Redmine их умеет, MAX — нет.
        # Смотрим, во что превращается.
        "comment_with_table": CommentAddedEvent(
            occurred_at=NOW,
            issue=_issue(),
            journal_id=63,
            notes=(
                "Замеры по портам:\r\n\r\n"
                "|Порт |Затухание |\r\n"
                "|--|--|\r\n"
                "|gi0/1 |0.3 dB |\r\n"
                "|gi0/2 |31.7 dB |\r\n"
            ),
            author=MAKSIM,
        ),
        "due_date_approaching": DueDateApproachingEvent(
            occurred_at=NOW,
            issue=_issue(
                issue_id=27,
                subject="Прокладка кабеля заземления в серверной Самолет 5",
                due_date=(NOW + timedelta(days=2)).date(),
            ),
            days_before=2,
        ),
    }


def _chat_id_from_args_or_env(args: argparse.Namespace) -> int | None:
    if args.chat_id is not None:
        return int(args.chat_id)
    raw = os.environ.get("MAX_TEST_CHAT_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"ERROR: MAX_TEST_CHAT_ID={raw!r} не int.", file=sys.stderr)
        return None


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Прогнать шаблоны сообщений через рендерер и MAX.",
    )
    parser.add_argument(
        "-e",
        "--event",
        choices=[*build_events().keys(), "all"],
        default="all",
        help="Какой шаблон проверять (по умолчанию все).",
    )
    parser.add_argument(
        "--chat-id",
        help="ID чата (по умолчанию — MAX_TEST_CHAT_ID из .env).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только напечатать markdown в консоль, в MAX не слать.",
    )
    parser.add_argument(
        "--mention-user-id",
        type=int,
        help=(
            "Добавить @упоминание в шаблоны, которые его поддерживают "
            "(new_issue, due_date_approaching). ID берётся из "
            "user_mapping_cli.py max-members."
        ),
    )
    parser.add_argument(
        "--mention-name",
        default="Исполнитель",
        help="Подпись упоминания (по умолчанию 'Исполнитель').",
    )
    args = parser.parse_args()

    settings = get_settings()
    renderer = MessageRenderer(
        redmine_base_url=settings.redmine_base_url_public or settings.redmine_url,
        tz=settings.tzinfo,
    )

    events = build_events()
    selected = events if args.event == "all" else {args.event: events[args.event]}

    # Упоминания приходят в render извне (их резолвит диспетчер из БД),
    # поэтому здесь подставляем вручную — иначе в dry-run их не увидеть.
    mentions = (
        [format_mention(args.mention_user_id, args.mention_name)]
        if args.mention_user_id
        else []
    )

    # Сначала рендерим всё: если шаблон битый, лучше упасть до отправки,
    # чем половину сообщений отправить, а на второй половине развалиться.
    rendered = {
        name: renderer.render(event, mentions=mentions)
        for name, event in selected.items()
    }

    for name, text in rendered.items():
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        print(text)

    if args.dry_run:
        print(
            f"\n[dry-run] отрендерено шаблонов: {len(rendered)}, ничего не отправлено"
        )
        return 0

    chat_id = _chat_id_from_args_or_env(args)
    if chat_id is None:
        print(
            "\nERROR: не задан получатель. Укажи --chat-id <N> "
            "или MAX_TEST_CHAT_ID в .env.",
            file=sys.stderr,
        )
        return 2

    ssl_context = ssl.create_default_context(cafile=settings.max_ca_bundle_path)
    async with MaxClient(token=settings.max_token, verify=ssl_context) as client:
        print(f"\n{'=' * 60}\nОтправка в чат {chat_id}\n{'=' * 60}")
        for name, text in rendered.items():
            try:
                sent = await client.send_message(
                    chat_id, text, format=MessageFormat.MARKDOWN
                )
            except MaxError as exc:
                print(f"  {name:<22} ОШИБКА: {exc}", file=sys.stderr)
                return 1
            print(f"  {name:<22} отправлено (mid={sent.message.body.mid})")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
