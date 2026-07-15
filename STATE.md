# STATE.md — живое состояние проекта

Правится в конце каждой сессии. То, что здесь — актуальный «прицел».
Всё, что попало в git-историю и стабильно — уходит в `CLAUDE.md`
или просто в код.

---

## Где мы сейчас

**Последний коммит `feature/claude-code-migration`:** `e35f4fd`
`feat(poller): StatusResolver — TTL-кэш "id статуса → имя"` (подэтап 7c).

**Текущий этап:** 7. Поллер и детектор событий.
**Закрыто в этапе 7:** ✅ 7a, ✅ 7b, ✅ 7c.
**В работе:** 🔜 **7d** — чистый детектор `poll_recent_changes`.

---

## Что закрыто в 7c (итог)

`StatusResolver` в `src/redmine_max_notifier/status_resolver.py`:
TTL-кэш «id статуса → имя», обновляется целиком, свежесть меряется
`time.monotonic()`, конкурентные промахи схлопываются в один HTTP-запрос
через `asyncio.Lock` + двойную проверку внутри лока.

Принятые решения (чтобы не переобсуждать):

- **`monotonic`, не `datetime.now()`** — настенные часы прыгают (NTP,
  перевод времени) и ломают арифметику TTL.
- **Ошибка Redmine при протухшем кэше → исключение наружу**, stale-значение
  НЕ отдаём. Молча подставленное старое имя маскирует недоступность
  Redmine; решение «ловить или падать» принимает поллер (7f).
- **Промах по свежему кэшу → сразу `None`, без рефреша.** Иначе событие
  с удалённым статусом било бы по API на каждом проходе. Реально новый
  статус подтянется по истечении TTL.

---

## Задача подэтапа 7d

Чистый детектор изменений — сердце этапа 7:

```python
async def poll_recent_changes(
    client: RedmineClient,
    resolver: StatusResolver,
    since: datetime,
    lookback: timedelta,
) -> tuple[list[Event], datetime]: ...
```

Без БД, без отправки, без APScheduler. На вход — «когда смотрели
в прошлый раз», на выход — список доменных событий и новая отметка
времени. Такую функцию легко тестировать: чистый вход → чистый выход.

### Что нужно сделать

1. Запрос свежих задач: `client.list_issues(updated_on=">=...", include=["journals"], sort="updated_on:desc")`.
   Окно = `since - lookback` (см. `polling_lookback_seconds`, якорь:
   дубли режет идемпотентность на 7e, пропуски — нет, поэтому окно
   расширяем).
2. Классификация:
   - `created_on` внутри окна → `NewIssueEvent`;
   - journal с `details[].name == "status_id"` → `StatusChangedEvent`
     (`old_value`/`new_value` — **строки**, резолвим через `resolver`);
   - journal с непустыми `notes` → `CommentAddedEvent`.
   - Одна journal-запись может дать **и** статус, **и** комментарий —
     это два разных события.
3. Новая отметка времени — обсудить на входе (см. ниже).

### Тонкости для обсуждения на входе

- **Что возвращать как `new_state`?** Максимальный `updated_on` среди
  увиденных задач или «время начала опроса»? Первое — не пропустим
  события при отставании часов Redmine, второе — проще. Склоняюсь
  к первому, с фолбэком на второе при пустом ответе.
- **`DueDateApproachingEvent` здесь НЕ трогаем** — это 7g, отдельный
  ежедневный job.
- **Приватные journal-записи** (`private_notes=True`) — шлём или нет?
  Склоняюсь к «не шлём»: приватный комментарий не должен утекать
  в общий чат проекта.

---

## Whitelist файлов для 7d

Читаешь только то, что тут перечислено. Ничего лишнего.

### Читать (существующий код)

- `src/redmine_max_notifier/events/models.py` — доменные события
  и `EventAdapter`. **Главный файл подэтапа.**
