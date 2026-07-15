# redmine-max-notifier

Сервис-интегратор: следит за изменениями в Redmine и шлёт уведомления
в мессенджер MAX.

## Как это работает

Сервис **опрашивает Redmine по REST API** раз в минуту — webhook'ов нет.
Плагин webhook'ов для Redmine 6.x развалился при установке, и от этой
идеи отказались на старте проекта.

```
Redmine REST API                       MAX Bot API
      │                                     ▲
      │ GET /issues.json?include=journals   │ POST /messages
      ▼                                     │
  ┌────────────────────────────────────────────────┐
  │  poller     детектор: что изменилось с прошлого│
  │             раза (по id, не по часам)          │
  │  dispatcher событие → чат проекта → шаблон     │
  │  БД         курсор поллера + журнал отправок   │
  └────────────────────────────────────────────────┘
```

Два фоновых задания (APScheduler):

- **раз в минуту** — свежие изменения: новые задачи, смены статуса,
  комментарии;
- **раз в сутки** — задачи с приближающимся сроком.

## Типы уведомлений

| Событие | Когда |
|---|---|
| Новая задача | появилась задача с id больше виденного |
| Смена статуса | в журнале задачи изменился `status_id` |
| Комментарий | в журнале появилась заметка (приватные не шлём) |
| Приближение срока | до `due_date` осталось ≤ N дней (по умолчанию 3) |

Закрытие задачи приходит как обычная смена статуса.

## Стек

Python 3.12+, FastAPI, httpx, Pydantic v2, SQLAlchemy 2.0 (async),
Alembic, APScheduler, Jinja2. Зависимости — uv, линтинг — Ruff,
типизация — mypy strict, тесты — pytest.

## Быстрый старт (локально)

```bash
uv sync                          # зависимости
cp .env.example .env             # и подставить реальные значения
uv run python scripts/build_ca_bundle.py   # CA-bundle для MAX, см. ниже
uv run alembic upgrade head      # схема БД
```

Прописать, в какой чат MAX слать уведомления по проекту:

```bash
uv run python scripts/routing_cli.py add --project-id 42 --chat-id -1001234567890
uv run python scripts/routing_cli.py list
```

Запуск:

```bash
uv run uvicorn --factory redmine_max_notifier.web.app:create_app --port 8000
```

Проверка живости — `GET /health`.

### Первый запуск ничего не отправит

Это не баг. Пустая таблица `polling_state` означает холодный старт:
первый цикл только запоминает, где сейчас находится Redmine (baseline),
и молчит. Иначе сервис на старте вывалил бы в чат всё, что попало
в окно опроса. Уведомления пойдут со второго цикла — то есть примерно
через минуту.

В логе это выглядит так:

```
холодный старт: baseline issue_id=1234 journal_id=5678, уведомления не отправляются
```

## Конфигурация

Все настройки — переменные окружения (или `.env` рядом с процессом).
Полный список с пояснениями — в [`.env.example`](.env.example).

Обязательные: `DATABASE_URL`, `REDMINE_URL`, `REDMINE_API_KEY`, `MAX_TOKEN`.

Что стоит знать про остальные:

- **`REDMINE_BASE_URL_PUBLIC`** — адрес Redmine для ссылок в сообщениях.
  Нужен, когда сервис ходит во внутренний адрес, а люди кликают по
  внешнему. Пусто — ссылки будут битые.
- **`TIMEZONE`** (по умолчанию `Europe/Moscow`) — в этой таймзоне
  считается час ежедневного задания и «какой сегодня день» при сравнении
  с `due_date`. Задаётся **явно**, а не берётся из ОС: серверы принято
  держать в UTC, и тогда «9 утра» молча превратилось бы в полдень.
- **`POLLING_LOOKBACK_SECONDS`** (300) — насколько расширять окно опроса
  назад. Страховка от рассинхрона часов между сервисом и Redmine.
  Дубли от этого не возникают: их режет детекция по id и журнал
  отправок. **Нужен NTP:** уплывут часы больше чем на это окно —
  начнём молча терять события.
- **`MAX_CA_BUNDLE_PATH`** — см. ниже.

### CA-bundle для MAX

