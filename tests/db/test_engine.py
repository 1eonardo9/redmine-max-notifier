"""Регрессионные тесты фабрик engine.py.

Тесты моделей ((test_polling_state, test_sent_notifications, test_routing))
использовали async_sessionmaker напрямую и не проходили через
create_session_factory() — из-за чего опечатка в её аргументах могла
существовать сколько угодно без единого падающего теста. Этот файл
закрывает дыру: минимальный тест «фабрика открывает рабочую сессию».
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from redmine_max_notifier.db.engine import create_session_factory


async def test_create_session_factory_opens_working_session(
    db_engine: AsyncEngine,
) -> None:
    """create_session_factory возвращает фабрику, чьи сессии
    реально привязаны к engine и умеют выполнять запросы.

    Ловит именно ту ошибку, которую пропустили тесты 6e:
    опечатку в имени аргумента bind → фабрика создаётся, но
    её сессии при первом же execute() падают с ArgumentError.
    """
    factory = create_session_factory(db_engine)

    async with factory() as session:
        result = await session.execute(select(1))
        assert result.scalar_one() == 1
