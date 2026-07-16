"""Обобщённый TTL-кэш «id → имя» для справочников Redmine.

Вырос из StatusResolver (этап 7c): статусы и приоритеты Redmine отдаёт
одинаково — весь справочник одним запросом, пары {id, name}, — и оба
резолвятся поллером ДО создания события (якорь 4.8), чтобы событие было
самодостаточным фактом, а шаблон в Redmine не ходил. Кэш-логика для них
одна (TTL на monotonic-часах, схлопывание конкурентных промахов через
lock + double-check), поэтому живёт в одном классе, параметризованном
функцией загрузки справочника.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Protocol

log = logging.getLogger(__name__)


class _NamedItem(Protocol):
    """Что угодно с числовым id и строковым именем (Status, Priority)."""

    id: int
    name: str


class NameResolver:
    """Асинхронный резолвер «id → имя» с кэшированием на время TTL.

    Не потокобезопасен, но безопасен для конкурентных корутин одного
    event loop'а: параллельные промахи схлопываются в один HTTP-запрос
    (см. _refresh).

    Загрузчик передаётся снаружи — одна логика на оба справочника:
        NameResolver(client.list_issue_statuses, ttl, label="статусов")
        NameResolver(client.list_issue_priorities, ttl, label="приоритетов")

    Экземпляр живёт столько же, сколько клиент Redmine, — создаётся один
    раз на приложение и переиспользуется job'ами поллера.
    """

    def __init__(
        self,
        fetch: Callable[[], Awaitable[Sequence[_NamedItem]]],
        ttl: timedelta,
        *,
        label: str,
    ) -> None:
        """
        Args:
            fetch: Корутина-загрузчик всего справочника, без аргументов.
                Обычно связанный метод клиента Redmine. Резолвер клиент
                не закрывает — владение остаётся за тем, кто клиент создал.
            ttl: Время жизни кэша (из Settings.status_cache_ttl_seconds).
            label: Имя справочника для логов ("статусов", "приоритетов").
        """
        if ttl.total_seconds() <= 0:
            raise ValueError(f"ttl должен быть положительным, получено {ttl}")

        self._fetch = fetch
        self._ttl_seconds = ttl.total_seconds()
        self._label = label

        # Кэш и отметка его заполнения. None означает "кэш ни разу
        # не заполнялся" — отличается от "заполнен и оказался пустым".
        self._cache: dict[int, str] = {}
        self._fetched_at: float | None = None

        # Lock, а не голая проверка: без него N корутин, промахнувшихся
        # одновременно, устроят N параллельных обновлений кэша.
        self._lock = asyncio.Lock()

    async def resolve(self, item_id: int) -> str | None:
        """Вернуть имя по id.

        Returns:
            Имя, либо None, если такого id в Redmine нет. None — не ошибка,
            а бизнес-факт: элемент справочника могли удалить уже после того,
            как он попал в journal старой задачи. Что делать — решает
            вызывающий код.

        Raises:
            RedmineError: любая ошибка клиента при обновлении кэша
                прокидывается наружу как есть. Протухший кэш при упавшем
                Redmine намеренно НЕ отдаём: тихо подставленное старое имя
                маскирует недоступность Redmine.
        """
        if not self._is_fresh():
            await self._refresh()

        # Промах по свежему кэшу — сразу None, без повторного запроса.
        # Иначе каждое событие с удалённым элементом било бы по API, а
        # реально новый элемент подтянется, когда истечёт TTL.
        return self._cache.get(item_id)

    def _is_fresh(self) -> bool:
        """Кэш заполнен и ещё не протух?

        Время меряем monotonic-часами, а не datetime.now(): настенные часы
        могут прыгнуть назад (NTP, перевод времени) и сломать арифметику
        TTL. monotonic — счётчик от старта процесса, назад не идёт.
        """
        if self._fetched_at is None:
            return False
        return (time.monotonic() - self._fetched_at) < self._ttl_seconds

    async def _refresh(self) -> None:
        """Перечитать справочник из Redmine и заменить кэш целиком."""
        async with self._lock:
            # Двойная проверка: пока мы стояли в очереди за локом, кэш мог
            # уже обновить тот, кто зашёл первым. Без этой строки очередь
            # из N корутин сделает N запросов подряд — лок бы их только
            # выстроил в цепочку, но не схлопнул.
            if self._is_fresh():
                return

            items = await self._fetch()

            # Присваиваем новый dict, а не мутируем старый: если запрос
            # выше упадёт, кэш останется в прежнем консистентном виде.
            self._cache = {item.id: item.name for item in items}
            self._fetched_at = time.monotonic()

            log.debug("кэш %s обновлён: %d шт.", self._label, len(self._cache))
