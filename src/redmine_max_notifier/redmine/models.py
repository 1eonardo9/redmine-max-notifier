"""Pydantic-модель для валидации ресурсов Redmine REST API,
покрывает основные сущности, нужные нотификатору:
Issue, Journal, Project, User, Status, Tracker, CustomField.
все модели наследуются от RedmineModel - единая конфигурация Pydantic.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class RedmineModel(BaseModel):
    """Базовый класс для всех моделей ресурсов Redmine.

    Конфигурация:
    - extra="ignore" — игнорируем неизвестные поля в JSON-ответах.
      Redmine может добавлять новые поля при апгрейдах; клиент не должен
      ломаться на ровном месте.
    - populate_by_name=True — позволяет создавать модель и по имени поля,
      и по алиасу (пригодится позже, если будут расхождения с именами в JSON).
    - str_strip_whitespace=True — обрезаем пробелы в строках при парсинге.
    """

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class NamedRef(RedmineModel):
    """Ссылка на ресурс в формате {id, name}.

    Универсальный тип для tracker, status, priority, project, author,
    assigned_to, category, fixed_version и т.д.

    name опционален: для parent (родительская задача) Redmine возвращает
    только {id} без name.
    """

    id: int
    name: str | None = None


class User(RedmineModel):
    """Пользователь Redmine.
    Полная форма (как от /users/current.json или /users/{id}.json) содержит
    login, mail и т.д. В составе других ресурсов (issue.author) приходит
    урезанная форма — поэтому почти всё опционально.
    """

    id: int
    login: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    mail: str | None = None
    admin: bool | None = None
    created_on: datetime | None = None
    last_login_on: datetime | None = None
    api_key: str | None = None  # возвращается только в /users/current.json


class Project(RedmineModel):
    """Модель проекта"""

    id: int
    name: str
    description: str | None = None
    status: str | None = None  # 1=active, 5=closed, 9=archived
    is_public: bool | None = None
    parent: NamedRef | None = None
    created_on: datetime | None = None
    updated_on: datetime | None = None


class Status(RedmineModel):
    """Модель статуса задачи (issue status)"""

    id: int
    name: str
    is_closed: bool | None = None


class Tracker(RedmineModel):
    """Трекер задачи (Ошибка, Фича, Поддержка и т.д.)"""

    id: int
    name: str


class CustomField(RedmineModel):
    """Модель кастомного поля задачи (issue custom field)
    value может быть:
    - строка
    - список строк (multi-value field)
    - None пустое пол"""

    id: int
    name: str
    value: str | list[str] | None = None


class JournalDetail(RedmineModel):
    """Одно изменение в записи журнала.

    property:
    - "attr" — изменение стандартного атрибута (status_id, assigned_to_id и т.д.)
    - "cf" — изменение custom field
    - "relation" — изменение связи между задачами
    - "attachment" — добавление/удаление вложения
    """

    property: str
    name: str
    old_value: str | None = None
    new_value: str | None = None


class Journal(RedmineModel):
    """Запись журнала задачи: комментарий и/или набор изменений.

    Приходит в составе Issue только при ?include=journals.
    notes — текст комментария (может отсутствовать, если запись содержит
    только изменения атрибутов).
    details — список изменений атрибутов в этой записи.
    """

    id: int
    user: NamedRef
    notes: str | None = None
    created_on: datetime
    private_notes: bool = False
    details: list[JournalDetail] = Field(default_factory=list)


class Issue(RedmineModel):
    """Задача Redmine (issue) — основная модель, с которой работает нотификатор."""

    id: int
    project: NamedRef
    tracker: NamedRef
    status: NamedRef
    priority: NamedRef
    author: NamedRef
    assigned_to: NamedRef | None = None
    category: NamedRef | None = None
    fixed_version: NamedRef | None = None
    parent: NamedRef | None = None  # обычно только {id}, name опционален

    subject: str
    description: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    done_ratio: int = 0
    is_private: bool = False
    estimated_hours: float | None = None
    spent_hours: float | None = None

    custom_fields: list[CustomField] = Field(default_factory=list)
    journals: list[Journal] = Field(default_factory=list)

    created_on: datetime
    updated_on: datetime
    closed_on: datetime | None = None
