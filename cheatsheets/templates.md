# Шаблоны сообщений

Файлы: `src/redmine_max_notifier/templates/*.md.j2`
Скрипт проверки: `scripts/smoke_templates.py`

Имя файла = тип события. Никакого маппинга в коде: `new_issue` →
`new_issue.md.j2`.

## Цикл правки

```bash
# 1. правишь шаблон
# 2. смотришь результат в консоли — Redmine и MAX не нужны
uv run python scripts/smoke_templates.py --dry-run

# 3. шлёшь себе в личку
uv run python scripts/smoke_templates.py

# 4. или в группу
uv run python scripts/smoke_templates.py --chat-id -76702338811265
```

Один шаблон:

```bash
uv run python scripts/smoke_templates.py --dry-run -e new_issue
```

С упоминанием (id из `user_mapping_cli.py max-members`):

```bash
uv run python scripts/smoke_templates.py --dry-run \
    --mention-user-id 252123521 --mention-name "Leonid"
```

Доступные кейсы: `new_issue`, `status_changed`, `comment_added`,
`comment_with_files`, `file_only`, `comment_with_table`,
`due_date_approaching`.

Данные в скрипте **намеренно неудобные**: спецсимволы markdown, длинная
тема, задача без исполнителя, многострочный комментарий. Красиво
отрендерить идеальные данные умеет любой шаблон.

## Фильтры

| Фильтр | Зачем |
|---|---|
| `\| md` | Экранировать текст из Redmine. **Обязателен** для всего, что писали люди: тема, комментарий, описание, имена |
| `\| dt` | Время в таймзоне из `TIMEZONE`. Голый `strftime` напечатает UTC |
| `\| status_emoji` | 🔵 Новая, ⚙️ В работе, ✅ Решена… |
| `\| priority_emoji` | 🟢 Низкий, 🟡 Нормальный, 🔴 Высокий/Срочный/Немедленный |

## Грабли

**Забыл `| md`** — первый же человек, написавший `*срочно*` в теме,
сломает вёрстку всего сообщения: звёздочка закроет жирный раньше времени.

**Голый `strftime` по datetime** — напечатает UTC. Человек закрыл
задачу в 13:55, в чате увидит 10:55. Используй `| dt`. Исключение —
`due_date`: это календарная дата без времени, таймзона к ней
неприменима.

**Inline `{% if %}` в конце строки** — `trim_blocks` съест перевод
строки, и поля слипнутся в одну кашу. Пиши условным выражением:

```jinja
{{ event.issue.assigned_to.name | md if event.issue.assigned_to else 'не назначено' }}
```

**Новый статус в Redmine** — эмодзи не пропадёт, будет нейтральный 📌.
Хочешь свой — добавь в `_STATUS_EMOJI` в `renderer.py` (один словарь
на все шаблоны, не в шаблонах).

## Что MAX не умеет

**Таблицы.** Markdown-таблица из комментария Redmine приезжает кашей
из палок. Решено оставить как есть (15.07.2026) — если начнут
пользоваться часто, обернём в код-блок.

## Проверка после правки

```bash
uv run pytest tests/test_renderer.py
```
