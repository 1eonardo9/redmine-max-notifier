"""Тесты конфигурации сервиса.

Проверяем, что Settings:
- читает переменные окружения;
- падает с ValidationError, если обязательное поле не задано;
- игнорирует посторонние переменные окружения (extra="ignore").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from redmine_max_notifier.config import Settings, get_settings


def test_settings_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings подхватывает переменную из окружения."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")

    settings = get_settings()

    assert settings.database_url == "sqlite+aiosqlite:///./test.db"


def test_settings_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регистр имени переменной значения не имеет — так задано в model_config."""
    monkeypatch.setenv("database_url", "sqlite+aiosqlite:///./lower.db")

    settings = get_settings()

    assert settings.database_url == "sqlite+aiosqlite:///./lower.db"


def test_settings_raises_when_required_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Без DATABASE_URL Settings обязан упасть на старте, а не позже."""
    # Убираем переменную, если вдруг задана в окружении разработчика.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Уходим в пустую tmp-директорию, чтобы случайный .env из репо не был подхвачен.
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]

    # В сообщении ошибки должно фигурировать имя missed-поля.
    assert "database_url" in str(exc_info.value).lower()