Сертификат `platform-api2.max.ru` подписан УЦ Минцифры РФ, которого нет
ни в `certifi`, ни в trust store Windows. Без своего bundle любой запрос
к MAX падает на проверке TLS.

```bash
uv run python scripts/build_ca_bundle.py   # certifi + сертификаты Госуслуг
```

Bundle коммитится в репозиторий (`certs/ca_bundle.pem`). Путь считается
**от рабочего каталога процесса** — в systemd он не тот, что в терминале,
поэтому на проде указывай абсолютный. Битый путь роняет сервис на старте:
это специально, лучше так, чем узнать о проблеме на первой отправке.

## Деплой (systemd)

Юнит-файл — [`deploy/redmine-max-notifier.service`](deploy/redmine-max-notifier.service),
там же построчные пояснения. Docker не используем.

```bash
# пользователь без шелла и домашнего каталога
sudo useradd --system --no-create-home --shell /usr/sbin/nologin redmine-notifier

# код
sudo git clone <repo> /opt/redmine-max-notifier
cd /opt/redmine-max-notifier
sudo uv sync --frozen

# секреты — вне рабочего дерева
sudo mkdir -p /etc/redmine-max-notifier
sudo cp .env.example /etc/redmine-max-notifier/env
sudo vim /etc/redmine-max-notifier/env
sudo chown root:redmine-notifier /etc/redmine-max-notifier/env
sudo chmod 640 /etc/redmine-max-notifier/env

# запуск
sudo cp deploy/redmine-max-notifier.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now redmine-max-notifier
journalctl -u redmine-max-notifier -f
```

Схему БД накатывать вручную — сервис миграции сам не применяет:

```bash
sudo -u redmine-notifier /opt/redmine-max-notifier/.venv/bin/alembic upgrade head
```

### Про пути в проде

Юнит работает под `ProtectSystem=strict`, то есть писать можно только
в `/var/lib/redmine-max-notifier` (его создаёт `StateDirectory=`).
Поэтому в `/etc/redmine-max-notifier/env`:

```ini
DATABASE_URL=sqlite+aiosqlite:////var/lib/redmine-max-notifier/notifier.db
MAX_CA_BUNDLE_PATH=/opt/redmine-max-notifier/certs/ca_bundle.pem
```

Четыре слэша в `DATABASE_URL` — не опечатка: три отделяют схему, четвёртый
начинает абсолютный путь. С тремя получится путь относительно рабочего
каталога, и БД уедет не туда.

### Мониторинг

`GET /health` на `127.0.0.1:8000` — можно вешать Zabbix. Полезно также
следить, что в логе регулярно появляется `цикл поллинга:` — молчание
дольше нескольких минут означает, что фоновые задания встали.

## Шпаргалки

Памятки по эксплуатации — в [cheatsheets/](cheatsheets/):
[routing](cheatsheets/routing.md) (проект → чат),
[@упоминания](cheatsheets/mentions.md) (где взять ID, как сопоставить людей),
[шаблоны](cheatsheets/templates.md) (правка и проверка в чате),
[эксплуатация](cheatsheets/operations.md) (запуск, миграции, диагностика).

## Разработка

```bash
bash scripts/check.sh   # ruff + mypy + pytest, полный прогон
uv run pytest           # только тесты
```

Перед `check.sh` на новых файлах — `git add`: pre-commit ходит только по
файлам, известным git, и untracked-код молча не проверяется.

Проектные соглашения — [CLAUDE.md](CLAUDE.md), текущее состояние
работ — [STATE.md](STATE.md).

## Этапы реализации

- [x] Этап 0. Каркас, тулинг, pre-commit
- [x] Этап 1. Async-клиент Redmine REST API
- [x] Этап 2. Клиент MAX Bot API
- [x] Этап 3. Модели событий
- [x] Этап 4. FastAPI-каркас, `/health`, lifespan
- [x] Этап 5. Шаблоны сообщений и маршрутизация
- [x] Этап 6. БД, миграции, дедупликация
- [x] Этап 7. Поллер, детектор событий, рассылка, дедлайны
- [ ] Этап 8. Финальные тесты и деплой
- [ ] Этап 9. Расширяемость: подписки на конкретные задачи

## Лицензия

MIT — см. [LICENSE](LICENSE).
