"""Доменные события Redmine, отправляемые в MAX.

Экспортирует три типа событий (NewIssue / IssueUpdated /
DueDateApproaching), их общую базу EventBase, вспомогательные модели
изменений (NameChange / DueDateChange), type alias Event для
discriminated union и EventAdapter для парсинга события из dict/JSON.
"""

from __future__ import annotations

from redmine_max_notifier.events.models import (
    DueDateApproachingEvent,
    DueDateChange,
    Event,
    EventAdapter,
    EventBase,
    IssueUpdatedEvent,
    NameChange,
    NewIssueEvent,
)

__all__ = [
    "DueDateApproachingEvent",
    "DueDateChange",
    "Event",
    "EventAdapter",
    "EventBase",
    "IssueUpdatedEvent",
    "NameChange",
    "NewIssueEvent",
]
