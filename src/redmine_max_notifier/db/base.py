"""Базовый класс для всех ORM-моделей проекта.

Все таблицы (PollingState, SentNotification, Routing) наследуются от
общего Base. Через `Base.metadata` SQLAlchemy знает полный список
таблиц — это критично для Alembic (Этап 6d), который сравнивает
описанные в коде таблицы с реальной схемой БД и генерирует миграции.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Общий предок всех ORM-моделей.

    Пустой класс — нам нужен только сам факт наследования от
    DeclarativeBase. Атрибуты `metadata` и `registry` появляются
    у Base автоматически, их выставляет SQLAlchemy.

    Позже сюда можно будет положить общие для всех таблиц вещи
    (стандартные колонки created_at/updated_at через миксин,
    naming_convention для внешних ключей и т.д.) — но не сейчас.
    """