- `src/redmine_max_notifier/redmine/models.py` — `Issue`, `Journal`,
  `JournalDetail` (структура `details` — ключевая для классификации).
- `src/redmine_max_notifier/redmine/client.py` — сигнатура `list_issues`
  (фильтры, пагинация).
- `src/redmine_max_notifier/status_resolver.py` — API резолвера.
- `src/redmine_max_notifier/config.py` — `polling_lookback_seconds`.
- `tests/conftest.py` — фикстуры `client` / `base_url` / `load_fixture`.
- `tests/fixtures/issue_with_journals.json` — эталон задачи с журналами.

### Создать (новые файлы)

- `src/redmine_max_notifier/poller.py` — `poll_recent_changes`.
- `tests/test_poller.py` — юнит-тесты детектора.
- Возможно, новые фикстуры в `tests/fixtures/` — по реальным ответам
  Redmine, не по догадкам (якорь 4.12).

### НЕ трогаем в этом подэтапе

- `db/*` — детектор чистый, состояние ему передают аргументом.
  `PollingState` подключим на 7f.
- `maxbot/*`, `renderer.py`, `routing.py` — отправка это 7e.
- `scheduler.py`, `web/app.py` — интеграция это 7f.

---

## Активные грабли (последние 2 этапа)

Свежие наблюдения. Когда этап закроется целиком и «повторяющаяся»
грабля себя не проявит ещё пару этапов — переносим её в `CLAUDE.md`
или просто удаляем.

- **Async-тест на мгновенном моке не проверяет конкурентность.**
  Тест «пять корутин промахнулись → один HTTP-запрос» был зелёным даже
  с выломанной двойной проверкой в `StatusResolver._refresh`. Причина:
  `httpx_mock.add_response` отвечает без реальной точки переключения,
  первая корутина проходит весь `_refresh`, ни разу не отдав управление
  event loop'у, остальные стартуют уже на заполненном кэше и до lock'а
  не доходят. Запрос один — но по другой причине. Лечится
  `httpx_mock.add_callback(...)` с `await asyncio.sleep(0.02)` внутри
  (+ `is_reusable=True`, иначе второй запрос упадёт раньше assert'а).
  **Мораль:** написал async-тест на гонку — выломай фичу и убедись,
  что он краснеет. Зелёный тест сам по себе не доказательство.
  См. `tests/redmine/test_status_resolver.py`.

- **`check.sh` не видит untracked-файлы.** pre-commit ходит только по
  файлам, известным git'у. Новый файл, не прошедший `git add`, ruff/mypy
  **не проверяют** — `check.sh` зелёный, а `git commit` тут же краснеет
  (поймали на E501 в `status_resolver.py`). Перед `check.sh` на новых
  файлах — сначала `git add`.

- **Часы в тестах не мокаются глобально.** `time.monotonic` — общий
  с event loop'ом, который меряет им свои таймеры. Заморозишь через
  monkeypatch — `asyncio.sleep` не проснётся никогда. В тестах TTL берём
  микроскопический (0.05с) и спим по-настоящему.

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
- ✅ **7c.** `StatusResolver` — TTL-кэш «id → имя» (lock + double-check,
  monotonic, 7 тестов).
- 🔜 **7d.** Чистый детектор `poll_recent_changes(client, state, resolver)
  → (events, new_state)` — без БД, без отправки. **← мы здесь**
- ⏭️ **7e.** Идемпотентность (`is_already_sent` / `mark_sent`) +
  диспетчер отправки (событие → рендер → routing → MAX).
- ⏭️ **7f.** Регулярный job поллера через APScheduler (собирает
  7c + 7d + 7e, читает/обновляет `PollingState`).
- ⏭️ **7g.** Ежедневный job «дедлайны»
  (`DueDateApproachingEvent`, cron в `due_date_job_hour`).
- ⏭️ **7h.** Smoke на живом Redmine + живом MAX + инструкция запуска
  сервиса (systemd-unit). Финальный коммит этапа.
