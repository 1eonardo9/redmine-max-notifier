"""TTL-кэш «id статуса задачи → имя статуса».

Зачем нужен: journal-запись Redmine отдаёт только id старого и нового
статуса (details.old_value / details.new_value — причём строками), а в
StatusChangedEvent должны лежать готовые человекочитаемые имена. Резолв
делает поллер до создания события — событие обязано быть самодостаточным
фактом, шаблонизатор в Redmine не ходит (см. якорь 4.8 в CLAUDE.md).

Кэш обновляется целиком: Redmine отдаёт все статусы одним запросом
(их обычно 5-15), поштучный резолв не имеет смысла.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from redmine_max_notifier.redmine.client import RedmineClient

log = logging.getLogger(__name__)


class StatusResolver:
    """Асинхронный резолвер имён статусов с кэшированием на время TTL.

    Не потокобезопасен, но безопасен для конкурентных корутин одного
    event loop'а: параллельные промахи схлопываются в один HTTP-запрос
    (см. _refresh).

    Экземпляр живёт столько же, сколько клиент Redmine, — создаётся один
    раз на приложение и переиспользуется всеми job'ами поллера.
    """

    def __init__(
        self,
        client: RedmineClient,
        ttl: timedelta,
    ) -> None:
        """
        Args:
            client: Клиент Redmine. Резолвер его не закрывает — владение
                остаётся за тем, кто клиент создал (lifespan приложения).
            ttl: Время жизни кэша. Берётся из Settings:
                StatusResolver(
                    client,
                    timedelta(seconds=settings.status_cache_ttl_seconds),
                )
        """
        if ttl.total_seconds() <= 0:
            raise ValueError(f"ttl должен быть положительным, получено {ttl}")

        self._client = client
        self._ttl_seconds = ttl.total_seconds()

        # Кэш и отметка его заполнения. None означает "кэш ни разу
        # не заполнялся" — отличается от "заполнен и оказался пустым".
        self._cache: dict[int, str] = {}
        self._fetched_at: float | None = None

        # Lock, а не голая проверка: без него N корутин, промахнувшихся
        # одновременно, устроят N параллельных обновлений кэша.
        self._lock = asyncio.Lock()

    async def resolve(self, status_id: int) -> str | None:
        """Вернуть имя статуса по id.

        Returns:
            Имя статуса, либо None, если такого статуса в Redmine нет.
            None — не ошибка, а бизнес-факт: статус могли удалить уже
            после того, как он попал в journal старой задачи. Что с этим
            делать, решает вызывающий код.

        Raises:
            RedmineError: любая ошибка клиента при обновлении кэша
                прокидывается наружу как есть. Протухший кэш при упавшем
                Redmine намеренно НЕ отдаём: тихо подставленное старое
                имя маскирует недоступность Redmine.
        """
        if not self._is_fresh():
            await self._refresh()

        # Промах по свежему кэшу — сразу None, без повторного запроса.
        # Иначе каждое событие с удалённым статусом било бы по API, а
        # реально новый статус и так подтянется, когда истечёт TTL.
        return self._cache.get(status_id)

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
        """Перечитать все статусы из Redmine и заменить кэш целиком."""
        async with self._lock:
            # Двойная проверка: пока мы стояли в очереди за локом, кэш мог
            # уже обновить тот, кто зашёл первым. Без этой строки очередь
            # из N корутин сделает N запросов подряд — лок бы их только
            # выстроил в цепочку, но не схлопнул.
            if self._is_fresh():
                return

            statuses = await self._client.list_issue_statuses()

            # Присваиваем новый dict, а не мутируем старый: если запрос
            # выше упадёт, кэш останется в прежнем консистентном виде.
            self._cache = {s.id: s.name for s in statuses}
            self._fetched_at = time.monotonic()

            log.debug("кэш статусов обновлён: %d шт.", len(self._cache))
