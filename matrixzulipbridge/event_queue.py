# MatrixZulipBridge - an appservice puppeting bridge for Matrix - Zulip
#
# Copyright (C) 2024 Emma Meijere <emgh@em.id.lv>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Originally licensed under the MIT (Expat) license:
# <https://github.com/hifi/heisenbridge/blob/2532905f13835762870de55ba8a404fad6d62d81/LICENSE>.
#
# [This file includes modifications made by Emma Meijere]
#
#
import asyncio
import logging


class EventQueue:
    def __init__(self, callback):
        self._callback = callback
        self._events = []
        self._loop = asyncio.get_running_loop()
        self._timer = None
        self._start = 0
        self._chain = asyncio.Queue()
        self._task = None
        self._timeout = 3600

    def start(self):
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        while True:
            try:
                task = await self._chain.get()
            except asyncio.CancelledError:
                logging.debug("EventQueue was cancelled.")
                return

            try:
                await asyncio.create_task(task)
            except asyncio.CancelledError:
                logging.debug("EventQueue task was cancelled.")
                return
            except asyncio.TimeoutError:
                logging.warning("EventQueue task timed out.")
            finally:
                self._chain.task_done()

    def _flush(self):
        events = self._events

        self._timer = None
        self._events = []

        self._chain.put_nowait(self._callback(events))

    def enqueue(self, event):
        now = self._loop.time()

        # always cancel timer when we enqueue
        if self._timer:
            self._timer.cancel()

        # stamp start time when we queue first event, always append event
        if len(self._events) == 0:
            self._start = now

        self._events.append(event)

        # if we have bumped ourself for half a second, flush now
        if now >= self._start + 0.5:
            self._flush()
        else:
            self._timer = self._loop.call_later(0.1, self._flush)
