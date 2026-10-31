"""Fail closed when dashboard configuration could mix data or roles."""

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Final

DATABASE_KEYS: Final = (
    "HOSPITAL_DASHBOARD_DB_PATH",
    "HOSPITAL_PHONE_DB_PATH",
    "KIROTHON_DB_PATH",
)
TOKEN_KEYS: Final = (
    "HOSPITAL_DASHBOARD_TOKEN",
    "MUNICIPAL_INTAKE_TOKEN",
    "MUNICIPAL_STAFF_TOKEN",
)


class InvalidDeployment(RuntimeError):
    """Deployment is missing a prerequisite for safe data separation."""


def validate_deployment(environment: Mapping[str, str]) -> Path:
    """Validate non-overlapping DB paths and independent role credentials."""
    paths: list[Path] = []
    for key in DATABASE_KEYS:
        raw = environment.get(key, "")
        path = Path(raw)
        if not raw or not path.is_absolute():
            raise InvalidDeployment(f"{key} must be an absolute path")
        paths.append(path.resolve())
    if len(set(paths)) != len(paths):
        raise InvalidDeployment("Dashboard, phone and template databases must differ")

    tokens = [environment.get(key, "") for key in TOKEN_KEYS]
    if any(re.fullmatch(r"[A-Za-z0-9_-]{32,}", token) is None for token in tokens) or len(set(tokens)) != len(tokens):
        raise InvalidDeployment("Three different URL-safe role tokens of at least 32 characters are required")
    return paths[0]
