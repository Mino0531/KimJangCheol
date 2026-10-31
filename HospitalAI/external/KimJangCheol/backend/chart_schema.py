"""KJC visit-preparation chart v1: an internal synthetic-data exchange format."""
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecordModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)


class Identity(RecordModel):
    name: str = Field(min_length=1, max_length=100)
    birth_date: date
    sex: Literal['female', 'male', 'unknown']
    phone: str = Field(pattern=r'^010-0000-\d{4}$', description='Synthetic phone only in this demo format.')
    region: str = Field(min_length=1, max_length=200)


class Provenance(RecordModel):
    as_of: date
    recorded_at: datetime
    source_kind: Literal['synthetic_fixture']
    verification_status: Literal['demo_verified', 'unverified']
    source_label: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def timezone(self):
        if self.recorded_at.tzinfo is None:
            raise ValueError('recorded_at에는 시간대가 필요합니다.')
        if self.as_of > self.recorded_at.date():
            raise ValueError('자료 기준일은 기록 작성일 이후일 수 없습니다.')
        return self


class ConditionRecord(RecordModel):
    condition_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=200)
    status: Literal['active', 'resolved', 'unknown']
    diagnosed_on: date | None
    note: str = Field(default='', max_length=500)


class ConditionHistory(RecordModel):
    knowledge_status: Literal['recorded', 'none_reported', 'unknown']
    items: list[ConditionRecord] = Field(max_length=100)


class AllergyRecord(RecordModel):
    allergy_id: str = Field(min_length=1, max_length=160)
    substance: str = Field(min_length=1, max_length=200)
    reaction: str | None = Field(default=None, max_length=300)
    recorded_on: date | None


class AllergyHistory(RecordModel):
    knowledge_status: Literal['recorded', 'none_reported', 'unknown']
    items: list[AllergyRecord] = Field(max_length=100)


class MedicationRecord(RecordModel):
    medication_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=300)
    status: Literal['active', 'stopped', 'unknown']
    started_on: date | None
    ended_on: date | None
    is_fictional: Literal[True]

    @model_validator(mode='after')
    def dates(self):
        if self.started_on and self.ended_on and self.ended_on < self.started_on:
            raise ValueError('약 복용 종료일이 시작일보다 빠릅니다.')
        if self.status == 'active' and self.ended_on:
            raise ValueError('복용 중인 약에는 종료일을 입력하지 않습니다.')
        return self


class MedicationHistory(RecordModel):
    knowledge_status: Literal['recorded', 'none_reported', 'unknown']
    items: list[MedicationRecord] = Field(max_length=100)


class PrescriptionItem(RecordModel):
    medication_id: str = Field(min_length=1, max_length=160)
    medication_name: str = Field(min_length=1, max_length=200, description='Snapshot at prescription time.')


class PrescriptionRecord(RecordModel):
    prescription_id: str = Field(min_length=1, max_length=160)
    prescribed_on: date
    department: str = Field(min_length=1, max_length=100)
    institution: str = Field(min_length=1, max_length=200)
    items: list[PrescriptionItem] = Field(min_length=1, max_length=100)


class ObservationRecord(RecordModel):
    observation_id: str = Field(min_length=1, max_length=160)
    observed_on: date
    systolic: float | None = Field(default=None, gt=0)
    diastolic: float | None = Field(default=None, gt=0)
    glucose: float | None = Field(default=None, gt=0)
    blood_pressure_unit: Literal['mmHg'] = 'mmHg'
    glucose_unit: Literal['mg/dL'] = 'mg/dL'
    glucose_context: Literal['fasting', 'random', 'unknown'] = 'unknown'

    @model_validator(mode='after')
    def measurements(self):
        if (self.systolic is None) != (self.diastolic is None):
            raise ValueError('수축기·이완기 혈압은 함께 입력합니다.')
        if self.systolic is None and self.glucose is None:
            raise ValueError('하나 이상의 측정값이 필요합니다.')
        return self


class EncounterRecord(RecordModel):
    encounter_id: str = Field(min_length=1, max_length=160)
    visited_on: date
    department: str = Field(min_length=1, max_length=100)
    institution: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(max_length=1000)


