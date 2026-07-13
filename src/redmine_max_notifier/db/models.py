"""ORM-модели таблиц проекта.

Все три модели живут в одном файле — их немного, они логически
связаны (все обслуживают поллер), дробить по файлам не за чем.

Синтаксис — SQLAlchemy 2.0: Mapped[X] + mapped_column(). Тип SQL-колонки
и nullability выводятся из аннотации автоматически.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from redmine_max_notifier.db.base import Base


class PollingState(Base):
    """Состояние поллера. Одна строка на всё приложение (id=1).

    Обновляется после каждого успешного цикла поллинга. Читается
    один раз при старте, чтобы понять, откуда продолжать.

    На первом запуске сервиса строки нет — поллер сам создаст её
    с id=1 и всеми None-полями, а после первого цикла проставит
    актуальные значения.
    """

    __tablename__ = "polling_state"

    # Primary key. Всегда 1 — таблица логически синглтон.
    # Без autoincrement: id мы задаём явно при первой вставке.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)

    # id последней увиденной задачи. None до первого цикла поллинга.
    last_seen_issue_id: Mapped[int | None] = mapped_column(default=None)

    # id последней увиденной записи журнала (для комментариев/статусов).
    # None до первого цикла.
    last_seen_journal_id: Mapped[int | None] = mapped_column(default=None)

    # Когда в последний раз крутили цикл поллинга. Полезно и для
    # мониторинга ("сервис молчит уже 2 часа"), и для расчёта окна
    # updated_on=">=<last_check_at - запас>" в самом поллере.
    last_check_at: Mapped[datetime | None] = mapped_column(default=None)


class SentNotification(Base):
    """Журнал успешно отправленных уведомлений.

    Единственная цель — дедупликация. Перед отправкой поллер проверяет,
    была ли уже запись с таким (event_type, issue_id, journal_id). Если
    была — пропускает. Если сервис упал после отправки, но до commit'а
    poll_state, повторный старт не породит дубль уведомления.

    Заодно — история для отладки: "почему пользователь получил три
    сообщения за минуту" смотрится глазами по этой таблице.
    """

    __tablename__ = "sent_notifications"

    # Уникальный ключ дедупликации.
    # journal_id может быть NULL (для new_issue и due_date_approaching
    # журнала ещё/уже нет). ВАЖНО: в SQLite NULL != NULL внутри UNIQUE —
    # два инсёрта с одинаковыми (event_type, issue_id, NULL) НЕ конфликтуют.
    # То же в PostgreSQL по стандарту. Для new_issue это не страшно:
    # (event_type='new_issue', issue_id) сам по себе уникален в жизни задачи —
    # но если параноить, поллер должен доп. проверять "не слали ли уже
    # new_issue про этот issue_id" через SELECT перед вставкой. Разберёмся
    # в 7-м этапе.
    __table_args__ = (
        UniqueConstraint(
            "event_type",
            "issue_id",
            "journal_id",
            name="uq_sent_notifications_event",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Тип события: "new_issue" | "status_changed" | "comment_added" |
    # "due_date_approaching". Строка, а не Enum на уровне БД:
    # добавить новый тип в PostgreSQL enum — миграция с DDL-плясками,
    # в строке — одна строка кода в поллере. Валидацию значения
    # обеспечивает Pydantic-модель события на входе в отправку.
    event_type: Mapped[str] = mapped_column()

    # id задачи из Redmine, по которой было уведомление.
    issue_id: Mapped[int] = mapped_column()

    # id записи журнала (для status_changed / comment_added).
    # None для new_issue и due_date_approaching.
    journal_id: Mapped[int | None] = mapped_column(default=None)

    # Момент отправки. timezone=True — колонка TIMESTAMP WITH TIME ZONE
    # в PostgreSQL, в SQLite фактически хранится как ISO-строка с tz.
    # server_default=func.now() — БД сама подставит текущее время
    # на уровне SQL при INSERT, без указания в Python-коде. Это
    # надёжнее, чем default=datetime.utcnow: время берётся с сервера
    # БД, а не с клиента (важно, если клиентов будет несколько).
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class Routing(Base):
    """Маршрутизация уведомлений: проект Redmine → чат MAX.

    Один проект может слать в несколько чатов (например, "разработка"
    и "менеджеры проекта"), один чат может получать из нескольких
    проектов (сводный чат для тимлида). Поэтому это отношение
    многие-ко-многим, а не одно из полей PollingState.

    Уникальность на уровне пары (project_id, chat_id) — защищает
    от случайной вставки дубля через будущую админку.

    Что заполняется руками (или на 5-м этапе через seed-скрипт),
    что читается поллером при выборе адресатов для каждого события.
    """

    __tablename__ = "routing"

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "chat_id",
            name="uq_routing_project_chat",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # id проекта в Redmine (числовой, из GET /projects.json).
    project_id: Mapped[int] = mapped_column()

    # chat_id в MAX. У MAX это int, для групп он отрицательный
    # (наблюдение из smoke-скрипта в этапе 2g).
    chat_id: Mapped[int] = mapped_column()
