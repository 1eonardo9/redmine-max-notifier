"""Async HTTP-клиент для MAX Bot API.

Обёртка вокруг httpx.AsyncClient, инкапсулирующая:
- базовый URL API,
- заголовок авторизации (Authorization: <token>, без Bearer),
- обязательный Content-Type: application/json,
- гранулярные таймауты,
- поддержку async context manager (async with).
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Self

import httpx

# База URL продакш-API MAX.
DEFAULT_BASE_URL = "https://platform-api2.max.ru"

# Гранулярные таймауты httpx: connect, read, write, pool.
# connect — успеть установить TCP+TLS.
# read — успеть получить ответ от API после отправки запроса.
# Значения подобраны с запасом; при необходимости переопределяем через параметр.
DEFAULT_TIMEOUT = httpx.Timeout(
    connect=5.0,
    read=15.0,
    write=10.0,
    pool=5.0,
)

logger = logging.getLogger(__name__)


class MaxClient:
    """Async-клиент к MAX Bot API.

    Использование через async context manager:

        async with MaxClient(token="...") as client:
            me = await client.get_me()

    Ручное управление жизненным циклом:

        client = MaxClient(token="...")
        try:
            me = await client.get_me()
        finally:
            await client.close()
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: httpx.Timeout | None = None,
        verify: bool | str = True,
    ) -> None:
        """Инициализация клиента.

        Args:
            token: токен бота, выданный @MasterBot. Передаётся в заголовке
                ``Authorization`` без префикса ``Bearer`` — это требование MAX API.
            base_url: базовый URL API. По умолчанию — прод platform-api2.max.ru.
            timeout: настройка таймаутов httpx. Если ``None`` — используем
                значения по умолчанию (см. ``DEFAULT_TIMEOUT``).
            verify: настройка TLS-верификации, пробрасывается в httpx.
                Возможные значения:
                - True — использовать стандартный CA-bundle certifi
                  (подходит для сайтов с сертификатами западных CA).
                - str — путь к кастомному PEM-файлу с корневыми CA.
                  Нужно для MAX API: их сертификат подписан национальным
                  CA Минцифры РФ, которого нет в certifi. Используй
                  scripts/build_ca_bundle.py чтобы собрать расширенный bundle.
                - False — отключить верификацию. НЕ ИСПОЛЬЗУЙ В ПРОДЕ,
                  только для отладки в контролируемом окружении.
        """
        if not token:
            #  Валидация на входе - лучше упасть с понятным сообщение,
            # чем ловить 401 от API уже во время запроса.
            raise ValueError("токен не может быть пустым!!!")

        self._token = token
        self._base_url = base_url.rstrip("/")  # Убираем хвостовой слэш, на всякий.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                # Голый токен, требование MAX API.
                "Authorization": self._token,
                # Все запросы к API идут в JSON.
                "Content-Type": "application/json",
                # User-Agent
                "User-Agent": "redmine-max_notifier/0.1 (+httpx)",
            },
            timeout=timeout or DEFAULT_TIMEOUT,
            trust_env=False,
            verify=verify,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Универсальный HTTP-запрос к MAX API.

        Все публичные методы клиента (get_me, send_message, get_updates и т.д.)
        должны идти через этот метод — здесь единая точка для логирования,
        обработки ошибок и (в 2d) ретраев.

        Args:
            method: HTTP-метод ("GET", "POST", ...).
            path: путь эндпоинта относительно base_url, например "/me" или "/messages".
            params: query-параметры (например, chat_id для POST /messages).
            json: тело запроса, будет сериализовано в JSON.

        Returns:
            Распарсенный JSON-ответ как словарь.

        Raises:
            httpx.HTTPStatusError: если сервер вернул не-2xx статус.
                (В 2d заменим на собственную иерархию MaxError.)
            httpx.TransportError: сетевые ошибки — таймауты, обрыв соединения.
                (В 2d обернём в MaxTransportError и добавим ретраи.)
        """
        # ВАЖНО: логируем только method+path, никогда — заголовки.
        # В headers лежит наш токен, слив в лог будет утечкой доступа к боту.
        logger.debug("MAX API запрос: %s %s params=%s", method, path, params)

        response = await self._client.request(
            method=method,
            url=path,
            params=params,
            json=json,
        )

        # Логируем результат до raise_for_status — чтобы даже при ошибке
        # в логе остался статус.
        logger.debug("MAX API ответ: %s %s -> %d", method, path, response.status_code)

        # Пока — стандартное поведение httpx: при не-2xx бросается HTTPStatusError.
        # В 2d заменим на маппинг статусов в собственные исключения
        # (MaxAuthError для 401, MaxRateLimitError для 429 и т.д.).
        response.raise_for_status()

        # Все успешные ответы MAX API — это JSON.
        # response.json() возвращает Any; аннотируем результат как dict.
        # На случай, если API вдруг вернёт список (get_updates, например) —
        # переделаем сигнатуру в 2c, когда до этого дойдём.
        data: dict[str, object] = response.json()
        return data

    async def get_me(self) -> dict[str, object]:
        """Получить информацию о самом боте — эквивалент "пинга".

        Метод MAX API: ``GET /me``.

        Если токен валиден — вернётся словарь с полями бота
        (user_id, first_name, username, is_bot и т.д.).
        Если токен неверный или отозван — прилетит 401 (HTTPStatusError).

        Пригодится на старте приложения как sanity-check конфигурации
        и в smoke-тестах.

        Returns:
            Сырой словарь с полями бота. В 2b обернём в Pydantic-модель ``BotInfo``.
        """
        return await self._request("GET", "/me")

    async def close(self) -> None:
        """Закрыть внутренний HTTP-клиент и освободить пул соединений."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        """Вход в async context manager. Возвращает сам клиент."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Выход из async context manager — гарантированно закрывает клиент."""
        await self.close()
