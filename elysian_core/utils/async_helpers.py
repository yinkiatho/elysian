"""
Async utility helpers shared across connector managers.
"""
import asyncio
from typing import List, Optional


async def cancel_tasks(
    reader_task: Optional[asyncio.Task],
    worker_tasks: List[asyncio.Task],
) -> None:
    """Gracefully cancel a reader task and a list of worker tasks.

    Cancels each task and awaits it so that CancelledError is consumed
    before the caller proceeds with cleanup.
    """
    if reader_task and not reader_task.done():
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

    for task in worker_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
