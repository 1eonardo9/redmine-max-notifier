"""Фабрики async-engine и async-sessionmaker для SQLAlchemy 2.0.

Engine — один на приложение, живёт от старта до shutdown, держит
пул физических соединений с БД. Session — короткоживущая «рабочая
тетрадь», создаётся под каждую единицу работы (HTTP-запрос, цикл
поллера, тест) через sessionmaker.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from redmine_max_notifier.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Создаёт AsyncEngine из настроек.

    Один экземпляр на приложение — не зови эту функцию из хендлеров
    или поллера. Правильное место вызова — lifespan create_app().

    echo=False: не логировать каждый SQL-запрос в stdout. В dev-режиме
    удобно временно поднять до True — увидишь всё, что SQLAlchemy
    отправляет в БД.

    pool_pre_ping=True: перед выдачей соединения из пула SQLAlchemy
    делает лёгкую проверку "SELECT 1". Стоит копейки, спасает от
    "закрытых с той стороны" соединений — типичная беда PostgreSQL
    за файрволом, который рвёт idle-коннекты по таймауту.
    """
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
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
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )
