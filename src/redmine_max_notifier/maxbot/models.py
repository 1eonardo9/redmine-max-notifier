"""Pydantic-модели для ресурсов и ответов MAX Bot API.

Все модели наследуются от MaxModel — базы с общими правилами.
Модели зеркалят структуру JSON-ответов MAX; отсутствующие в JSON
опциональные поля становятся None.

Соответствие эндпоинтов моделям:
    GET /me -> BotInfo
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class MaxModel(BaseModel):
    """Базовая модель для всех Pydantic-моделей MAX Bot API.

    Общие правила через ``model_config``:

    - ``extra="ignore"`` — если MAX добавит в JSON новое поле, которого нет
      в модели, оно молча игнорируется, а не роняет парсинг. Это защищает
      клиент от несовместимых изменений API. Тот же приём применён в
      RedmineModel (см. мастер-промт, "Накопленные технические решения").

    - ``frozen=True`` — экземпляры моделей неизменяемы после создания.
      Это делает их безопасными для передачи между корутинами и упрощает
      рассуждение о состоянии: получил объект — он не изменится под ногами.

    - ``populate_by_name=True`` — если позже понадобится переименовать
      Python-поле через ``Field(alias=...)``, можно будет передавать
      значение и по алиасу (имя из JSON), и по Python-имени. Задел на будущее.
    """

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )


class BotInfo(MaxModel):
    """Ответ ``GET /me`` — информация о самом боте.

    Поле ``last_activity_time`` в MAX API приходит как Unix-timestamp
    В МИЛЛИСЕКУНДАХ (не секундах!). Валидатор ниже конвертирует его
    в timezone-aware ``datetime`` (UTC), чтобы дальше по коду работать
    с нормальным типом, а не с голым числом.
    """

    user_id: int
    first_name: str
    username: str
    is_bot: bool

    # datetime вместо int — конвертация ниже, в валидаторе.
    last_activity_time: datetime

    # Опциональные поля — MAX может их не прислать (например, у бота
    # не заполнено описание или отключена аватарка).
    description: str | None = None
    avatar_url: str | None = None
    full_avatar_url: str | None = None
    name: str | None = None

    @field_validator("last_activity_time", mode="before")
    @classmethod
    def _parse_millis_timestamp(cls, value: object) -> object:
        """Преобразовать Unix-timestamp в миллисекундах в datetime (UTC).

        Как это работает:

        1. Pydantic-валидатор с ``mode="before"`` вызывается ДО стандартной
           валидации поля. То есть на входе — сырое значение из JSON
           (int или что-то ещё), на выходе — то, что Pydantic будет пытаться
           привести к целевому типу (в нашем случае datetime).

        2. Если пришло число — считаем, что это миллисекунды, и делим на 1000
           для получения секунд. ``datetime.fromtimestamp(..., tz=UTC)``
           даёт timezone-aware объект — это правильная практика,
           naive-datetime (без tz) в проде — источник вечных багов.

        3. Если пришло что-то другое (строка ISO, уже datetime) — не трогаем,
           отдаём как есть. Pydantic сам разберётся стандартным парсером.
           Это делает валидатор устойчивым, если MAX однажды сменит формат.

        Аргумент ``value`` типизирован как ``object``, а не ``Any`` —
        в проекте включён строгий mypy, ``Any`` отключает проверки.
        ``object`` заставляет явно проверить тип перед использованием
        (``isinstance(value, int | float)``).
        """
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        return value


class MessageFormat(StrEnum):
    """Формат разметки текста сообщения.

    StrEnum (Python 3.11+) — это Enum, где каждое значение при str()
    возвращает свою строку. Удобно для API, где значения передаются
    как строки: MessageFormat.MARKDOWN превращается в "markdown"
    автоматически, когда попадает в JSON.

    Плюс type-safety: если случайно написать format="markdon" (опечатка),
    mypy этого не поймает — а с enum поймает сразу.
    """

    MARKDOWN = "markdown"
    HTML = "html"


class NewMessageBody(MaxModel):
    """Тело POST /messages — то, что мы отправляем в чат.

    Из документации MAX API поддерживаются также поля attachments,
    link, notify, но пока они нам не нужны — добавим по требованию
    (правило "не тащить в модель всё подряд").
    """

    text: str
    # None означает что MAX сам решает как отобразить, обычно plain-text
    format: MessageFormat | None = None


class MessageBody(MaxModel):
    """Тело сообщения (message.body в ответах MAX).

    Пока держим только mid и seq — этого хватает, чтобы подтвердить
    отправку и позже искать сообщение по id. Полный набор полей у MAX
    больше, но правило "не тащить в модель всё подряд" отменяет соблазн
    завести всё сразу.
    """

    mid: str  # ID сообщения (message id)
    seq: int  # порядковый номер в чате
    text: str | None = None


class Message(MaxModel):
    """Один message-объект MAX.

    MAX возвращает message-объект в разных местах: в ответе POST /messages
    (обёрнутый в {"message": ...}), внутри update_type=message_created
    и т.д. Поэтому модель вынесена отдельно, а не встроена в SentMessage.

    Сейчас забираем только body — этого достаточно для подтверждения
    отправки. Остальные поля (timestamp, sender, recipient, chat)
    добавим, когда начнём читать входящие сообщения (этап поллера).
    """

    body: MessageBody


class SentMessage(MaxModel):
    """Ответ MAX на POST /messages.

    Реальная структура ответа — обёртка над message-объектом::

        {"message": {"body": {...}, "sender": {...}, "chat": {...}, ...}}

    Поэтому у SentMessage единственное поле message: Message.
    Раньше здесь ошибочно был плоский body — модель не соответствовала API,
    но тесты этого не поймали, потому что фикстуры моков повторяли ту же
    ошибку. Проявилось на первом реальном вызове (этап 2g).
    """

    message: Message


class Update(MaxModel):
    """Одно событие из MAX API (GET /updates).

    MAX присылает много типов событий (bot_added, bot_removed, bot_started,
    message_created, message_edited, message_callback, ...). У всех есть общее
    поле update_type, а остальная структура зависит от типа.

    На 2c нам нужен минимум, чтобы helper-скрипт из 2f смог поймать
    "меня добавили в чат" и вытащить chat_id. Discriminated union по
    update_type сделаем, когда реально понадобится обрабатывать разные типы.

    Что храним сейчас:
    - update_type: тип события ("bot_added", "message_created", ...);
    - timestamp: время события (миллисекунды -> datetime как в BotInfo);
    - chat_id: ID чата, если событие связано с чатом (bot_added / message_created);
    - raw: полный dict исходного JSON, чтобы helper-скрипт мог показать
      всё содержимое события пользователю без хардкода полей.
    """

    update_type: str
    timestamp: datetime

    # chat_id опционален: не у каждого события есть чат
    # (у bot_started, например, есть user_id, но не всегда chat_id).
    chat_id: int | None = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def _parse_millis_timestamp(cls, value: object) -> object:
        """Преобразовать Unix-timestamp в миллисекундах в datetime (UTC).

        Копия валидатора из BotInfo. Дублирование сознательное:
        абстракция ради одной строки — избыточно, а формально общего
        предка ради двух моделей делать не хочется. Если появится третья
        модель с миллисекундным timestamp — вынесем в base.
        """
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        return value


class UpdatesResponse(MaxModel):
    """Ответ GET /updates.

    Args:
        updates: список пришедших событий (может быть пустым, если
            за время ожидания ничего не произошло).
        marker: курсор для следующего запроса. Передаётся в параметре
            marker следующего GET /updates, чтобы получить события,
            произошедшие после этой позиции. Может быть None, если
            сервер пока не сформировал новый маркер.
    """

    updates: list[Update]
    marker: int | None = None
