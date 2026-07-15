# STATE.md — живое состояние проекта

Правится в конце каждой сессии. То, что здесь — актуальный «прицел».
Всё, что попало в git-историю и стабильно — уходит в `CLAUDE.md`
или просто в код.

---

## Где мы сейчас

**Последний коммит `main`:** SHA-хэш добавляю после пуша 7b — сейчас
это коммит с сообщением `feat(scheduler): APScheduler в lifespan +
heartbeat-job` (подэтап 7b).

**Текущий этап:** 7. Поллер и детектор событий.
**Закрыто в этапе 7:** ✅ 7a, ✅ 7b.
**В работе:** 🔜 **7c** — `StatusResolver`.

---

## Задача подэтапа 7c

Собрать `StatusResolver` — небольшой кэш «id статуса → имя статуса»
с TTL. Нужен поллеру: событие `StatusChangedEvent` должно содержать
готовые `old_status_name / new_status_name` до того, как попадёт
в рендер (см. якорь 4.8 в CLAUDE.md — резолв делает поллер, а не
шаблонизатор).

### Что нужно сделать

1. Класс `StatusResolver` — асинхронный (метод `resolve(status_id)`
   есть await, потому что при промахе кеша идёт HTTP-вызов).
2. Хранилище кеша — `dict[int, str]` + timestamp последнего обновления.
3. TTL берётся из `Settings.status_cache_ttl_seconds` (уже добавлено
   в 7a, default 3600).
4. При промахе или истечении TTL — вызов `client.list_issue_statuses()`
   и обновление кеша целиком (не поштучно — API возвращает все статусы
   одним запросом).
5. Юнит-тесты на моках Redmine через pytest-httpx.

### Целевой API (примерно)

```python
class StatusResolver:
    def __init__(
        self,
        client: RedmineClient,
        ttl: timedelta,
    ) -> None: ...

    async def resolve(self, status_id: int) -> str | None: ...
```

Возвращает `None`, если статус не найден в Redmine (защита от
«удалили статус, но событие с ним пришло по journal»).

### Тонкости для обсуждения на входе

- **Гонка при инвалидации.** Если два корутина одновременно
  промахнулись — оба пойдут в HTTP? Или ставим `asyncio.Lock`?
  Правильный ответ — Lock (иначе будет N параллельных обновлений
  кеша). Обсудим при коде.
- **`resolve` возвращает `str | None` или бросает исключение?**
  Склоняюсь к `str | None`, потому что «не нашли статус» — не
  ошибка приложения, а бизнес-факт (поллер решает, что делать).

---

## Whitelist файлов для 7c

Читаешь только то, что тут перечислено. Ничего лишнего.

### Читать (существующий код)

- `src/redmine_max_notifier/config.py` — оттуда `status_cache_ttl_seconds`.
- `src/redmine_max_notifier/redmine/client.py` — метод
  `list_issue_statuses()` (добавлен в 7a).
- `src/redmine_max_notifier/redmine/models.py` — модель `Status`.
- `tests/fixtures/issue_statuses.json` — эталон ответа Redmine для мока.
- `tests/redmine/conftest.py` — паттерн фикстур для тестов клиента.

### Создать (новые файлы)

- `src/redmine_max_notifier/status_resolver.py` — сам класс.
- `tests/redmine/test_status_resolver.py` — юнит-тесты.

### НЕ трогаем в этом подэтапе

- `web/app.py` — интеграция в lifespan будет только на 7f, когда
  появится реальный поллер.
- `db/*` — резолвер не ходит в БД.
- `maxbot/*`, `renderer/*`, `routing/*` — вообще другая область.

---

## Активные грабли (последние 2 этапа)

Свежие наблюдения. Когда этап закроется целиком и «повторяющаяся»
грабля себя не проявит ещё пару этапов — переносим её в `CLAUDE.md`
или просто удаляем.

- **`AsyncIOScheduler.shutdown()` — асинхронный по факту.** Метод
  декорирован `@run_in_event_loop`, реальный переход в
  `STATE_STOPPED` происходит не при возврате, а в следующей итерации
  event loop'а (через `call_soon_threadsafe`). В проде — 0 проблем
  (loop гасится сразу). В тестах приходится дожимать через
  `await asyncio.sleep(0)` в цикле «до N тиков», иначе проверка
  `scheduler.running is False` даёт ложный True. См. пример в
  `tests/web/test_scheduler_lifespan.py`.

- **`apscheduler` не типизирован.** В `pyproject.toml` секция
  `[[tool.mypy.overrides]] module=["apscheduler.*"]` с
  `ignore_missing_imports=true`. Убрать, если появятся стабы.

- **mypy в pre-commit — изолированная venv.** Каждая новая рантайм-
  или тест-зависимость **обязана** быть в `additional_dependencies`
  mypy-хука в `.pre-commit-config.yaml`. Иначе `import-untyped` /
  `import-not-found`.

- **pre-commit стэшит несостейдженные правки.** Если
  `pre-commit run --all-files` зелёный, а `git commit` красный —
  почти всегда правки конфигов не застейджены. Лечится
  `git add <файл-конфига>` и повторным коммитом.

- **CC требует VPN для API.** `api.anthropic.com` отдаёт 403 с
  российского IP (Cloudflare-edge геоблокирует). Запуск CC — только
  через алиас `claude-vpn`, который выставляет HTTPS_PROXY на локальный
  порт HAPP + NO_PROXY для внутренней сети (192.168.100.0/24).
  Наш прод-код не пострадает: во всех `httpx.AsyncClient` стоит
  `trust_env=False` (грабля из этапа 2, см. якорь 4.2 в CLAUDE.md).

---

## Дорожная карта этапа 7 (шпаргалка)

- ✅ **7a.** Settings под поллер + `list_issue_statuses()` + фикстура
  `issue_statuses.json` с живого Redmine.
- ✅ **7b.** APScheduler 3.x + `scheduler.py` (фабрика + heartbeat) +
  интеграция в lifespan (start после engine, shutdown до dispose).
- 🔜 **7c.** `StatusResolver` — TTL-кэш «id → имя». **← мы здесь**
- ⏭️ **7d.** Чистый детектор `poll_recent_changes(client, state, resolver)
  → (events, new_state)` — без БД, без отправки.
- ⏭️ **7e.** Идемпотентность (`is_already_sent` / `mark_sent`) +
  диспетчер отправки (событие → рендер → routing → MAX).
- ⏭️ **7f.** Регулярный job поллера через APScheduler (собирает
  7c + 7d + 7e, читает/обновляет `PollingState`).
- ⏭️ **7g.** Ежедневный job «дедлайны»
  (`DueDateApproachingEvent`, cron в `due_date_job_hour`).
- ⏭️ **7h.** Smoke на живом Redmine + живом MAX + инструкция запуска
  сервиса (systemd-unit). Финальный коммит этапа.
