"""One in-process gate for queue jobs and HTTP background evaluations."""

import asyncio
from collections.abc import Awaitable, Callable



class JobExecutionService:
    lock = asyncio.Lock()

    async def run(self, operation: Callable[[], Awaitable[None]]) -> None:
        async with self.lock:
            # Cancellation must not release the gate while to_thread still uses audio/temp files.
            running = asyncio.create_task(operation())
            try:
                await asyncio.shield(running)
            except asyncio.CancelledError:
                try:
                    await running
                except Exception:
                    pass
                raise
