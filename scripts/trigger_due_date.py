"""Разовый запуск проверки дедлайнов — вне расписания APScheduler.

Штатно `run_due_date_cycle` дёргается раз в сутки в 9:00 (см.
`due_date_job_hour`). Ждать утра, чтобы проверить напоминание о сроке,
неудобно — этот скрипт собирает те же зависимости, что и lifespan
(`web/app.py`), и вызывает цикл ровно один раз.

Гоняет НАСТОЯЩИЙ прод-код (`jobs.run_due_date_cycle`), не имитацию.
Дедуп по `notified_due_date` в силе: на одно значение `due_date`
напоминание уйдёт единожды, сколько скрипт ни повторяй. Чтобы получить
новое — сдвинь срок на задаче.

Запуск на проде — тем же wrapper'ом, что и routing_cli: env-файл через
`set -a; . env; set +a`, затем `sudo -u redmine-notifier` с проброшенными
переменными и `.venv/bin/python scripts/trigger_due_date.py`. Обязательные
переменные (`DATABASE_URL`, `REDMINE_URL`, `REDMINE_API_KEY`, `MAX_TOKEN`,
`MAX_CA_BUNDLE_PATH`) должны попасть в `--preserve-env`, иначе `Settings`
упадёт с `ValidationError` ещё до подключения.

Exit code всегда 0: `run_due_date_cycle` ловит исключения сам и пишет
их в лог, наружу не выпускает. Результат смотри в journalctl.
"""

from __future__ import annotations

import asyncio
import ssl
from datetime import timedelta

from redmine_max_notifier.config import get_settings
from redmine_max_notifier.db.engine import create_engine, create_session_factory
from redmine_max_notifier.jobs import JobDeps, run_due_date_cycle
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.name_resolver import NameResolver
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.renderer import MessageRenderer


async def main_async() -> None:
    """Собирает зависимости как lifespan и один раз гоняет цикл дедлайнов.

    Порядок и параметры повторяют web/app.py:64-118 — это единственный
    способ не разъехаться с боевой сборкой. Закрываем ресурсы в finally
    в обратном порядке, как shutdown в lifespan.
    """
    settings = get_settings()

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    redmine_client = RedmineClient(
        base_url=settings.redmine_url,
        api_key=settings.redmine_api_key,
    )
    # CA-bundle УЦ Минцифры — иначе TLS до platform-api2.max.ru не пройдёт
    # (якорь 4.3). Контекст собираем так же, как lifespan.
    ssl_context = ssl.create_default_context(cafile=settings.max_ca_bundle_path)
    max_client = MaxClient(token=settings.max_token, verify=ssl_context)

    ttl = timedelta(seconds=settings.status_cache_ttl_seconds)
    status_resolver = NameResolver(
        redmine_client.list_issue_statuses, ttl, label="статусов"
    )
    priority_resolver = NameResolver(
        redmine_client.list_issue_priorities, ttl, label="приоритетов"
    )
    renderer = MessageRenderer(
        redmine_base_url=settings.redmine_base_url_public or settings.redmine_url,
        tz=settings.tzinfo,
    )

    deps = JobDeps(
        client=redmine_client,
        status_resolver=status_resolver,
        priority_resolver=priority_resolver,
        renderer=renderer,
        max_client=max_client,
        session_factory=session_factory,
        lookback=timedelta(seconds=settings.polling_lookback_seconds),
        due_date_threshold_days=settings.due_date_threshold_days,
        coexecutors_field_id=settings.coexecutors_field_id,
        tz=settings.tzinfo,
    )

    try:
        await run_due_date_cycle(deps)
    finally:
        await max_client.close()
        await redmine_client.aclose()
        await engine.dispose()


def main() -> None:
    """Sync-обёртка над async main — точка входа скрипта."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
