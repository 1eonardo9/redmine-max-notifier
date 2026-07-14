"""Тесты жизненного цикла AsyncIOScheduler внутри lifespan FastAPI.

Что здесь проверяется:
  1. После входа в `lifespan_context` в app.state.scheduler лежит
     запущенный AsyncIOScheduler.
  2. Внутри контекста зарегистрирован job 'heartbeat' с
     IntervalTrigger — это гарантия, что фабрика create_scheduler()
     действительно навесила маячок, а не вернула пустой scheduler.
  3. После выхода из контекста scheduler остановлен
     (running == False). Плюс лог фиксирует старт и остановку.

Чего здесь НЕТ и почему:
  Тесты НЕ ждут фактического срабатывания heartbeat'а. Trigger стоит
  на 60 секунд, ждать реальный тик — значит подвесить весь прогон
  на минуту ради одной строчки лога. Достаточно факта, что job
  зарегистрирован в scheduler'e — то, что APScheduler по своему
  триггеру действительно вызовет функцию, гарантирует сама библиотека
  и её собственные тесты.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI


async def test_scheduler_is_running_inside_lifespan(app_raw: FastAPI) -> None:
    """Внутри lifespan_context scheduler жив и запущен.

    app_raw даёт сырое приложение — сами прогоняем lifespan, чтобы
    поймать момент «уже startup прошёл, shutdown ещё нет».
    """
    async with app_raw.router.lifespan_context(app_raw):
        scheduler = app_raw.state.scheduler

        assert isinstance(scheduler, AsyncIOScheduler)
        assert scheduler.running is True


async def test_heartbeat_job_is_registered(app_raw: FastAPI) -> None:
    """Job 'heartbeat' зарегистрирован и висит на IntervalTrigger.

    Проверяем как факт наличия job'а с ожидаемым id, так и тип
    триггера — если кто-то нечаянно поменяет IntervalTrigger на
    CronTrigger без обновления теста, регрессия будет поймана.
    """
    async with app_raw.router.lifespan_context(app_raw):
        scheduler: AsyncIOScheduler = app_raw.state.scheduler
        job = scheduler.get_job("heartbeat")

        assert job is not None, "heartbeat job должен быть зарегистрирован"
        assert isinstance(job.trigger, IntervalTrigger)


async def test_scheduler_is_stopped_after_lifespan(app_raw: FastAPI) -> None:
    """После выхода из lifespan_context scheduler остановлен.

    Инвариант ресурсной гигиены: как engine.dispose() не должен
    оставлять коннекты БД, так и scheduler.shutdown() не должен
    оставлять фоновой контур, иначе на graceful shutdown в проде
    приложение будет висеть.

    Тонкость AsyncIOScheduler 3.x. Его метод shutdown() декорирован
    @run_in_event_loop и реальный переход в STATE_STOPPED происходит
    не в момент возврата scheduler.shutdown(), а в следующей итерации
    event loop'а — через loop.call_soon_threadsafe. Поэтому проверка
    сразу после выхода из lifespan_context ложно провалится:
    scheduler.running всё ещё True, потому что отложенный callback
    не успел отработать. Уступаем управление loop'у до тех пор, пока
    state не переключится, но не больше 10 tick'ов подряд — иначе
    регрессия «shutdown вообще не вызвался» пройдёт незамеченной.

    В проде event loop гасится сразу после lifespan, поэтому
    отложенный на один tick shutdown никаких проблем не создаёт —
    scheduler всё равно умирает вместе с loop'ом.
    """
    async with app_raw.router.lifespan_context(app_raw):
        scheduler: AsyncIOScheduler = app_raw.state.scheduler

    for _ in range(10):
        if not scheduler.running:
            break
        await asyncio.sleep(0)

    assert scheduler.running is False


async def test_lifespan_logs_scheduler_start_and_shutdown(
    app_raw: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup и shutdown scheduler'а отражены в логах.

    Проверяем подстроки, а не точный текст — сообщения могут
    получать косметические правки (см. существующий тест
    test_lifespan_logs_startup_and_shutdown).
    """
    caplog.set_level(logging.INFO, logger="redmine_max_notifier.web.app")

    async with app_raw.router.lifespan_context(app_raw):
        pass

    messages = [record.message for record in caplog.records]
    assert any("планировщик" in m.lower() for m in messages), (
        "Ожидался лог о запуске планировщика"
    )
    assert any("остановка планировщика" in m.lower() for m in messages), (
        "Ожидался лог об остановке планировщика"
    )
