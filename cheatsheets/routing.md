# Routing: какой проект в какой чат

Скрипт: `scripts/routing_cli.py`

Без маршрута уведомления **никуда не поедут**: событие будет
детектировано, но диспетчер напишет в лог `routing не настроен` и
пропустит его. Это первое, что стоит проверить, если «сервис работает,
а сообщений нет».

## Добавить маршрут

```bash
uv run python scripts/routing_cli.py add --project-id 1 --chat-id -76702338811265
```

- `project-id` — числовой id проекта в Redmine (не строковый
  идентификатор вроде `d-telekom`).
- `chat-id` — id чата MAX. У групп он **отрицательный**, у личек
  положительный.

## Посмотреть

```bash
uv run python scripts/routing_cli.py list

uv run python scripts/routing_cli.py list-project --project-id 1
```

## Удалить

```bash
uv run python scripts/routing_cli.py remove --project-id 1 --chat-id -76702338811265
```

## Где взять chat_id

```bash
uv run python scripts/user_mapping_cli.py max-chats   # на проде: rmn mapping max-chats
```

```
CHAT_ID            ТИП        НАЗВАНИЕ
-76702338811265    chat       D-Telecom NOTIFY
```

Показывает только чаты, где состоит бот: нет в списке — сначала
добавь бота в чат. Fallback без CLI — curl с нашим CA-bundle
(системный trust store сертификат Минцифры не знает):

```bash
curl --cacert certs/ca_bundle.pem -H "Authorization: $MAX_TOKEN" \
    https://platform-api2.max.ru/chats
```

## Где взять project_id

```bash
uv run python scripts/user_mapping_cli.py redmine-users   # покажет задачи и проекты
```

Либо в самом Redmine: `Настройки проекта` → в URL/API. Числовой id
виден в `GET /projects.json`.

## Многие-ко-многим

Один проект может слать в несколько чатов, один чат — принимать из
нескольких проектов. Просто добавь несколько маршрутов.

```bash
# Проект 1 шлёт и в общий чат, и в чат дежурных
add --project-id 1 --chat-id -111
add --project-id 1 --chat-id -222
```

> **Осторожно с несколькими чатами на проект.** Отметка «отправлено»
> ставится на событие целиком, а не на пару (событие, чат). Если из
> двух чатов доставка упала в один — повтора не будет ни для одного.
> Пока договорились жить с одним чатом на проект (решено 15.07.2026).
