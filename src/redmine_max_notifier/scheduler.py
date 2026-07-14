"""APScheduler-обвязка для фоновых задач сервиса.

Ответственность модуля:
  - собрать `AsyncIOScheduler` с разумными дефолтами job'ов;
  - зарегистрировать «маячок живости» (heartbeat), который раз в минуту
    пишет в лог `tick`. Пока это единственный job — на этапах 7f/7g
    к нему прибавятся регулярный поллинг и суточная проверка дедлайнов.

Почему AsyncIOScheduler, а не BackgroundScheduler.
  У нас весь стек async (FastAPI + SQLAlchemy async + httpx.AsyncClient).
  AsyncIOScheduler живёт в текущем event-loop'e: async-функции он вызовет
  через `await` без прыжков между потоками, а значит объекты БД-сессии,
  задачи `httpx` и прочий стейт event-loop'а останутся консистентными.
  BackgroundScheduler крутит job'ы в отдельных потоках — для нашего стека
  это чужой мир, каждый await из такого потока пришлось бы прогонять
  через `asyncio.run_coroutine_threadsafe`. Не наш путь.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


async def _heartbeat() -> None:
    """Маячок живости планировщика.

    Пока настоящих поллер-job'ов нет, эта функция — единственное
    доказательство, что scheduler реально крутится, а не висит
    молчаливым трупом. На этапе 7f её можно оставить как есть
    (лишний INFO раз в минуту не мешает) или снять — как удобнее.
    """
    logger.info("tick")


def create_scheduler() -> AsyncIOScheduler:
    """Собирает `AsyncIOScheduler` с настройками под наш проект.

    Возвращает **не запущенный** scheduler — start() отдельным шагом,
    чтобы lifespan мог контролировать порядок инициализации (сначала
    engine БД, потом scheduler; на shutdown — наоборот).

    Дефолты job'ов (`job_defaults`):
      - coalesce=True — если несколько запусков job'а «наслоились»
        (event-loop подвис, был долгий GC, дебагер и т.д.),
        объединяем их в один. Иначе после разморозки прилетит залп
        отложенных вызовов, что для поллинга Redmine нежелательно.
      - max_instances=1 — не запускать второй инстанс job'а, пока
        предыдущий не отработал. Защита от «сеть тупит, poll идёт
        75 секунд, а таймер уже зовёт следующий» — иначе два поллера
        поборются за одну строку `polling_state` в БД.
      - misfire_grace_time=30 — если запуск опоздал более чем на
        30 секунд (например, приложение только что стартануло),
        пропускаем этот тик и ждём следующего. Разумный компромисс
        для минутного цикла.

    Регистрируется один job — heartbeat раз в минуту. Настоящие
    job'ы поллера прикрутит этап 7f (регулярный) и 7g (суточный).
    """
    scheduler = AsyncIOScheduler(
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 30,
        },
    )

    # IntervalTrigger(minutes=1) — «каждые 60 секунд от start()».
    # id="heartbeat" — стабильный идентификатор, чтобы можно было
    # адресоваться к этому job'у извне (например, снять для теста).
    # replace_existing=True — если по какой-то причине job уже
    # зарегистрирован (пересоздание scheduler в тестах), не падать,
    # а перезаписать. На проде это не сработает — там scheduler один.
    scheduler.add_job(
        _heartbeat,
        trigger=IntervalTrigger(minutes=1),
        id="heartbeat",
        name="Heartbeat (маячок живости)",
        replace_existing=True,
    )

    return scheduler
