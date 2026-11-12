"""Short-lived exact-input cache for authenticated users, never persisted."""
import asyncio
from collections import OrderedDict
from copy import deepcopy
from time import monotonic


class ModelResponseCache:
    def __init__(self, ttl_seconds=120, max_entries=256):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.pending = {}
        self.waiters = {}
        self.loop = None

    async def get(self, key, loader, cacheable=None, *, cancel_if_unused=False):
        loop = asyncio.get_running_loop()
        if loop is not self.loop:
            self.pending = {}
            self.waiters = {}
            self.loop = loop
        cached = self.entries.get(key)
        if cached and monotonic() - cached[0] < self.ttl_seconds:
            self.entries.move_to_end(key)
            return deepcopy(cached[1]), True
        self.entries.pop(key, None)
        task = self.pending.get(key)
        hit = task is not None
        if task is None:
            if len(self.pending) >= self.max_entries:
                return await loader(), False
            async def load():
                try:
                    result = await loader()
                    if self.pending.get(key) is asyncio.current_task() and (cacheable is None or cacheable(result)):
                        self.entries[key] = (monotonic(), deepcopy(result))
                        while len(self.entries) > self.max_entries:
                            self.entries.popitem(last=False)
                    return result
                finally:
                    if self.pending.get(key) is asyncio.current_task():
                        self.pending.pop(key, None)
            task = asyncio.create_task(load())
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self.pending[key] = task
        self.waiters[task] = self.waiters.get(task, 0) + 1
        try:
            return deepcopy(await asyncio.shield(task)), hit
        finally:
            remaining = self.waiters.get(task, 1) - 1
            if remaining:
                self.waiters[task] = remaining
            else:
                self.waiters.pop(task, None)
                # A bounded consultation must not leave its sole model loader
                # occupying a concurrency slot after the response deadline.
                if cancel_if_unused and not task.done():
                    if self.pending.get(key) is task:
                        self.pending.pop(key, None)
                    task.cancel()

    async def aclose(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.waiters.clear()
        self.entries.clear()
