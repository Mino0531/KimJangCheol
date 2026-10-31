"""Explicit, fail-closed settings for local and integrated deployments."""

from dataclasses import dataclass, field
from os import getenv
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

ENV_FILE: Final = Path(__file__).resolve().parent.parent / ".env"


def load_environment(path: Path | None = None) -> None:
    """Load the backend's environment file without replacing existing variables."""
    load_dotenv(dotenv_path=path or ENV_FILE, override=False)


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: str = "hospital.sqlite3"
    environment: str = "production"
    service_token: str = ""
    admin_token: str = ""
    dev_verification_token: str = ""
    seed_demo: bool = False
    hospital_url: str = ""
    hospital_token: str = field(default="", repr=False)
    municipal_url: str = ""
    municipal_token: str = field(default="", repr=False)
    public_data_key: str = field(default="", repr=False)
    azure_speech_key: str = field(default="", repr=False)
    azure_speech_region: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=getenv("HOSPITAL_DB_PATH", "hospital.sqlite3"),
            environment=getenv("HOSPITAL_ENV", "production"),
            service_token=getenv("HOSPITAL_SERVICE_TOKEN", ""),
            admin_token=getenv("HOSPITAL_ADMIN_TOKEN", ""),
            dev_verification_token=getenv("HOSPITAL_DEV_VERIFICATION_TOKEN", ""),
            seed_demo=getenv("HOSPITAL_SEED_DEMO", "false").lower() == "true",
            hospital_url=getenv("HOSPITAL_DASHBOARD_URL", ""),
            hospital_token=getenv("HOSPITAL_DASHBOARD_TOKEN", ""),
            municipal_url=getenv("MUNICIPAL_INTAKE_URL", ""),
            municipal_token=getenv("MUNICIPAL_INTAKE_TOKEN", ""),
            public_data_key=getenv("DATA_GO_KR_SERVICE_KEY", ""),
            azure_speech_key=getenv("AZURE_SPEECH_KEY", ""),
            azure_speech_region=getenv("AZURE_SPEECH_REGION", ""),
        )
