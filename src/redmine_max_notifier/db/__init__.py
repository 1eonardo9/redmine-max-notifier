"""Публичный API пакета db.

Отсюда Alembic (env.py на этапе 6d) и остальной код проекта импортируют
Base и ORM-модели. Импорт моделей здесь ВАЖЕН: пока модуль models
не импортирован, соответствующих таблиц нет в Base.metadata — а
именно по Base.metadata Alembic сравнивает "что описано в коде"
с "что есть в БД" для генерации миграций.
"""

from __future__ import annotations

from redmine_max_notifier.db.base import Base
from redmine_max_notifier.db.models import PollingState, Routing, SentNotification

__all__ = [
    "Base",
    "PollingState",
    "Routing",
    "SentNotification",
]
