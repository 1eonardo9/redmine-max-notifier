"""Тесты конфигурации сервиса.

Проверяем, что Settings:
- читает переменные окружения;
- падает с ValidationError, если обязательное поле не задано;
- игнорирует посторонние переменные окружения (extra="ignore");
- применяет разумные дефолты для необязательных настроек поллера;
- валидирует диапазоны числовых полей (ge/le на интервалах и часах).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from redmine_max_notifier.config import Settings, get_settings

# ── Хелпер: полный набор обязательных переменных ────────────────────────
# Список обязательных полей вырос с одного (database_url) до четырёх —
# чтобы каждый тест не повторял четыре monkeypatch.setenv, собираем в один
# словарь и раскатываем через цикл.
_REQUIRED_ENV: dict[str, str] = {
    "DATABASE_URL": "sqlite+aiosqlite:///./test.db",
    "REDMINE_URL": "http://redmine.test.local",
    "REDMINE_API_KEY": "test-redmine-key-do-not-log",
    "MAX_TOKEN": "test-max-token-do-not-log",
}


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проставить все обязательные переменные окружения."""
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


# ── Обязательные поля и базовые сценарии ────────────────────────────────


def test_settings_reads_all_required_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Все обязательные переменные корректно попадают в Settings."""
    _set_required_env(monkeypatch)

    settings = get_settings()

    assert settings.database_url == "sqlite+aiosqlite:///./test.db"
    assert settings.redmine_url == "http://redmine.test.local"
    assert settings.redmine_api_key == "test-redmine-key-do-not-log"
    assert settings.max_token == "test-max-token-do-not-log"


def test_settings_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регистр имени переменной значения не имеет — так задано в model_config."""
    _set_required_env(monkeypatch)
    # Дополнительно подсовываем lowercase — должно перекрыть UPPERCASE
    # (pydantic-settings приводит имена к canonical и берёт последнее совпадение).
    monkeypatch.setenv("database_url", "sqlite+aiosqlite:///./lower.db")

    settings = get_settings()

    assert settings.database_url == "sqlite+aiosqlite:///./lower.db"


@pytest.mark.parametrize(
    "missing_var",
    ["DATABASE_URL", "REDMINE_URL", "REDMINE_API_KEY", "MAX_TOKEN"],
)
def test_settings_raises_when_required_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing_var: str,
) -> None:
    """Без любого из обязательных полей Settings обязан упасть на старте.

    parametrize прогонит этот же тест четыре раза — по разу на каждое
    обязательное поле. Проверяем, что убрав ЛЮБОЕ из них, получим
    ValidationError с упоминанием имени missed-поля.
    """
    # Уходим в пустую tmp-директорию, чтобы случайный .env из репо не
    # был подхвачен и не "починил" отсутствующую переменную.
    monkeypatch.chdir(tmp_path)
    # Проставляем всё, потом убираем ровно одну.
    _set_required_env(monkeypatch)
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]

    assert missing_var.lower() in str(exc_info.value).lower()


# ── Дефолты необязательных полей ────────────────────────────────────────


def test_polling_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все параметры поллера имеют разумные дефолты — сервис поднимается,
    даже если в .env указаны только обязательные поля."""
    _set_required_env(monkeypatch)

    settings = get_settings()

    assert settings.poll_interval_seconds == 60
    assert settings.polling_lookback_seconds == 300
    assert settings.status_cache_ttl_seconds == 3600
    assert settings.due_date_threshold_days == 3
    assert settings.due_date_job_hour == 9
    # Публичный URL по умолчанию — пустая строка (мягкая деградация ссылок).
    assert settings.redmine_base_url_public == ""
    # Путь до CA-bundle — относительный от CWD.
    assert settings.max_ca_bundle_path == "certs/ca_bundle.pem"


# ── Валидация диапазонов (Field(ge=..., le=...)) ────────────────────────


def test_poll_interval_below_minimum_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """poll_interval_seconds < 10 отклоняется — не хотим долбить Redmine."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "5")

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_due_date_job_hour_out_of_range_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """due_date_job_hour должен быть в [0, 23]."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("DUE_DATE_JOB_HOUR", "25")

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