class VisitPreparation(RecordModel):
    mobility: Literal['independent', 'needs_support', 'wheelchair', 'unknown']
    hearing: Literal['standard', 'needs_slow_speech', 'unknown']
    preferred_channel: Literal['voice', 'text', 'unknown']
    companion: Literal['planned', 'not_planned', 'unknown']
    phone_type: Literal['feature_phone', 'smartphone', 'unknown']
    staff_note: str = Field(max_length=800)


class PatientChart(RecordModel):
    patient_id: str = Field(pattern=r'^demo-patient-\d{3}$')
    chart_number: str = Field(pattern=r'^KJC-2026-\d{3}$')
    identity: Identity
    provenance: Provenance
    conditions: ConditionHistory
    allergies: AllergyHistory
    medications: MedicationHistory
    prescriptions: list[PrescriptionRecord] = Field(max_length=500)
    observations: list[ObservationRecord] = Field(max_length=2000)
    encounters: list[EncounterRecord] = Field(max_length=500)
    visit_preparation: VisitPreparation

    @model_validator(mode='after')
    def consistency(self):
        as_of = self.provenance.as_of
        if self.identity.birth_date > as_of:
            raise ValueError('생년월일은 자료 기준일 이후일 수 없습니다.')
        age = as_of.year - self.identity.birth_date.year - ((as_of.month, as_of.day) < (self.identity.birth_date.month, self.identity.birth_date.day))
        if age > 120:
            raise ValueError('시연 대상 연령 범위를 벗어났습니다.')
        for history in [self.conditions, self.allergies, self.medications]:
            if (history.knowledge_status == 'recorded') != bool(history.items):
                raise ValueError('recorded 상태에는 항목이 필요하고, 미확인·없음 상태에는 항목을 넣지 않습니다.')
        collections = [(self.conditions.items, 'condition_id'), (self.allergies.items, 'allergy_id'),
                       (self.medications.items, 'medication_id'), (self.prescriptions, 'prescription_id'),
                       (self.observations, 'observation_id'), (self.encounters, 'encounter_id')]
        for rows, key in collections:
            ids = [getattr(row, key) for row in rows]
            if len(set(ids)) != len(ids):
                raise ValueError(f'{key}가 중복됐습니다.')
        medication_ids = {m.medication_id for m in self.medications.items}
        for rx in self.prescriptions:
            ids = [m.medication_id for m in rx.items]
            if not set(ids) <= medication_ids:
                raise ValueError('처방이 존재하지 않는 약 ID를 참조합니다.')
            if len(set(ids)) != len(ids):
                raise ValueError('처방 내 약 ID가 중복됐습니다.')
        dated = [c.diagnosed_on for c in self.conditions.items] + [a.recorded_on for a in self.allergies.items]
        dated += [d for m in self.medications.items for d in [m.started_on, m.ended_on]]
        dated += [r.prescribed_on for r in self.prescriptions] + [r.observed_on for r in self.observations] + [r.visited_on for r in self.encounters]
        if any(d and d > as_of for d in dated):
            raise ValueError('과거 기록 날짜가 자료 기준일 이후입니다.')
        return self


class ChartBundle(RecordModel):
    format_id: Literal['KJC_VISIT_PREPARATION_CHART']
    schema_version: Literal['1.0']
    hospital_code: Literal['KJC_DEMO']
    data_classification: Literal['synthetic']
    generated_at: datetime
    patients: list[PatientChart] = Field(min_length=1, max_length=200)

    @model_validator(mode='after')
    def uniqueness(self):
        if self.generated_at.tzinfo is None:
            raise ValueError('generated_at에는 시간대가 필요합니다.')
        for key in ['patient_id', 'chart_number']:
            values = [getattr(p, key) for p in self.patients]
            if len(set(values)) != len(values):
                raise ValueError(f'{key}가 중복됐습니다.')
        if any(p.provenance.recorded_at > self.generated_at for p in self.patients):
            raise ValueError('파일 생성 시각이 차트 작성 시각보다 빠릅니다.')
        return self
