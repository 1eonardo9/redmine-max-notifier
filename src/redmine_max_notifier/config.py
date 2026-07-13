"""Конфигурация приложения через переменные окружения и .env-файл.

Единый Settings-объект, из которого весь остальной код берёт настройки:
DATABASE_URL, токены, адреса внешних сервисов. Валидируется на старте —
если чего-то не хватает или значение битое, сервис падает сразу с внятной
ошибкой, а не через час на первом обращении к БД.
"""

from __future__ import annotations

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


def get_settings() -> Settings:
    """Фабрика Settings.

    Пока без кэширования — на Этапе 6b в lifespan FastAPI мы создадим
    ровно один экземпляр и положим его в app.state.settings, откуда его
    заберут поллер и роутеры. В тестах фабрику удобно звать заново
    с подменённым окружением через monkeypatch.
    """
    return Settings()  # type: ignore[call-arg]
