"""Retry durable municipality notifications while the server is running."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
import json
import logging
import sqlite3
from typing import AsyncIterator

import anyio
import httpx2
from fastapi import FastAPI

from app.municipal_bridge import MunicipalConfig, MunicipalOutbox

RETRY_SECONDS = 5.0
logger = logging.getLogger(__name__)


async def _retry(outbox: MunicipalOutbox, config: MunicipalConfig) -> None:
    while True:
        try:
            await anyio.to_thread.run_sync(outbox.recover)
            await outbox.dispatch(config)
        except (sqlite3.Error, httpx2.HTTPError, OSError, json.JSONDecodeError):
            logger.warning("Municipality retry failed; pending events retained")
        await anyio.sleep(RETRY_SECONDS)


def municipal_lifespan(outbox: MunicipalOutbox, config: MunicipalConfig | None) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if config is None:
            yield
            return
        async with anyio.create_task_group() as group:
            group.start_soon(_retry, outbox, config)
            try:
                yield
            finally:
                group.cancel_scope.cancel()

    return lifespan
