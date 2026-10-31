# 병원 방문 준비 서비스 · 공통 환자 차트 양식 v1.0

우리 서비스의 가상 데이터 교환 양식입니다. 공식 진료기록지·법정 의료서식·의료기관 EMR 표준 또는 FHIR 호환 형식을 구현한 것은 아닙니다. 현재 수신기는 가상 환자와 가상 연락처만 허용합니다.

목적은 어르신의 방문 준비에 필요한 기본정보·기존 지병·복용약·과거 기록·도움이 필요한 상황을 같은 구조로 전달하는 것입니다. AI의 진단, 처방 생성·변경, 복용 판단은 포함하지 않습니다.

## 제공 파일

- `backend/schemas/patient-chart-v1.schema.json`: 기계 검증용 JSON Schema
- `backend/schemas/patient-chart-v1.template.json`: 한 명을 작성할 수 있는 유효한 빈 양식
- `backend/fixtures/charts-v1.json`: 이 양식으로 작성한 가상 환자 12명
- `backend/chart_schema.py`: 실제 API·파일 가져오기에 사용하는 Pydantic 검증 모델
- `backend/generate_charts.py`: 스키마·빈 양식·가상 데이터 재생성 도구

UTF-8 JSON, 확장자 `.json`을 사용합니다. 하나의 파일에 환자 여러 명을 넣을 수 있으며 최대 200명입니다. 양식에 정의되지 않은 필드는 오류입니다. 필수 배열은 빈 배열이라도 반드시 입력합니다.

## 파일 머리말

| 필드 | 값·의미 |
|---|---|
| format_id | 고정값 `KJC_VISIT_PREPARATION_CHART` |
| schema_version | 고정값 `1.0` |
| hospital_code | 고정값 `KJC_DEMO` |
| data_classification | 고정값 `synthetic` |
| generated_at | 시간대가 포함된 생성 시각. 예: `2026-10-30T14:00:00+09:00` |
| patients | 환자 차트 배열 |

## 환자 한 명의 차트

| 그룹 | 필드 | 내용 |
|---|---|---|
| 식별 | patient_id, chart_number | 환자 연결 키·차트번호. 이름이나 전화번호로 자동 병합하지 않음 |
| 기본정보 | identity | 이름, 생년월일, 성별, 가상 연락처, 거주 지역 |
| 출처 | provenance | 자료 기준일, 작성 시각, 가상 자료 출처, 검증 구분 |
| 지병 | conditions | 인지 상태 + 질환 ID·이름·현재/과거 상태·진단일·비고 |
| 알레르기 | allergies | 인지 상태 + 알레르기 ID·원인·반응·기록일 |
| 약 | medications | 인지 상태 + 약 ID·이름·목적·현재 상태·시작/종료일 |
| 이전 처방 | prescriptions | 처방 ID·날짜·진료과·기관·약 ID 및 당시 약 이름 |
| 측정값 | observations | 측정 ID·날짜·혈압·혈당·단위·혈당 측정 조건 |
| 진료 이력 | encounters | 진료 ID·방문일·진료과·기관·제목·기록 내용 |
| 방문 준비 | visit_preparation | 보행·청취·선호 채널·동행 계획·휴대전화·안내 참고사항 |

### 식별·출처

- patient_id: `demo-patient-001` 형식.
- chart_number: `KJC-2026-001` 형식.
- identity.sex: `female`, `male`, `unknown`.
- identity.phone: `010-0000-1001` 형식의 가상 번호.
- provenance.source_kind: `synthetic_fixture`.
- provenance.verification_status: `demo_verified` 또는 `unverified`. 의료진이 실환자 정보를 확인했다는 의미가 아닙니다.
- provenance.as_of: `YYYY-MM-DD`. 차트의 자료 기준일이며 통화 시각과 별도입니다.

### 미확인과 없음의 구분

conditions, allergies, medications는 `knowledge_status`와 `items`를 가집니다.

| knowledge_status | items | 의미 |
|---|---|---|
| recorded | 최소 1개 | 저장된 항목이 있음 |
| none_reported | 빈 배열 | 없다고 보고된 기록. 의학적으로 부재가 확정된 것은 아님 |
| unknown | 빈 배열 | 자료가 없거나 확인되지 않음 |

미확인을 0·없음·정상으로 자동 변환하지 않습니다. 알 수 없는 날짜·알레르기 반응·측정값은 허용된 위치에서 `null`로 작성합니다.

### 약과 처방

