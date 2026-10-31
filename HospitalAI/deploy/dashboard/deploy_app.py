"""ASGI entry point for an isolated dashboard process.

The deployment environment is validated before importing the original app,
which otherwise opens a database during module import.
"""

import os

from deploy_config import validate_deployment

os.environ["HOSPITAL_DB_PATH"] = str(validate_deployment(os.environ))

from backend.app import app  # noqa: E402  -- DB path must be set before import
from backend.clinical_digest import register_digest_routes  # noqa: E402

register_digest_routes(app)
