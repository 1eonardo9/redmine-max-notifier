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
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from redmine_max_notifier.jobs import JobDeps, run_due_date_cycle, run_poll_cycle

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


def register_poll_job(
    scheduler: AsyncIOScheduler,
    deps: JobDeps,
    *,
    interval_seconds: int,
) -> None:
    """Повесить регулярный поллинг Redmine на планировщик.

    Отдельной функцией, а не внутри create_scheduler(): фабрика
    планировщика не должна знать про клиентов Redmine и MAX, а тестам
    удобно поднимать голый scheduler без единого похода в сеть.

    Дефолты job'ов из create_scheduler() тут работают на нас:
    max_instances=1 не даст двум циклам одновременно бодаться за
    строку polling_state, а coalesce=True схлопнет накопившиеся тики
    в один, если event loop подвисал.

    Аргумент deps передаём через args — APScheduler вызовет
    run_poll_cycle(deps). Замыкание тут тоже сработало бы, но так
    job остаётся обычной функцией, которую видно в jobs.py.
    """
    scheduler.add_job(
        run_poll_cycle,
        trigger=IntervalTrigger(seconds=interval_seconds),
        args=(deps,),
        id="poll_recent_changes",
        name="Поллинг свежих изменений Redmine",
        replace_existing=True,
    )
    logger.info("поллинг Redmine зарегистрирован: раз в %dс", interval_seconds)


def register_due_date_job(
    scheduler: AsyncIOScheduler,
    deps: JobDeps,
    *,
    hour: int,
) -> None:
    """Повесить суточную проверку дедлайнов на планировщик.

    CronTrigger(hour=N, minute=0) — «каждый день в N:00». Часовой пояс
    не указываем: APScheduler возьмёт локальный пояс сервера, а
    due_date_job_hour и задуман как «час по местному времени» (9 утра —
    начало рабочего дня). Прибей мы сюда UTC, на сервере с UTC+3
    напоминания уезжали бы в 12:00.

    misfire_grace_time из job_defaults (30 секунд) для суточного job'а
    строговат: если сервис перезапускали ровно в 9:00, тик пропадёт
    до завтра. Ставим час — напоминание о дедлайне не протухает от
    того, что приехало в 9:40.
    """
    scheduler.add_job(
        run_due_date_cycle,
        trigger=CronTrigger(hour=hour, minute=0),
        args=(deps,),
        id="due_date_approaching",
        name="Ежедневная проверка дедлайнов",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("проверка дедлайнов зарегистрирована: ежедневно в %d:00", hour)
