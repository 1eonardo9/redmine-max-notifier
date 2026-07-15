# Эксплуатация: запуск, миграции, диагностика

## Запуск

```bash
# локально
uv run uvicorn --factory redmine_max_notifier.web.app:create_app --port 8000

# проверка живости
curl http://127.0.0.1:8000/health
```

Ускорить цикл для проверки (минимум 10 секунд):

```bash
POLL_INTERVAL_SECONDS=15 uv run uvicorn --factory \
    redmine_max_notifier.web.app:create_app --port 8000
```

На проде — systemd:

```bash
sudo systemctl status redmine-max-notifier
sudo systemctl restart redmine-max-notifier
journalctl -u redmine-max-notifier -f
```

## Миграции

```bash
uv run alembic upgrade head          # накатить
uv run alembic current               # где мы сейчас
uv run alembic history               # список
uv run alembic downgrade -1          # откатить одну
```

Сервис миграции **сам не применяет** — накатывать руками после
деплоя.

## Что в логах при нормальной работе

```
цикл поллинга: задач в окне 1, событий 1        ← нашёл и отправил
цикл поллинга: задач в окне 0, событий 0        ← тишина, всё ок
холодный старт: baseline issue_id=…             ← первый запуск, молчит
проверка дедлайнов: задач 3, напоминаний 1
```

Молчание дольше пары минут = фоновые задания встали.

## Диагностика: сообщений нет

По порядку:

**1. Маршрут прописан?**

```bash
uv run python scripts/routing_cli.py list
```

Нет — событие детектируется, но идёт в лог `routing не настроен`.

**2. Это не холодный старт?**

```bash
uv run python -c "import sqlite3; print(sqlite3.connect('redmine_max_notifier.db').execute('select * from polling_state').fetchall() or 'ПУСТО -> холодный старт')"
```

Пусто — первый цикл только поставит baseline и промолчит. Это норма.

**3. Событие не отправлено ранее?**

```bash
uv run python -c "import sqlite3; [print(r) for r in sqlite3.connect('redmine_max_notifier.db').execute('select event_type, issue_id, journal_id, sent_at from sent_notifications order by id desc limit 10')]"
```

Есть запись — дедуп сработал правильно, повтора не будет.

**4. Сервисный юзер видит задачи?**

```bash
uv run python scripts/user_mapping_cli.py redmine-users
```

Пусто — у роли нет права **Просмотр задач**. Членство в проекте само
по себе доступа не даёт.

## Состояние поллера

> Консольного `sqlite3` в Windows нет, поэтому команды ниже — через
> Python. На Linux можно и `sqlite3 <файл> "<запрос>"`.

```bash
uv run python -c "import sqlite3; print(sqlite3.connect('redmine_max_notifier.db').execute('select * from polling_state').fetchall())"
```

`(1, 34, 63, '2026-07-15 12:36:52')` = дошли до задачи 34, журнала 63.

**Сбросить в холодный старт** (сервис забудет, где был, и промолчит
один цикл):

```bash
uv run python -c "import sqlite3; c=sqlite3.connect('redmine_max_notifier.db'); c.execute('delete from polling_state'); c.commit()"
```

**Переотправить событие** (для проверки):

```bash
uv run python -c "import sqlite3; c=sqlite3.connect('redmine_max_notifier.db'); c.execute('delete from sent_notifications where issue_id=34'); c.commit()"
```

Только на dev: в проде это реальный спам в чат.

## Проверки перед коммитом

```bash
bash scripts/check.sh     # ruff + mypy + pytest
uv run pytest             # только тесты
```

**Новые файлы сначала `git add`** — pre-commit ходит только по
файлам, известным git, и untracked-код молча не проверяется.

## Прод: частые грабли

**Пути**. `MAX_CA_BUNDLE_PATH` и `DATABASE_URL` — абсолютные. У systemd
другой рабочий каталог, относительные пути ведут не туда. В
`DATABASE_URL` **четыре** слэша:

```ini
DATABASE_URL=sqlite+aiosqlite:////var/lib/redmine-max-notifier/notifier.db
```

**Права на запись**. Юнит с `ProtectSystem=strict` разрешает писать
только в `/var/lib/redmine-max-notifier`. БД должна лежать там.

**Время**. Сервер держим в UTC, бизнес-таймзона задаётся явно:
`TIMEZONE=Europe/Moscow`. От таймзоны ОС ничего не зависит.

**NTP обязателен**. `POLLING_LOOKBACK_SECONDS=300` — запас на рассинхрон
часов с Redmine. Уплывут больше — начнём **молча терять события**.
