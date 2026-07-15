"""ORM-модели таблиц проекта.

Все три модели живут в одном файле — их немного, они логически
связаны (все обслуживают поллер), дробить по файлам не за чем.

Синтаксис — SQLAlchemy 2.0: Mapped[X] + mapped_column(). Тип SQL-колонки
и nullability выводятся из аннотации автоматически.
"""

from __future__ import annotations

from datetime import date, datetime

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

    # Срок, о котором напоминали. Заполняется ТОЛЬКО для
    # due_date_approaching, у остальных типов событий None.
    #
    # Зачем отдельная колонка. Ключ (event_type, issue_id, journal_id)
    # для дедлайнов вырождается в (due_date_approaching, issue_id, NULL) —
    # даты в нём нет, поэтому напоминание ушло бы РОВНО ОДИН РАЗ за жизнь
    # задачи. Сдвинули срок с 15.07 на 30.07 — про новый срок сервис бы
    # промолчал, причём чем важнее задача (её и двигают), тем вероятнее
    # про неё замолчать.
    #
    # С этой колонкой дедуп работает на пару (задача, срок): одно
    # напоминание на каждое значение due_date. В UniqueConstraint её НЕ
    # добавляем — там она бесполезна ровно по той же причине, по которой
    # бесполезен journal_id: NULL != NULL (якорь 4.10). Дедуп держится
    # на явном SELECT в sent_notifications.is_already_sent.
    notified_due_date: Mapped[date | None] = mapped_column(default=None)

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


class UserMapping(Base):
    """Сопоставление «пользователь Redmine → пользователь MAX».

    Нужно для @упоминаний: в событии лежит assigned_to.id из Redmine,
    а чтобы пингнуть человека в чате, требуется его user_id в MAX.
    Связи между системами не существует, сопоставлять по имени нельзя
    («Максим Мерзляков» в Redmine против «Максим» в MAX, плюс
    однофамильцы), поэтому таблица заполняется руками — людей немного.

    Отношение многие-ко-многим, как у Routing, и это не абстракция впрок:
    - один Redmine-юзер → несколько MAX-юзеров (задача на Петю, пингуем
      Петю и его тимлида);
    - несколько Redmine-юзеров → один MAX-юзер (Петя в отпуске, все его
      задачи пингуют Васю);
    - произвольные пары (у человека два аккаунта, учётка-робот и т.п.).

    Уникальность на паре (redmine_user_id, max_user_id) — защита от
    дубля, иначе человек получил бы два упоминания в одном сообщении.
    """

    __tablename__ = "user_mapping"

    __table_args__ = (
        UniqueConstraint(
            "redmine_user_id",
            "max_user_id",
            name="uq_user_mapping_redmine_max",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # id пользователя в Redmine (issue.assigned_to.id, issue.author.id).
    redmine_user_id: Mapped[int] = mapped_column()

    # user_id в MAX (GET /chats/{chat_id}/members). Положительный.
    max_user_id: Mapped[int] = mapped_column()

    # Имя, которое покажется в упоминании: [max_name](max://user/id).
    #
    # Снимок имени из MAX на момент добавления, а не живой запрос:
    # упоминание рендерится на каждое уведомление, и ходить за именем
    # в API каждый раз — лишний round-trip ради строки, которая меняется
    # раз в никогда. Ссылка всё равно работает по user_id: даже если
    # человек сменил имя, пинг дойдёт, просто подпись будет старой.
    #
    # Имя именно MAX-пользователя, а не Redmine: при сопоставлении
    # «Петя → Вася» в чате должен быть виден и упомянут Вася.
    max_name: Mapped[str] = mapped_column()
