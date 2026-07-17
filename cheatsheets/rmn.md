# rmn: прод-обёртка CLI

Скрипт: `deploy/rmn` → на проде живёт в `/usr/local/bin/rmn`.

## Зачем

На dev-машине CLI запускается просто: `uv run python scripts/... `
подхватывает локальный `.env`. На проде `.env` рядом с кодом нет
(секреты в `/etc/redmine-max-notifier/env`, читает только root),
а писать в БД надо от сервисного юзера. Без обёртки каждая команда
выглядит так:

```bash
sudo bash -c 'set -a; . /etc/redmine-max-notifier/env; set +a; \
  cd /opt/redmine-max-notifier && \
  sudo -u redmine-notifier \
    --preserve-env=DATABASE_URL,REDMINE_URL,REDMINE_API_KEY,MAX_TOKEN,MAX_CA_BUNDLE_PATH \
    .venv/bin/python scripts/user_mapping_cli.py list'
```

С обёрткой:

```bash
rmn mapping list
```

`sudo` руками не нужен — команды с БД поднимут права сами (пароль
sudo спросится как обычно).

## Установка (однократно, после первого деплоя)

```bash
sudo install -m 755 /opt/redmine-max-notifier/deploy/rmn /usr/local/bin/rmn
```

После очередного redeploy повторить — вдруг обёртка обновилась.

## Команды

| Команда | Что под капотом |
|---|---|
| `rmn mapping <args>` | `scripts/user_mapping_cli.py` — @упоминания |
| `rmn routing <args>` | `scripts/routing_cli.py` — проект → чат |
| `rmn due-date` | `scripts/trigger_due_date.py` — разовый прогон дедлайнов |
| `rmn status` | `systemctl status redmine-max-notifier` |
| `rmn logs [args]` | `journalctl -u ... -f` (аргументы уходят journalctl) |
| `rmn restart` | `systemctl restart` + status |
| `rmn help` | шпаргалка |

Аргументы после `mapping` / `routing` — те же, что в памятках
[mentions.md](mentions.md) и [routing.md](routing.md): просто замени
`uv run python scripts/user_mapping_cli.py` на `rmn mapping`
(и аналогично для routing).

## Примеры

```bash
# маппинг людей для @упоминаний
rmn mapping max-members
rmn mapping redmine-users
rmn mapping add --redmine-user-id 10 --max-user-id 252123521
rmn mapping list

# роутинг «проект -> чат»
rmn mapping max-chats     # откуда взять chat_id
rmn routing list
rmn routing add --project-id 1 --chat-id -76702338811265

# дедлайны, не дожидаясь 9:00
rmn due-date

# сервис
rmn status
rmn logs --since -1h
rmn restart
```

## Ограничения

- Обёртка — **только для прода**: пути `/opt` и `/etc` зашиты.
  На dev-машине по-прежнему `uv run python scripts/...`.
- Smoke-скрипты (`smoke_max.py`, `smoke_templates.py`, ...) намеренно
  не обёрнуты: это dev-инструменты, на проде им делать нечего.
