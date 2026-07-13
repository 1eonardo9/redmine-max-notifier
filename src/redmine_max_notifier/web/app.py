"""Собирает фабрику async-сессий, привязанную к engine.

Возвращает callable: SessionLocal() → новая AsyncSession.

expire_on_commit=False — важный дефолт для async.
По умолчанию SQLAlchemy после commit() помечает все объекты
как "устаревшие" и при следующем обращении к их атрибутам
подтягивает свежие данные из БД. В async-мире это ловушка:
невинное чтение `obj.some_attr` после commit() внезапно
становится async-операцией, которую нельзя await'ить внутри
обычного property. Отключаем — после commit объекты остаются
валидными до конца сессии.
"""

from __future__ import annotations

import logging
from _collections_abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from redmine_max_notifier.config import Settings, get_settings
from redmine_max_notifier.db.engine import create_engine, create_session_factory
from redmine_max_notifier.web.routes.health import router as health_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Хук жизненного цикла ASGI-приложения.

    Startup (до yield):
      - создаём AsyncEngine из Settings, сохраняем в app.state.engine;
      - создаём фабрику сессий, сохраняем в app.state.session_factory.

    Shutdown (после yield):
      - вызываем engine.dispose(), чтобы корректно закрыть пул
        соединений. Без этого на graceful shutdown в логах будет
        предупреждение о незакрытых ресурсах.

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

    try:
        yield
    finally:
        logger.info("Приложение остановлено, закрытие БД")
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
