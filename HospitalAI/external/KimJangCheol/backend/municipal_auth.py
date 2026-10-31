"""Separate service and staff credentials for municipal records."""
from hmac import compare_digest
from os import getenv
from typing import Annotated
from fastapi import Header, HTTPException


def _check(expected: str, authorization: str | None, other: str) -> None:
    hospital = getenv('HOSPITAL_DASHBOARD_TOKEN', '')
    if (not expected or (other and compare_digest(expected, other))
            or (hospital and compare_digest(expected, hospital))
            or not authorization or not compare_digest(authorization, f'Bearer {expected}')):
        raise HTTPException(401, 'Authentication required')


def require_municipal_intake(authorization: Annotated[str | None, Header()] = None) -> None:
    _check(getenv('MUNICIPAL_INTAKE_TOKEN', ''), authorization, getenv('MUNICIPAL_STAFF_TOKEN', ''))


def require_municipal_staff(authorization: Annotated[str | None, Header()] = None) -> None:
    _check(getenv('MUNICIPAL_STAFF_TOKEN', ''), authorization, getenv('MUNICIPAL_INTAKE_TOKEN', ''))
