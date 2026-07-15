"""Фабрика ASGI-приложения и его жизненный цикл.

create_app() собирает FastAPI, lifespan поднимает и гасит всё, что
живёт столько же, сколько процесс: движок БД, HTTP-клиенты Redmine
и MAX, кэш статусов, рендерер и планировщик фоновых задач.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from redmine_max_notifier.config import Settings, get_settings
from redmine_max_notifier.db.engine import create_engine, create_session_factory
from redmine_max_notifier.jobs import JobDeps
from redmine_max_notifier.maxbot.client import MaxClient
from redmine_max_notifier.redmine.client import RedmineClient
from redmine_max_notifier.renderer import MessageRenderer
from redmine_max_notifier.scheduler import (
    create_scheduler,
    register_due_date_job,
    register_poll_job,
)
from redmine_max_notifier.status_resolver import StatusResolver
from redmine_max_notifier.web.routes.health import router as health_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Хук жизненного цикла ASGI-приложения.

    Startup (до yield), строго в этом порядке:
      1. создаём AsyncEngine из Settings, сохраняем в app.state.engine;
      2. создаём фабрику сессий, сохраняем в app.state.session_factory;
      3. поднимаем HTTP-клиенты, резолвер статусов и рендерер — по
         одному экземпляру на процесс (см. PollerDeps);
      4. поднимаем AsyncIOScheduler, вешаем поллинг и запускаем —
         ПОСЛЕ engine, потому что job поллера ходит в БД, и engine
         обязан быть готов к его первому тику.

    Shutdown (после yield), строго в обратном порядке:
      1. останавливаем scheduler — гасим тех, кто может обращаться
         к БД и сети, ДО того как всё это закроется. wait=False —
         не блокируемся на завершении текущих job'ов: цикл поллинга
         идемпотентен, оборванный тик повторится после рестарта без
         дублей в чате.
      2. закрываем HTTP-клиенты — освобождаем пулы соединений;
      3. engine.dispose() — корректно закрываем пул БД. Без этого
         uvicorn ругнётся warning'ом на незакрытые ресурсы.

    Settings уже лежат в app.state.settings — их туда положила
    фабрика create_app() ДО того, как lifespan запустился. Это важно:
    Settings нужны в момент валидации входящих параметров uvicorn
    (`--factory` создаёт app, а lifespan стартует позже, при первом
    ASGI-scope startup).
    """
    settings: Settings = app.state.settings

    logger.info("Приложение запускается, инициализация БД")
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    app.state.engine = engine
    app.state.session_factory = session_factory
    logger.info("БД инициализирована")

    logger.info("Инициализация клиентов Redmine и MAX")
    redmine_client = RedmineClient(
        base_url=settings.redmine_url,
        api_key=settings.redmine_api_key,
    )
    # Сертификат platform-api2.max.ru подписан УЦ Минцифры, которого нет
    # ни в certifi, ни в trust store Windows (якорь 4.3) — поэтому свой
    # CA-bundle. Контекст собираем здесь, на старте: httpx задепрекейтил
    # verify=<str>, а заодно битый путь к bundle уронит сервис сразу,
    # а не на первой отправке через час после деплоя.
    ssl_context = ssl.create_default_context(cafile=settings.max_ca_bundle_path)
    max_client = MaxClient(token=settings.max_token, verify=ssl_context)
    resolver = StatusResolver(
        redmine_client,
        ttl=timedelta(seconds=settings.status_cache_ttl_seconds),
    )
    # Ссылки в сообщениях должны вести на адрес, по которому Redmine
    # доступен пользователям, а он не обязан совпадать с тем, куда
    # ходит сервис. Если публичный не задан — берём рабочий.
    renderer = MessageRenderer(
        redmine_base_url=settings.redmine_base_url_public or settings.redmine_url,
    )

    app.state.redmine_client = redmine_client
    app.state.max_client = max_client

    logger.info("Запуск планировщика фоновых задач")
    deps = JobDeps(
        client=redmine_client,
        resolver=resolver,
        renderer=renderer,
        max_client=max_client,
        session_factory=session_factory,
        lookback=timedelta(seconds=settings.polling_lookback_seconds),
        due_date_threshold_days=settings.due_date_threshold_days,
    )
    scheduler = create_scheduler()
    register_poll_job(
        scheduler,
        deps,
        interval_seconds=settings.poll_interval_seconds,
    )
    register_due_date_job(scheduler, deps, hour=settings.due_date_job_hour)
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Планировщик запущен")

    try:
        yield
    finally:
        logger.info("Приложение остановлено, остановка планировщика")
        scheduler.shutdown(wait=False)
        logger.info("Закрытие HTTP-клиентов")
        await max_client.close()
        await redmine_client.aclose()
        logger.info("Закрытие БД")
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Собирает и возвращает экземпляр FastAPI.

    Параметр settings нужен ради тестов: тест подсовывает свой
    Settings с in-memory SQLite, минуя переменные окружения.

    В боевом запуске (uvicorn --factory) параметр опущен, Settings
    берётся из окружения через get_settings() — если DATABASE_URL
    не задан, поднимется ValidationError и uvicorn упадёт с внятной
    ошибкой ещё до открытия сокета. Это правильное поведение.
    """
    # Настройка логирования уровня приложения.
    # Библиотечные модули (MaxClient, RedmineClient, web/app.py)
    # пишут в logging.getLogger(__name__) и полагаются на настройку
    # снаружи. force=True нужен на случай повторного вызова create_app()
    # в тестах — без него basicConfig был бы no-op'ом.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )

    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="Redmine -> MAX Notifier",
        description=(
            "Сервис отправки событийных уведомлений из Redmine в мессенджер MAX."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # Settings кладём В app.state ДО lifespan — lifespan прочитает их
    # оттуда. Так весь стейт приложения централизован в одном месте,
    # а lifespan остаётся чистым от глобалов.
    app.state.settings = settings

    app.include_router(health_router)

    return app
