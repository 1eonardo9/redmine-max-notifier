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

from redmine_max_notifier.maxbot.models import (
    BotInfo,
    MessageFormat,
    NewMessageBody,
    SentMessage,
    UpdatesResponse,
)

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
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, object]:
        """Универсальный HTTP-запрос к MAX API.

        Все публичные методы клиента должны идти через этот метод — здесь
        единая точка для логирования, обработки ошибок и (в 2d) ретраев.

        Args:
            method: HTTP-метод ("GET", "POST", ...).
            path: путь эндпоинта относительно base_url.
            params: query-параметры.
            json: тело запроса (будет сериализовано в JSON).
            timeout: точечное переопределение таймаута для этого запроса.
                Нужно для long polling: get_updates ждёт ответа десятки секунд,
                стандартного 15-секундного read timeout не хватит.
                Если None — используется глобальный timeout AsyncClient.

        Returns:
            Распарсенный JSON-ответ как словарь.
        """
        logger.debug("MAX API запрос: %s %s params=%s", method, path, params)

        # httpx.USE_CLIENT_DEFAULT — специальный sentinel, означающий
        # "не переопределяй, возьми из AsyncClient". Именно так httpx
        # различает "явно None (без таймаута)" и "оставь как есть".
        request_timeout: httpx.Timeout | httpx._client.UseClientDefault = (
            timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
        )

        response = await self._client.request(
            method=method,
            url=path,
            params=params,
            json=json,
            timeout=request_timeout,
        )

        logger.debug("MAX API ответ: %s %s -> %d", method, path, response.status_code)
        response.raise_for_status()

        data: dict[str, object] = response.json()
        return data

    async def get_me(self) -> BotInfo:
        """Получить информацию о самом боте — эквивалент "пинга".

        Метод MAX API: ``GET /me``.

        Если токен валиден — вернётся ``BotInfo`` с полями бота.
        Если токен неверный или отозван — прилетит 401 (HTTPStatusError).

        Пригодится на старте приложения как sanity-check конфигурации
        и в smoke-тестах.

        Returns:
            Типизированная модель ``BotInfo`` с данными бота.
        """
        # _request возвращает сырой словарь. Pydantic-модель строится
        # через model_validate — она сама разберётся с типами полей
        # и вызовет наши валидаторы (в частности, _parse_millis_timestamp
        # для last_activity_time).
        raw = await self._request("GET", "/me")
        return BotInfo.model_validate(raw)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        format: MessageFormat | None = None,
    ) -> SentMessage:
        """Отправить сообщение в чат.

        Метод MAX API: ``POST /messages?chat_id={chat_id}``.

        Args:
            chat_id: ID чата или пользователя. Для чата — отрицательное число
                (историческая договорённость MAX, аналог telegram-supergroup id);
                для лички с пользователем — положительное. ID группы получаем
                через helper-скрипт на 2f (long polling GET /updates + событие
                bot_added).
            text: текст сообщения. Максимальная длина по докам — 4000 символов.
                Клиент длину не валидирует; при превышении MAX вернёт 400,
                который в 2d превратится в MaxValidationError.
            format: разметка текста. Если None — MAX отобразит plain text.

        Returns:
            ``SentMessage`` — распарсенный ответ MAX c ID отправленного сообщения.

        Raises:
            httpx.HTTPStatusError: при не-2xx ответе. В 2d заменим на
                собственную иерархию.
        """
        # Собираем тело запроса через Pydantic-модель, а не голым словарём.
        # Плюсы: mypy проверит типы, exclude_none уберёт неустановленные
        # опциональные поля (не будем слать "format": null, если он не задан —
        # некоторые API на такое ругаются 400-й).
        body = NewMessageBody(text=text, format=format)

        raw = await self._request(
            "POST",
            "/messages",
            params={"chat_id": chat_id},
            json=body.model_dump(mode="json", exclude_none=True),
        )
        return SentMessage.model_validate(raw)

    async def close(self) -> None:
        """Закрыть внутренний HTTP-клиент и освободить пул соединений."""
        await self._client.aclose()

    async def get_updates(
        self,
        *,
        marker: int | None = None,
        timeout: int = 30,
        limit: int = 100,
    ) -> UpdatesResponse:
        """Получить входящие события через long polling.

        Метод MAX API: ``GET /updates``.

        Как работает:
        1. Клиент отправляет запрос с marker (позиция в потоке событий)
           и timeout (максимальное время ожидания на стороне сервера).
        2. Если события уже есть — MAX сразу возвращает их пачкой.
        3. Если событий нет — MAX держит соединение открытым до timeout секунд,
           после чего вернёт пустой список.
        4. Клиент повторяет с новым marker из ответа.

        В боевом сервисе мы этот метод НЕ используем — сервис outbound-only,
        входящие события не обрабатывает. Метод нужен для helper-скрипта
        (2f): один раз запустить long polling, поймать событие bot_added
        при добавлении бота в тестовую группу, вытащить chat_id.

        Args:
            marker: курсор позиции. None (по умолчанию) — начать с текущих
                свежих событий. Для последующих вызовов передавать marker
                из ответа предыдущего запроса.
            timeout: сколько секунд сервер держит соединение, ожидая события.
                MAX разрешает до 90 сек. Дефолт 30 — компромисс между
                отзывчивостью и снижением числа запросов.
            limit: максимум событий в одном ответе (защита от лавины,
                если бота добавили в чат с длинной историей активности).

        Returns:
            ``UpdatesResponse`` — список событий и новый marker для
            следующего запроса.
        """
        # ВАЖНО: HTTP read_timeout httpx должен быть БОЛЬШЕ, чем серверный
        # timeout, иначе httpx оборвёт соединение раньше, чем MAX решит
        # ответить, и мы будем ловить httpx.ReadTimeout вместо ответа.
        # Запас в 10 секунд — с большим запасом на сетевые задержки.
        http_timeout = httpx.Timeout(
            connect=DEFAULT_TIMEOUT.connect,
            read=timeout + 10,
            write=DEFAULT_TIMEOUT.write,
            pool=DEFAULT_TIMEOUT.pool,
        )

        # Формируем query-параметры. Оставляем только заданные — если
        # marker=None, вообще не отправляем этот параметр.
        params: dict[str, str | int] = {
            "timeout": timeout,
            "limit": limit,
        }
        if marker is not None:
            params["marker"] = marker

        raw = await self._request(
            "GET", "/updates", params=params, timeout=http_timeout
        )
        return UpdatesResponse.model_validate(raw)

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
