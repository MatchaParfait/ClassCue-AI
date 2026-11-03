"""Keep upload workers alive until their resources can be safely released."""

from __future__ import annotations

import asyncio

from anyio import CancelScope
from starlette.concurrency import run_in_threadpool


async def run_upload_io(function, *args):
    # AnyIO shielding alone does not protect against an explicit Task.cancel().
    with CancelScope(shield=True):
        worker = asyncio.create_task(run_in_threadpool(function, *args))
        cancelled = None
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as error:
                if worker.cancelled():
                    raise
                cancelled = error
        if cancelled is not None:
            raise cancelled
        return result
