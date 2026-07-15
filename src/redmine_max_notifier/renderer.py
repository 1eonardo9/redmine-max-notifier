"""Рендерер текстовых сообщений для отправки в MAX.

Единственная задача: взять доменное событие (см. events/models.py)
и вернуть готовую строку в формате Markdown, которую MaxClient
скормит в POST /messages с format="markdown".

Шаблоны — Jinja2, лежат в пакете redmine_max_notifier/templates/,
имя файла = event_type + ".md.j2". Никакого маппинга «событие → имя
шаблона» в коде: ассоциация задаётся именем файла.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined

from redmine_max_notifier.events.models import Event

# Формат даты-времени в сообщениях: 15.07.2026 13:55.
_DT_FORMAT = "%d.%m.%Y %H:%M"

# Дефолтная таймзона рендерера. Вынесена в константу, а не в default
# аргумента: вызов в default вычисляется один раз при импорте модуля
# (Ruff B008). ZoneInfo иммутабелен, шарить один экземпляр безопасно.
_DEFAULT_TZ = ZoneInfo("Europe/Moscow")

# Символы, имеющие смысл в markdown-диалекте MAX. Обратный слеш первым:
# иначе он экранировал бы уже вставленные нами escape-последовательности.
_MD_SPECIAL = re.compile(r"([\\*_`\[\]])")


def escape_markdown(value: object) -> str:
    """Экранировать спецсимволы markdown в тексте из Redmine.

    Зачем. Тема задачи и текст комментария приходят от людей, а шаблон
    заворачивает их в разметку: *{{ subject }}*. Тема вида
    "Авария: *обрыв* ОК" превращает это в кашу — звёздочки внутри
    закрывают жирный раньше времени, и дальше едет вся вёрстка
    сообщения. Достаточно одного человека, написавшего *срочно*
    в комментарии.

    Применяется в шаблонах фильтром `| md` — ко всему, что пришло
    из Redmine (subject, notes, description, имена). К нашему
    собственному тексту не нужен: мы его пишем сами и знаем, что там.
    """
    return _MD_SPECIAL.sub(r"\\\1", str(value))


def format_datetime(value: datetime, tz: ZoneInfo) -> str:
    """Показать время события в таймзоне людей, а не в UTC.

    Redmine отдаёт время в UTC ("created_on": "2026-07-15T06:36:32Z"),
    и strftime по такому datetime напечатает UTC как есть. В сообщении
    это выглядит как "10:55" вместо "13:55" — и человек, который минуту
    назад закрыл задачу, видит уведомление на три часа раньше.

    naive-время трактуем как UTC: все datetime в проекте приходят из
    Redmine (с Z) либо создаются как datetime.now(UTC), так что это
    не догадка, а наш инвариант. Без этой ветки astimezone() молча
    подставил бы таймзону ОС.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz).strftime(_DT_FORMAT)


class MessageRenderer:
    """Рендерит событие и собираем сообщение для MAX
    Один инстанс на приложение — создаётся в lifespan FastAPI
    (или в поллере на Этапе 7). Не хранит per-request состояния,
    потокобезопасен в пределах одного event loop.
    """

    def __init__(
        self,
        redmine_base_url: str,
        tz: ZoneInfo = _DEFAULT_TZ,
    ) -> None:
        """Собирает Environment один раз на всё время жизни рендерера.

        redmine_base_url — база URL инстанса Redmine (например,
        "http://redmine.d-telekom.home"). Подмешивается в глобалы
        Jinja, чтобы шаблоны могли строить ссылку на задачу
        без передачи URL каждый раз в render(). Trailing slash
        аккуратно срезается — шаблон пишет "{{ redmine_base_url }}/issues/{{ id }}".

        tz — таймзона, в которой людям показывается время события
        (Settings.timezone). Дефолт — чтобы не тащить конфиг в тесты
        и разовые скрипты; боевой код передаёт значение явно.
        """
        self._env = Environment(
            loader=PackageLoader("redmine_max_notifier", "templates"),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
            keep_trailing_newline=False,
        )
        self._env.globals["redmine_base_url"] = redmine_base_url.rstrip("/")
        # Фильтр `| md` — экранирование текста из Redmine, см. escape_markdown.
        self._env.filters["md"] = escape_markdown
        # Фильтр `| dt` — время в таймзоне людей. В шаблонах не должно
        # остаться ни одного голого strftime по datetime: он напечатает
        # UTC, приехавший из Redmine.
        self._env.filters["dt"] = lambda value: format_datetime(value, tz)

    def render(self, event: Event) -> str:
        """Собирает markdown-сообщение по типу события.
        Имя шаблона выводится напрямую из event.event_type:
        "new_issue" → "new_issue.md.j2". Если файла нет —
        Jinja поднимет TemplateNotFound, что означает
        «поллер прислал событие, для которого забыли шаблон».
        Ловить это исключение здесь не будем: это программерская
        ошибка, а не runtime-ситуация — пусть падает громко.
        В контекст кладём само событие под именем "event". Шаблон
        обращается к полям как event.issue.subject, event.notes и т.д.
        StrictUndefined гарантирует, что опечатка в имени поля
        превратится в UndefinedError при рендере, а не в пустую строку.
        """
        template_name = f"{event.event_type}.md.j2"
        template = self._env.get_template(template_name)
        return template.render(event=event).strip()
