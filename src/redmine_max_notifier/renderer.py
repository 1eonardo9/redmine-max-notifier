"""Рендерер текстовых сообщений для отправки в MAX.

Единственная задача: взять доменное событие (см. events/models.py)
и вернуть готовую строку в формате Markdown, которую MaxClient
скормит в POST /messages с format="markdown".

Шаблоны — Jinja2, лежат в пакете redmine_max_notifier/templates/,
имя файла = event_type + ".md.j2". Никакого маппинга «событие → имя
шаблона» в коде: ассоциация задаётся именем файла.
"""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, StrictUndefined

from redmine_max_notifier.events.models import Event


class MessageRenderer:
    """Рендерит событие и собираем сообщение для MAX
    Один инстанс на приложение — создаётся в lifespan FastAPI
    (или в поллере на Этапе 7). Не хранит per-request состояния,
    потокобезопасен в пределах одного event loop.
    """

    def __init__(self, redmine_base_url: str) -> None:
        """Собирает Environment один раз на всё время жизни рендерера.
        redmine_base_url — база URL инстанса Redmine (например,
        "http://redmine.d-telekom.home"). Подмешивается в глобалы
        Jinja, чтобы шаблоны могли строить ссылку на задачу
        без передачи URL каждый раз в render(). Trailing slash
        аккуратно срезается — шаблон пишет "{{ redmine_base_url }}/issues/{{ id }}".
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
