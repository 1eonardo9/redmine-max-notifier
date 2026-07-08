"""Доменные события Redmine, отправляемые в MAX.

Экспортирует четыре типа событий (NewIssue / StatusChanged /
CommentAdded / DueDateApproaching), их общую базу EventBase,
type alias Event для discriminated union и EventAdapter для
парсинга события из dict/JSON.
"""

from __future__ import annotations

from redmine_max_notifier.events.models import (
    CommentAddedEvent,
    DueDateApproachingEvent,
    Event,
    EventAdapter,
    EventBase,
    NewIssueEvent,
    StatusChangedEvent,
)

__all__ = [
    "CommentAddedEvent",
    "DueDateApproachingEvent",
    "Event",
    "EventAdapter",
    "EventBase",
    "NewIssueEvent",
    "StatusChangedEvent",
]
