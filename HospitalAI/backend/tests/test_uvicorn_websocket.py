"""Exercise the real server's WebSocket upgrade instead of ASGI TestClient."""

import socket
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from app.azure_stt import speech_router
from app.call_capabilities import CallCapabilities
from app.config import Settings
from app.db import Database


def test_speech_websocket_upgrades_over_real_uvicorn_socket(tmp_path: Path) -> None:
    path = tmp_path / "server.sqlite"
    settings = Settings(environment="development", database_path=str(path))
    database = Database(path)
    app = FastAPI()
    app.include_router(speech_router(settings, CallCapabilities(database, "", "development")))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
            connection.settimeout(10)
            connection.sendall((
                "GET /calls/test/speech HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Origin: http://127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
            ).encode("ascii"))
            response = connection.recv(1024)
            assert response.startswith(b"HTTP/1.1 101 ")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
    assert not thread.is_alive()
