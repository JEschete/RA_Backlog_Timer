"""Background scan management and SSE fan-out.

One scan runs at a time. Progress frames are broadcast to every connected
browser; late subscribers immediately receive the most recent frame so a
refreshed tab does not sit blank until the next game finishes.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
from typing import AsyncIterator

from ..config import log
from ..credentials import Credentials
from ..models import RAAuthError, ScanProgress
from ..scanner import Scanner, ScanOptions, ScanResult


class ScanManager:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._scanner: Scanner | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._latest = ScanProgress(state="idle")
        self._last_result: ScanResult | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- state ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def latest(self) -> ScanProgress:
        return self._latest

    @property
    def last_result(self) -> ScanResult | None:
        return self._last_result

    def status(self) -> dict:
        data = asdict(self._latest)
        data["running"] = self.running
        if self._last_result is not None:
            data["result"] = asdict(self._last_result)
        return data

    # --- pub/sub -------------------------------------------------------

    def _broadcast(self, progress: ScanProgress) -> None:
        """Called from the scanner (already on the event loop thread)."""
        self._latest = progress
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(progress)
            except asyncio.QueueFull:
                pass          # a slow client may miss intermediate frames

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        queue.put_nowait(self._latest)      # prime with current state
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    # --- control -------------------------------------------------------

    def start(self, conn, creds: Credentials, options: ScanOptions) -> bool:
        if self.running:
            return False

        self._scanner = Scanner(conn, creds, options, progress_cb=self._broadcast)
        self._last_result = None
        self._latest = ScanProgress(state="running", detail="Starting...")
        self._task = asyncio.create_task(self._run(self._scanner))
        return True

    async def _run(self, scanner: Scanner) -> None:
        try:
            self._last_result = await scanner.run()
        except RAAuthError as exc:
            self._broadcast(ScanProgress(
                state="error", detail=str(exc), errors=[str(exc)]))
        except asyncio.CancelledError:
            self._broadcast(ScanProgress(state="cancelled", detail="Scan cancelled"))
            raise
        except Exception as exc:
            log.exception("Scan task failed")
            self._broadcast(ScanProgress(
                state="error", detail=f"{type(exc).__name__}: {exc}",
                errors=[str(exc)]))

    async def cancel(self) -> bool:
        if not self.running:
            return False
        if self._scanner:
            self._scanner.cancel()
        # Give the scanner a beat to stop cleanly before forcing it.
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            if self._task and not self._task.done():
                self._task.cancel()
        return True


manager = ScanManager()
