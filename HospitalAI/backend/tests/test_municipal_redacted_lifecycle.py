"""The application runs privacy notices independently of ordinary intake."""

from pathlib import Path
from threading import Event

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox
from app.municipal_retry import municipal_lifespan


def test_lifespan_starts_privacy_notice_worker(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / 'calls.sqlite')
    database.initialize()
    CallStore(database).initialize()
    outbox = MunicipalOutbox(database)
    outbox.initialize()
    started = Event()

    async def observe(database, config):
        started.set()
        await anyio.sleep_forever()

    monkeypatch.setattr('app.municipal_retry.dispatch_redacted', observe, raising=False)
    app = FastAPI(lifespan=municipal_lifespan(outbox, MunicipalConfig('http://127.0.0.1:1', 'fixture')))
    with TestClient(app):
        assert started.wait(3)