- medications.items[].status: `active`, `stopped`, `unknown`.
- `active`만 현재 복용약 목록에 표시합니다. 중단된 약도 DB와 과거 처방에는 보존합니다.
- 약품은 `is_fictional: true`이며 시연용 이름을 사용합니다. 용량·복용법·처방 권고를 생성하지 않습니다.
- 처방 항목은 같은 환자의 medications에 존재하는 medication_id를 참조해야 합니다.
- prescription의 medication_name은 처방 당시의 이름을 보관하는 스냅샷입니다.
- 과거에 처방됐다는 이유만으로 현재 복용 중이라고 판단하지 않습니다.

### 측정값

- 혈압 단위: `mmHg`. 수축기·이완기를 함께 작성합니다.
- 혈당 단위: `mg/dL`.
- glucose_context: `fasting`, `random`, `unknown`. 시연 데이터는 `unknown`을 사용합니다.
- 혈압만 또는 혈당만 있는 기록을 허용합니다. 선택한 지표에 값이 있는 날짜만 그래프로 표시합니다.
- 단위·숫자·누락 여부를 검사하며, 값의 정상/이상 판정은 수행하지 않습니다.

### 방문 준비

| 필드 | 허용 값 |
|---|---|
| mobility | independent / needs_support / wheelchair / unknown |
| hearing | standard / needs_slow_speech / unknown |
| preferred_channel | voice / text / unknown |
| companion | planned / not_planned / unknown |
| phone_type | feature_phone / smartphone / unknown |
| staff_note | 안내 참고 문장, 최대 800자 |

이 양식의 staff_note는 차트 제공자가 기록한 방문 준비 참고사항입니다. 통화 후 직원이 추가한 메모(`desk_actions`)와 별개로 보관합니다.

## 가져오기 규칙

1. 파일 전체를 스키마로 검증합니다. 형식 버전, 필수 필드, 날짜, 단위, 중복 ID, 처방의 약 참조를 검사합니다.
2. 환자 ID·차트번호와 기존 연결을 확인합니다. 같은 ID의 다른 이름/생년월일, 다른 환자가 쓰는 차트번호는 충돌입니다.
3. 파일은 포함된 각 환자의 완전한 차트 스냅샷입니다. 해당 환자의 차트 항목만 갱신하고 파일에 없는 환자는 그대로 둡니다.
4. 파일 전체를 단일 트랜잭션으로 반영합니다. 중간 환자에 오류가 있으면 전부 롤백합니다.
5. 같은 내용의 파일은 다시 적용하지 않습니다. 더 오래된 자료가 현재 차트를 덮어쓰지 못하게 합니다.
6. `chart_imports`에 파일 해시·가져온 시각·환자 수를, `chart_revisions`에 원본과 이전 차트를 보존합니다.
7. 통화, 과거 위치, 직원 메모·확인 상태는 수정하지 않습니다.

### 검증만 하기

```sh
backend/.venv/bin/python -m backend.import_charts backend/fixtures/charts-v1.json --validate-only
```

### DB에 가져오기

```sh
backend/.venv/bin/python -m backend.import_charts backend/fixtures/charts-v1.json
```

CLI는 실제 SQLite 백업을 만든 뒤 반영합니다. DB를 삭제하거나 초기화하지 않습니다. 서버 최초 실행에도 이 파일을 검증·반영하며, 이미 반영된 파일이면 다시 쓰지 않습니다.

## API

| 메서드·경로 | 역할 |
|---|---|
| GET /api/team/chart-format | 형식 ID·버전·JSON Schema |
| GET /api/team/chart-template | 작성용 빈 JSON |
| POST /api/team/chart-imports | 공통 JSON 본문 검증·가져오기 |
| GET /api/team/chart-imports | 최근 가져오기 내역 |
| GET /api/team/patients/{id}/chart | 화면용으로 변환된 차트 |
| GET /api/team/patients/{id}/chart/export | 해당 환자의 공통 양식 JSON 내보내기 |

422는 형식·참조 오류, 409는 기존 DB와의 식별·기준일 충돌입니다. 로컬 시연 전용 API이며 공개 운영용 인증 체계를 제공하지 않습니다.

## 파일과 DB 매핑

`identity/provenance/visit_preparation/allergies`는 patients의 데이터 필드에 보관합니다. conditions·medications·prescriptions·observations·encounters는 기존 환자별 테이블에 나눠 보관합니다. 화면에는 `active` 약만 투영하고, 전체 약 이력은 `medication_history`로 제공합니다. 원본 공통 차트는 chart_revisions에 별도로 남습니다.
