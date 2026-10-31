"""Versioned staff actions for municipal and hospital consultation workflows."""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic import Field as InputField
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backend.consultations import require_dashboard_token
from backend.municipal_auth import require_municipal_staff
from backend.municipal_intake import MunicipalCase, _case


class StaffWorkflowEvent(SQLModel, table=True):
    __tablename__ = 'municipal_workflow_events'
    id: str = Field(primary_key=True)
    call_id: str = Field(index=True)
    actor_name: str
    action: str
    note: str
    occurred_at: str


class HospitalWorkflow(SQLModel, table=True):
    __tablename__ = 'hospital_case_workflows'
    call_id: str = Field(primary_key=True)
    version: int = 0
    status: str = 'received'
    actor_name: str = ''
    note: str = ''
    updated_at: str | None = None


class HospitalWorkflowEvent(SQLModel, table=True):
    __tablename__ = 'hospital_workflow_events'
    id: str = Field(primary_key=True)
    call_id: str = Field(index=True)
    version: int
    action: str
    actor_name: str
    note: str
    occurred_at: str


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    expected_version: int = InputField(ge=0)
    action: Literal['start', 'complete']
    actor_name: str = InputField(min_length=1, max_length=100)
    note: str = InputField(default='', max_length=2000)

    @field_validator('actor_name')
    @classmethod
    def actor_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('담당자 이름을 입력해 주세요.')
        return value.strip()


def workflow_history(session: Session, call_id: str) -> list[dict]:
    rows = session.exec(select(StaffWorkflowEvent).where(StaffWorkflowEvent.call_id == call_id)
                        .order_by(StaffWorkflowEvent.occurred_at, StaffWorkflowEvent.id)).all()
    return [row.model_dump(exclude={'id', 'call_id'}) for row in rows]


def hospital_workflow(session: Session, call_id: str) -> dict:
    row = session.get(HospitalWorkflow, call_id)
    if row is None:
        return {'version': 0, 'status': 'received', 'actor_name': '', 'note': '', 'updated_at': None}
    return row.model_dump(exclude={'call_id'})


def hospital_handed_over(session: Session, call_id: str) -> bool:
    from backend.municipal_handoff import MunicipalHandoff

    return session.exec(select(MunicipalHandoff).where(MunicipalHandoff.call_id == call_id)).first() is not None


def reset_for_new_handoff(session: Session, call_id: str) -> None:
    """Newly reviewed clinical data requires a fresh hospital review."""
    row = session.get(HospitalWorkflow, call_id)
    if row is None:
        return
    row.version += 1
    row.status = 'received'
    row.actor_name = ''
    row.note = ''
    row.updated_at = datetime.now(UTC).isoformat()
    session.add(row)
    session.add(HospitalWorkflowEvent(id=str(uuid4()), call_id=call_id, version=row.version,
        action='new_handoff', actor_name='', note='', occurred_at=row.updated_at))


def register_workflow_routes(app: FastAPI, engine: Engine) -> None:
    @app.post('/api/municipality/inbox/{call_id}/workflow', dependencies=[Depends(require_municipal_staff)])
    def municipal_action(call_id: str, body: WorkflowInput) -> dict:
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            case = session.get(MunicipalCase, call_id)
            if case is None:
                raise HTTPException(404, '접수된 통화가 없습니다.')
            if case.version != body.expected_version:
                raise HTTPException(409, '상담이 변경되었습니다. 다시 확인해 주세요.')
            allowed = ('acknowledged', 'in_progress', 'handed_over') if body.action == 'complete' else ('acknowledged',)
            if case.unread or case.status not in allowed:
                raise HTTPException(409, '새 상담 내용을 확인한 뒤 처리해 주세요.')
            case.status = 'in_progress' if body.action == 'start' else 'completed'
            case.version += 1
            case.updated_at = datetime.now(UTC).isoformat()
            session.add(case)
            session.add(StaffWorkflowEvent(id=str(uuid4()), call_id=call_id, actor_name=body.actor_name,
                action=body.action, note=body.note.strip(), occurred_at=case.updated_at))
            session.commit()
            session.refresh(case)
            result = _case(session, case)
            result['workflow_history'] = workflow_history(session, call_id)
            result['hospital_workflow'] = hospital_workflow(session, call_id)
            result['hospital_handed_over'] = hospital_handed_over(session, call_id)
            return {'case': result}

    @app.post('/api/team/municipality-cases/{call_id}/workflow', dependencies=[Depends(require_dashboard_token)])
    def hospital_action(call_id: str, body: WorkflowInput) -> dict:
        from backend.municipal_handoff import MunicipalHandoff

        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            if session.exec(select(MunicipalHandoff).where(MunicipalHandoff.call_id == call_id)).first() is None:
                raise HTTPException(404, '인계된 상담이 없습니다.')
            row = session.get(HospitalWorkflow, call_id)
            if row is None:
                row = HospitalWorkflow(call_id=call_id)
            if row.version != body.expected_version:
                raise HTTPException(409, '처리 상태가 변경되었습니다. 다시 확인해 주세요.')
            if row.status == 'completed' or (body.action == 'start' and row.status == 'in_progress'):
                raise HTTPException(409, '이미 처리된 상태입니다.')
            row.status = 'in_progress' if body.action == 'start' else 'completed'
            row.version += 1
            row.actor_name = body.actor_name
            row.note = body.note.strip()
            row.updated_at = datetime.now(UTC).isoformat()
            session.add(row)
            session.add(HospitalWorkflowEvent(id=str(uuid4()), call_id=call_id, version=row.version,
                action=body.action, actor_name=row.actor_name, note=row.note, occurred_at=row.updated_at))
            session.commit()
            session.refresh(row)
            return {'workflow': row.model_dump(exclude={'call_id'})}
