"""Фабрика ASGI-приложения FastAPI.

На этом этапе приложение делает ровно одно: поднимается,
отвечает на GET /health и корректно завершается. На Этапе 6
в lifespan подтянется инициализация БД, на Этапе 7 —
запуск APScheduler.
"""

from __future__ import annotations

import logging
from _collections_abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from redmine_max_notifier.web.routes.health import router as health_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Хук жизненного цикла ASGI-приложения.

    Код ДО yield выполняется один раз на старте — после того, как
    uvicorn биндит сокет, но до того, как принят первый запрос.
    Код ПОСЛЕ yield — на graceful shutdown (SIGTERM/SIGINT).

    Сейчас пусто, только логирование. На Этапе 6 сюда придёт
    подключение к БД (engine + session factory), на Этапе 7 —
    старт и остановка APScheduler.
    """
    logger.info("Приложение запускается")
    yield
    logger.info("Приложение остановлено")


def create_app() -> FastAPI:
    """Собирает и возвращает экземпляр FastAPI.

    Фабрика (а не модульный глобал `app = FastAPI()`) нужна,
    чтобы:
    - в тестах собирать свежий инстанс под каждый тест-модуль
      без состояния от предыдущих тестов;
    - в будущем прокидывать Settings/зависимости параметрами,
      а не через модульные глобалы;
    - dev- и test-инстансы могли жить рядом без конфликтов.
    """

    # Настройка логирования уровня приложения.
    # Библиотечные модули (MaxClient, RedmineClient, наш web/app.py)
    # пишут в logging.getLogger(__name__) и полагаются на то, что
    # уровень и хендлеры настроены снаружи. basicConfig() создаёт
    # StreamHandler на root-логгере и выставляет INFO.
    #
    # force=True нужен на случай повторного вызова create_app()
    # в тестах — без него basicConfig был бы no-op'ом, если
    # обработчики уже добавлены.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )

    app = FastAPI(
        title="Redmine -> MAX Notifier",
        description=(
            "Сервис отправки событийных уведомлений из Redmineв мессенджер MAX."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # Подключаем роутеры. Каждый роутер — отдельный модуль в web/routes/.
    # По мере роста сервиса сюда добавятся admin-роутер и, возможно,
    # приёмник callback'ов от MAX.
    app.include_router(health_router)

    return app
