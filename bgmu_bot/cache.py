"""Bounded in-memory source responses with atomic snapshot replacement."""

import asyncio
from collections import OrderedDict
import copy
from dataclasses import dataclass
import time
import weakref

from .source import SourceError


@dataclass
class Entry:
    value: dict
    expires_at: float


class ResponseCache:
    def __init__(self, ttl=0, max_entries=128, clock=time.monotonic):
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self.entries = OrderedDict()
        self.locks = weakref.WeakValueDictionary()
        self.generation = 0
        self.on_change = None

    async def get(self, key, loader):
        if self.ttl <= 0:
            return await loader()
        async with self.locks.setdefault(key, asyncio.Lock()):
            entry = self.entries.get(key)
            if entry and entry.expires_at > self.clock():
                self.entries.move_to_end(key)
                return entry.value
            generation = self.generation
            try:
                value = await loader()
            except SourceError:
                if entry is None:
                    raise
                stale = copy.deepcopy(entry.value)
                meta = stale.get("meta")
                if not isinstance(meta, dict):
                    meta = stale["meta"] = {}
                meta["stale"] = True
                warning = "Не удалось обновить данные. Показана предыдущая копия бота."
                meta["warning"] = " ".join(value for value in (meta.get("warning", ""), warning) if value)
                return stale
            if generation == self.generation:
                self.entries[key] = Entry(value, self.clock() + self.ttl)
                self.entries.move_to_end(key)
                while len(self.entries) > self.max_entries:
                    self.entries.popitem(last=False)
                self._notify()
            return value

    def replace(self, values):
        replacement = OrderedDict((key, Entry(value, self.clock() + self.ttl)) for key, value in values.items())
        while len(replacement) > self.max_entries:
            replacement.popitem(last=False)
        self.entries = replacement
        self.generation += 1
        self._notify()

    def merge(self, values):
        # Preserve expiration times of unrelated schedules; publish in one step.
        replacement = self.entries.copy()
        for key, value in values.items():
            replacement.pop(key, None)
            replacement[key] = Entry(value, self.clock() + self.ttl)
        while len(replacement) > self.max_entries:
            replacement.popitem(last=False)
        self.entries = replacement
        self.generation += 1
        self._notify()

    def _notify(self):
        if self.on_change is not None:
            self.on_change(self.entries)
