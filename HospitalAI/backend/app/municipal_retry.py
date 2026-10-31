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
from app.municipal_capability_recovery import recover_failed_utterances
from app.municipal_redacted_delivery import dispatch_redacted
from app.recommendation_recovery import recover_confirmed_recommendations

RETRY_SECONDS = 5.0
logger = logging.getLogger(__name__)


async def _retry(outbox: MunicipalOutbox, config: MunicipalConfig) -> None:
    while True:
        try:
            await anyio.to_thread.run_sync(outbox.recover)
            await anyio.to_thread.run_sync(recover_confirmed_recommendations, outbox.database)
            await outbox.dispatch(config)
            if await recover_failed_utterances(outbox.database, config):
                await outbox.dispatch(config)
        except (sqlite3.Error, httpx2.HTTPError, OSError, json.JSONDecodeError):
            logger.warning("Municipality retry failed; pending events retained")
        await anyio.sleep(RETRY_SECONDS)


async def _retry_redacted(outbox: MunicipalOutbox, config: MunicipalConfig) -> None:
    """Keep compatibility-gated privacy notices separate from ordinary delivery."""
    while True:
        try:
            await dispatch_redacted(outbox.database, config)
        except (sqlite3.Error, httpx2.HTTPError, OSError, json.JSONDecodeError):
            logger.warning("Privacy notice retry failed; pending events retained")
        await anyio.sleep(RETRY_SECONDS)


def municipal_lifespan(outbox: MunicipalOutbox, config: MunicipalConfig | None) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if config is None:
            yield
            return
        from app.municipal_recommend_retry import retry_loop

        async with anyio.create_task_group() as group:
            group.start_soon(_retry, outbox, config)
            group.start_soon(_retry_redacted, outbox, config)
            group.start_soon(retry_loop, outbox.database, config)
            try:
                yield
            finally:
                group.cancel_scope.cancel()

    return lifespan
