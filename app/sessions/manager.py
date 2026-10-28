"""Single-camera admission with cooldown and an explicit inactivity heuristic."""
import asyncio
from collections import OrderedDict
from contextlib import suppress
import time

from app.sessions.controller import State


class SessionManager:
    def __init__(self, settings, controller, clock=time.monotonic):
        self.settings, self.controller, self.clock = settings, controller, clock
        self.seen = OrderedDict()
        self.last_activity = float('-inf')
        self.cooldown_until = 0
        self.armed = True
        self.enabled = True
        self.task = None

    def rearm(self):
        now = self.clock()
        if (now >= self.cooldown_until
                and now - self.last_activity >= self.settings.porch_clear_seconds
                and (self.task is None or self.task.done())):
            self.armed = True
            self.controller.state = State.IDLE

    def submit(self, event):
        self.rearm()
        metrics = self.controller.metrics
        metrics['events_received'] += 1
        if event.ended:
            # Detection end is not a verified departure or absence indication.
            metrics['detection_end_ignored'] += 1
            return
        self.last_activity = self.clock()
        if event.event_ref in self.seen:
            metrics['events_duplicate'] += 1
            return
        self.seen[event.event_ref] = None
        if len(self.seen) > 1024:
            self.seen.popitem(last=False)
        if not self.enabled or not self.armed or (self.task and not self.task.done()):
            metrics['events_suppressed'] += 1
            return
        self.armed = False
        self.task = asyncio.create_task(self._run())

    def request_test(self):
        self.rearm()
        if not self.enabled or not self.armed or (self.task and not self.task.done()):
            return False
        self.armed = False
        self.last_activity = self.clock()
        self.controller.metrics['manual_tests'] += 1
        self.task = asyncio.create_task(self._run())
        return True

    async def _run(self):
        try:
            await self.controller.run()
        finally:
            self.cooldown_until = self.clock() + self.settings.cooldown_seconds

    async def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.cooldown_until = self.clock() + self.settings.cooldown_seconds
