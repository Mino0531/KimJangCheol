"""Explicitly configured, separate-process production phone gateway."""

from importlib import import_module
from os import getenv
from pathlib import Path
import sys

from fastapi import FastAPI


class RuntimeConfigurationError(RuntimeError):
    """The phone service cannot start with unsafe runtime settings."""


def initialize() -> FastAPI:
    backend = Path(getenv("HOSPITAL_BACKEND_DIR", str(Path(__file__).resolve().parent / "backend"))).resolve()
    database_setting = getenv("HOSPITAL_DB_PATH", "")
    proxy_secret = getenv("HOSPITAL_TRUSTED_PROXY_TOKEN", "")
    service_secret = getenv("HOSPITAL_SERVICE_TOKEN", "")
    if (getenv("HOSPITAL_ENV", "") != "production" or len(proxy_secret) < 32 or len(service_secret) < 32
            or proxy_secret == service_secret):
        raise RuntimeConfigurationError("Explicit production environment and separate private secrets are required")
    if not database_setting or not Path(database_setting).is_absolute():
        raise RuntimeConfigurationError("An absolute, phone-specific SQLite path is required")
    database = Path(database_setting).resolve()
    if (not backend.is_dir() or (backend / ".env").exists() or database.is_relative_to(backend)
            or database.name in {"records.sqlite3", "ai_quota.sqlite3"} or not database.parent.is_dir()):
        raise RuntimeConfigurationError("Unsafe backend, environment file, or database path")

    sys.path.insert(0, str(backend))
    main = import_module("app.main")
    capabilities = import_module("app.call_capabilities")
    database_module = import_module("app.db")
    gateway = import_module("gateway")
    caps = capabilities.CallCapabilities(database_module.Database(str(database)), service_secret, "production")
    secrets = gateway.GatewaySecrets(proxy_secret, service_secret)
    return gateway.build_gateway(main.app, caps, secrets)


app = initialize()
