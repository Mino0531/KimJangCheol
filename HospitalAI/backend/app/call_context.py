"""Shared dependencies for consent-gated call endpoint groups."""

from collections.abc import Callable
from dataclasses import dataclass, field

from app.call_capabilities import CallCapabilities
from app.db import Database, Row
from app.hospital_bridge import HospitalConfig
from app.municipal_bridge import MunicipalConfig
from app.orchestrator import ServiceBundle
from app.schemas import SessionInput


@dataclass(frozen=True, slots=True)
class CallContext:
    database: Database
    search_database: Database
    services: ServiceBundle
    auth: Callable[..., None]
    session_or_404: Callable[[str], Row]
    create_session: Callable[[SessionInput], dict[str, str | bool]]
    capabilities: CallCapabilities
    hospital: HospitalConfig | None = None
    municipal: MunicipalConfig | None = None
    public_data_key: str = field(default="", repr=False)
