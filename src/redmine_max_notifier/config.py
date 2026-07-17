"""Конфигурация приложения через переменные окружения и .env-файл.

Единый Settings-объект, из которого весь остальной код берёт настройки:
DATABASE_URL, токены, адреса внешних сервисов, параметры поллера.
Валидируется на старте — если чего-то не хватает или значение битое, сервис
падает сразу с внятной ошибкой, а не через час на первом обращении к БД
или Redmine.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    r"""Настройки сервиса, читаемые из окружения и\или .env-файла.
    Каждое поле класса = одна переменная окружения. Регистр не важен
    (см. case_sensitive ниже), но по конвенции в .env пишем UPPER_CASE:
    `DATABASE_URL=...`.

    Поле без дефолта — обязательное. Если такая переменная не задана,
    Settings() кинет ValidationError на старте.
    """

    # SettingsConfigDict — конфигурация самого класса Settings.
    # Это не обычное поле модели, а специальный атрибут-настройка,
    # который pydantic-settings ищет по имени `model_config`.
    model_config = SettingsConfigDict(
        env_file=".env",  # откуда читать .env (относительно рабочего каталога)
        env_file_encoding="utf-8",  # для WIN дефолт = cp1251 - ЯВНО UTF-8
        case_sensitive=False,  # DATABASE_URL и database_url одно и тоже
        extra="ignore",  # лишние переменные в .env игнорируются
    )

    # ── База данных ──────────────────────────────────────────────────────
    # SQLAlchemy-совместимый URL. Примеры:
    #   dev:  sqlite+aiosqlite:///./redmine_max_notifier.db
    #   prod: postgresql+asyncpg://user:pass@host:5432/dbname
    #
    # Тип — обычная str, не HttpUrl/PostgresDsn: SQLAlchemy сам провалидирует
    # формат при создании engine, а sqlite-URL не подойдёт ни под один
    # готовый Pydantic-DSN.
    database_url: str

    # ── Redmine ──────────────────────────────────────────────────────────
    # Базовый URL Redmine-инстанса, к которому ходит наш async-клиент.
    # Пример: "http://redmine.d-telekom.home". Без хвостового слэша.
    redmine_url: str

    # API-ключ сервисного пользователя (My account → API access key).
    # Кладётся в заголовок X-Redmine-API-Key каждого запроса.
    redmine_api_key: str

    # Публичный URL Redmine для ссылок в сообщениях MAX.
    # Часто совпадает с redmine_url, но бывают случаи, когда сервис
    # ходит во внутренний адрес (http://redmine.d-telekom.home), а
    # пользователи кликают по внешнему (https://redmine.example.com).
    # По дефолту — пусто; MessageRenderer падёт с осмысленной ошибкой,
    # если ссылка нужна, а её нет. Обязательного значения нет намеренно:
    # для чисто dev-запусков сервис можно поднять без ссылок в шаблонах.
    redmine_base_url_public: str = ""

    # Id кастомного поля «Соисполнители» (формат user, multiple).
    # Id — свойство конкретной инсталляции Redmine: на другом инстансе
    # то же поле получит другой номер, поэтому конфиг, а не константа
    # в коде. Если поля с таким id на задаче нет — соисполнителей
    # просто нет, это не ошибка.
    coexecutors_field_id: int = Field(default=3, ge=1)

    # ── MAX ──────────────────────────────────────────────────────────────
    # Токен MAX-бота. Кладётся в заголовок Authorization без префикса
    # Bearer (см. накопленные решения по MAX-клиенту).
    max_token: str

    # Путь до склеенного CA-bundle (certifi + сертификаты Госуслуг).
    # Собирается скриптом scripts/build_ca_bundle.py, коммитится в репу.
    # Относительный путь считается от CWD процесса.
    max_ca_bundle_path: str = "certs/ca_bundle.pem"

    # ── Поллер: свежие изменения ─────────────────────────────────────────
    # Как часто крутить задание "свежие изменения" (секунды).
    # 60с — разумный дефолт: события долетают за минуту, нагрузка на
    # Redmine — 60 запросов в час, около нуля.
    poll_interval_seconds: int = Field(default=60, ge=10)

    # На сколько секунд назад расширяем окно updated_on относительно
    # last_check_at. Нужно, чтобы события, случившиеся в момент прошлого
    # опроса, не пропустились из-за рассинхрона часов, задержек репликации
    # БД Redmine и т.п. Дубли режутся идемпотентностью sent_notifications.
    polling_lookback_seconds: int = Field(default=300, ge=0)

    # TTL кэша словаря "id статуса → name". Статусы в Redmine меняются
    # раз в никогда, но всё равно перечитываем раз в час на всякий
    # случай (админ добавил новый статус — не хочется рестартовать сервис).
    status_cache_ttl_seconds: int = Field(default=3600, ge=60)

    # ── Поллер: дедлайны ─────────────────────────────────────────────────
    # За сколько дней до due_date шлём DueDateApproachingEvent.
    # 3 дня — стандартный "предупреждающий" порог.
    due_date_threshold_days: int = Field(default=3, ge=0)

    # В какой час гонять ежедневное задание "дедлайны". 9 — начало
    # рабочего дня, самое время получить напоминание. Час считается
    # в таймзоне из поля timezone ниже, а не в таймзоне ОС.
    due_date_job_hour: int = Field(default=9, ge=0, le=23)

    # ── Время ────────────────────────────────────────────────────────────
    # Бизнес-таймзона сервиса: в ней считается час запуска задания
    # "дедлайны" и то, какой сегодня день при сравнении с due_date.
    #
    # Задаётся ЯВНО, а не берётся из ОС, намеренно. Прод-серверы принято
    # держать в UTC (логи, БД, отладка), и тогда "9 утра" по местной
    # таймозне ОС превратилось бы в полдень — молча, без единой ошибки
    # в логах. Плюс сервер могут пересоздать из другого образа, и TZ
    # уедет вместе с ним.
    #
    # Имя — из базы IANA ("Europe/Moscow", "Asia/Yekaterinburg").
    # Валидируется на старте: битое имя уронит сервис сразу, а не
    # через сутки на первом запуске задания.
    timezone: str = "Europe/Moscow"

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Проверить, что таймзона существует в базе IANA.

        ZoneInfo кидает ZoneInfoNotFoundError на опечатку вроде
        "Europe/Moskow". Ловим на старте — иначе узнали бы об этом
        только когда не пришло напоминание о дедлайне.
        """
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"неизвестная таймзона {value!r}; ожидается имя из базы IANA, "
                f"например Europe/Moscow"
            ) from exc
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        """Таймзона как объект — для CronTrigger и вычисления 'сегодня'."""
        return ZoneInfo(self.timezone)


def get_settings() -> Settings:
    """Фабрика Settings.

    Пока без кэширования — в lifespan FastAPI мы создаём ровно один
    экземпляр и кладём его в app.state.settings, откуда его забирают
    и поллер, и роутеры. В тестах фабрику удобно звать заново с
    подменённым окружением через monkeypatch.
    """
    return Settings()  # type: ignore[call-arg]
