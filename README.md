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
там же построчные пояснения. Сам сервис в Docker не заворачиваем.

Прод — та же машина, где живёт Redmine: Debian 13, Redmine в Docker
(`redmine:6`, порт 80 хоста проброшен в 3000 контейнера). Реверс-прокси
в цепочке нет, поэтому `REDMINE_URL=http://localhost` попадает прямо
в контейнер.

### 1. Доставка кода

Прод-машина не видит Gitea, поэтому код едет через `scp`. Архив собираем
`git archive` — он кладёт в тарбол **только отслеживаемые git'ом файлы**,
то есть без `.env`, `.venv`, `__pycache__` и локальной `*.db`. Список
исключений руками не поддерживаем намеренно: забыть в нём `.env` — значит
увезти свои dev-секреты на прод и потом гадать, откуда сервис взял токен.

```bash
# на dev-машине
git archive --format=tar.gz -o /tmp/rmn.tgz HEAD
scp /tmp/rmn.tgz user@redmine:/tmp/

# на прод-машине
sudo mkdir -p /opt/redmine-max-notifier
sudo tar -xzf /tmp/rmn.tgz -C /opt/redmine-max-notifier
```

### 2. Пользователь и зависимости

```bash
# пользователь без шелла и домашнего каталога
sudo useradd --system --no-create-home --shell /usr/sbin/nologin redmine-notifier

# uv должен лежать в системном PATH: под sudo пользовательский
# ~/.local/bin не подхватывается
sudo apt install uv    # если пакета нет — официальный установщик:
# curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

# .venv собираем от root. Код принадлежит root, сервис его только читает:
# скомпрометированный сервис не должен уметь переписать свои исходники.
# Прав 755 ему хватает, чтобы читать и исполнять .venv.
cd /opt/redmine-max-notifier
sudo uv sync --frozen
```

### 3. Секреты — вне рабочего дерева

```bash
sudo mkdir -p /etc/redmine-max-notifier
sudo cp /opt/redmine-max-notifier/.env.example /etc/redmine-max-notifier/env
sudo vim /etc/redmine-max-notifier/env
sudo chown root:redmine-notifier /etc/redmine-max-notifier/env
sudo chmod 640 /etc/redmine-max-notifier/env
```

Пути здесь — **только абсолютные**. Юнит работает под
`ProtectSystem=strict`, писать можно единственно
в `/var/lib/redmine-max-notifier`:

```ini
DATABASE_URL=sqlite+aiosqlite:////var/lib/redmine-max-notifier/notifier.db
MAX_CA_BUNDLE_PATH=/opt/redmine-max-notifier/certs/ca_bundle.pem
REDMINE_URL=http://localhost
TIMEZONE=Europe/Moscow
```

Четыре слэша в `DATABASE_URL` — не опечатка: три отделяют схему, четвёртый
начинает абсолютный путь. С тремя получится путь относительно рабочего
каталога, и БД уедет не туда. Если `DATABASE_URL` укажет мимо
`/var/lib/redmine-max-notifier`, сервис упадёт не на старте, а на первой
записи — искать будешь долго.

### 4. Каталог состояния и миграции

Схему БД накатываем вручную — сервис миграции сам не применяет.

Каталог создаём руками **до** alembic: `StateDirectory=` в юните создал бы
его только при первом старте сервиса, а миграции идут раньше — писать
было бы некуда. Существующий каталог systemd не смущает, он лишь поправит
владельца.

```bash
sudo install -d -o redmine-notifier -g redmine-notifier -m 755 \
    /var/lib/redmine-max-notifier
```

`alembic/env.py` берёт настройки через наш `Settings`, а тот требует **все**
обязательные переменные, не только `DATABASE_URL` — подсунуть одну не выйдет,
будет `ValidationError`. Поэтому затягиваем env-файл целиком:

```bash
sudo bash -c 'set -a; . /etc/redmine-max-notifier/env; set +a; \
  cd /opt/redmine-max-notifier && \
  sudo -u redmine-notifier \
    --preserve-env=DATABASE_URL,REDMINE_URL,REDMINE_API_KEY,MAX_TOKEN,MAX_CA_BUNDLE_PATH \
    .venv/bin/alembic upgrade head'
```

Внешний `sudo` нужен, чтобы прочитать env-файл (он `640 root:redmine-notifier`),
внутренний `sudo -u` — чтобы файл БД создал сервисный юзер, а не root.
Иначе сервис не сможет писать в собственную базу.

### 5. Запуск

```bash
sudo cp /opt/redmine-max-notifier/deploy/redmine-max-notifier.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now redmine-max-notifier
journalctl -u redmine-max-notifier -f
```

Первый цикл поллинга ничего не отправляет — он ставит baseline курсора
(холодный старт). Уведомления пойдут со второго.

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
