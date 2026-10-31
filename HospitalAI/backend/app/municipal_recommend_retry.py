"""Only dispatch recommendations to a withdrawal-aware receiver."""

import json
import logging
import socket
import sqlite3

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from app.db import Database
from app.hospital_bridge import DispatchResult, HospitalConfig
from app.municipal_bridge import MunicipalConfig
from app.municipal_recommend import MunicipalRecommendationDispatch

RETRY_SECONDS = 5.0
logger = logging.getLogger(__name__)


class LocationCapabilities(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True)
    location_tombstones: StrictBool


async def retry_once(database: Database, config: MunicipalConfig) -> DispatchResult:
    """Keep all pending location events until this receiver proves it can revoke."""
    dispatch = MunicipalRecommendationDispatch(database)
    pending = await anyio.to_thread.run_sync(dispatch._pending)
    if not pending:
        return DispatchResult(0, 0)
    limits = httpx2.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30)
    transport = httpx2.AsyncHTTPTransport(http2=True, retries=3, limits=limits,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
    timeout = httpx2.Timeout(connect=5, read=5, write=5, pool=5)
    try:
        async with httpx2.AsyncClient(transport=transport, timeout=timeout,
                                      follow_redirects=False, trust_env=False) as client:
            response = await client.get(f'{config.base_url.rstrip("/")}/api/municipality/intake/capabilities',
                headers={'Authorization': f'Bearer {config.token}', 'Accept': 'application/json'})
        if response.status_code != 200:
            return DispatchResult(0, pending)
        capability = LocationCapabilities.model_validate(response.json())
        if capability.location_tombstones is not True:
            return DispatchResult(0, pending)
    except (httpx2.HTTPError, ValidationError, json.JSONDecodeError, UnicodeDecodeError):
        return DispatchResult(0, pending)
    return await dispatch.dispatch(HospitalConfig(config.base_url, config.token))


async def retry_loop(database: Database, config: MunicipalConfig) -> None:
    """Run independently so a slow capability endpoint never blocks urgent alerts."""
    while True:
        try:
            await retry_once(database, config)
        except (sqlite3.Error, httpx2.HTTPError, OSError, json.JSONDecodeError):
            logger.warning('Location delivery retry failed; pending events retained')
        await anyio.sleep(RETRY_SECONDS)
