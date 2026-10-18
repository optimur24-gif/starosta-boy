"""The website adapter supplies its actual navigation tree to the bot."""

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class SourceError(Exception):
    """A safe boundary for upstream connection or website format errors."""


class GroupUnavailable(SourceError):
    """A saved group is no longer present in the current academic period."""


@dataclass(frozen=True)
class Choice:
    key: str
    label: str


@dataclass(frozen=True)
class GroupMatch:
    path: tuple[str, ...]
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Menu:
    title: str
    choices: tuple[Choice, ...]


@dataclass(frozen=True)
class Section:
    title: str
    entries: tuple[str, ...]
    short_title: str | None = None
    short_entries: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Schedule:
    title: str
    day: date
    sections: tuple[Section, ...]
    notices: tuple[str, ...] = ()
    source_url: str = "https://bgmu-time.ru/"
    display_notices: tuple[str, ...] | None = None


class ScheduleSource(Protocol):
    async def menu(self, path: tuple[str, ...]) -> Menu | None:
        """Read available website sections; None means a date can be selected."""
        ...

    async def schedule(self, path: tuple[str, ...], day: date) -> Schedule:
        """Read website data; implementations may use a short in-memory cache."""
        ...
