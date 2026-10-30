# 화순군 방문 동행 데스크 — 지자체 대시보드

화순군을 배경으로 만든 가상 지자체 담당자 화면입니다. 어르신의 전화 내용을 확인하고 병원에 질문을 전달한 뒤, 회신을 어르신에게 다시 안내하는 중간 업무를 다룹니다. 병원 화면과 동일한 FastAPI·SQLite를 사용합니다.

## 병원과 구분되는 연결·위기 대응 역할

병원은 환자 차트·개별 건강 기록과 방문 안내를 담당하고, 지자체는 병원 연결·안부 확인·위기 상황의 연락과 인계·후속 확인을 담당합니다. 첫 화면에 진행 중인 위기 대응과 병원 연결 업무를 구분해 배치했습니다.

‘안전·위기 대응’ 메뉴와 주민 상세의 ‘위기 대응’ 탭에서 담당자가 상황을 직접 등록합니다. 통화 문장에 특정 단어가 들어 있다고 자동으로 위급도를 판단하거나 신고하지 않습니다. 초기 ‘연락 두절·안부 확인’ 가상 사례 2건은 통화 후 추가 접수라는 별도 출처로 표시됩니다.

위급하면 화면 입력이나 병원 회신을 기다리지 않고 119에 도움을 요청합니다. 공식 안내의 위치·연락처·발생 경위·현재 상태·주변 위험요소 전달 및 상담원 안내에 따른 통화 유지 절차를 요약했습니다. 근거는 [찾기쉬운 생활법령정보의 응급환자 신고 안내](https://easylaw.go.kr/CSP/CnpClsMain.laf?ccfNo=1&cciNo=2&cnpClsNo=1&csmSeq=906&popMenu=ov)이며, 화면의 기록·인계 단계는 서비스 업무 절차입니다. 기관 검색은 중앙응급의료센터 E-Gen으로 연결합니다. 확인일은 2026-10-30입니다.

연락 버튼은 실제 통화를 실행하지 않습니다. 담당자가 실제 연락한 대상·연결 여부·전달 내용·연락 시각을 입력합니다. 자동 신고·출동·이송 병원 배정 기능은 없습니다. 화면의 시연 좌표는 신고용 현재 위치로 사용하지 않고 직접 확인한 주소·주변 건물을 별도로 적습니다.

위기 대응 상태는 `checking → contacted → handed_over → closed`이며 기존 병원 방문 업무 상태와 별개입니다. 연결되지 않은 연락만으로 인계를 완료할 수 없습니다. 종료하려면 관계기관 인계 확인 또는 주민 본인·보호자에게 직접 확인한 안부 기록이 필요합니다. 재접수 시 이전 대응은 처리 이력에 보존합니다. 진행 중인 위기 대응이 있으면 일반 방문 요청 전송·업무 완료를 보류하고 대응 확인을 먼저 이어갑니다.

기존 `POST /api/municipality/cases/{case_id}/actions`에 다음 action_type을 추가했습니다.

- safety_opened: situation_type, note, 선택 location_text
- safety_updated: note, location_text
- safety_contact_logged: contact_target, contact_result, note
- safety_handoff_recorded: note (연결된 연락 기록 필요)
- safety_closed: resolution=handoff_confirmed 또는 safe_confirmed, note

상황 유형은 health_concern/fall_report/unreachable/other, 연결 결과는 connected/no_answer입니다. 모든 변경에 최신 expected_version이 필요합니다. `safety_guidance`는 bootstrap 응답에 포함되며, 기존 의료 RAG 원문과 별도 파일에서 관리합니다.

## 실행

프로젝트 루트에서 최초 `python3 setup.py`, 이후 `python3 run.py`를 실행합니다. Windows는 `py setup.py`, `py run.py`입니다. 기본 포트는 8767입니다. 기존 서버를 사용 중이었다면 새 라우트가 반영되도록 재시작합니다.

- 지자체 화면: http://127.0.0.1:8767/municipality
- 병원 화면: http://127.0.0.1:8767/
- API 문서: http://127.0.0.1:8767/docs

포트가 사용 중이면 `python3 run.py --port 8777` 또는 Windows의 `py run.py --port 8777`로 실행하고 접속 주소의 포트도 바꿉니다.

## 시연

1. 지자체에서 이정례 님을 선택합니다. 기존 통화의 요약·확정 발화, 가상 지원 정보와 병원 요청이 보입니다.
2. ‘병원 대시보드 열기’를 눌러 병원 화면으로 이동합니다.
3. 상단 ‘지자체 연계 요청’에서 이정례 님의 요청에 답변을 작성합니다. 환자 이름을 누르면 병원 차트 쪽으로 이동합니다.
4. 지자체 화면에서 새로고침 버튼을 누르거나 약 8초를 기다립니다. 상태가 ‘재안내 필요’로 바뀌고 병원 답변이 보입니다.
5. 주민에게 안내했다고 가정하고 ‘통화 연결·안내’를 선택하여 전달 내용을 저장합니다. 실제 전화 발신 기능은 없습니다.
6. ‘안내 완료’가 되면 마무리 내용을 입력하여 ‘처리 완료’로 기록합니다.
7. ‘처리 이력’에서 요청·병원 답변·연락 결과·완료 기록을 확인합니다. 서버 재시작 후에도 보존됩니다.

다른 업무는 임정숙 님처럼 ‘접수’ 상태에서 시작할 수 있습니다. 담당자를 저장하고 병원에 확인할 질문을 전달합니다. 담당자 미배정이면 전송을 막습니다.

부재중·다시 연락 요청은 완료로 바뀌지 않으며 ‘주민 재안내’에서 다시 확인할 수 있습니다. 병원 회신 이전의 연락 기록도 남길 수 있지만, 회신 후 안내가 끝나야 업무를 완료할 수 있습니다.

‘통화 내용 기록’에서는 과거 통화까지 볼 수 있습니다. 업무 미등록 통화를 선택하면 확정 발화·요약을 보고 ‘이 통화에 업무 등록하기’로 연결합니다. 같은 통화에 두 개의 업무를 생성하지 않습니다.

## 데이터와 화면 범위

- 초기 통화 15건을 재사용하고 가상 연계 업무 12건을 추가했습니다.
- 독거·동행·기기·이동 지원 정보는 별도로 설정한 가상 시나리오이며 병력에서 자동 추론하지 않습니다. 새로 등록한 업무의 지원 정보는 ‘미확인’입니다.
- 지자체 API는 이름·연령·연락처·지역·방문 목적·요약·통화 내용·지원 정보만 제공합니다. 환자 차트와 처방·복용약 목록은 포함하지 않습니다.
- ‘우선 확인’은 담당자의 업무 분류이며 응급도나 의료적 중증도를 뜻하지 않습니다.
- ‘확인 기한’은 담당자가 정하는 다음 확인 시각이며 실제 병원 예약일이 아닙니다. 입력·표시는 한국시간입니다.
- 지역별 집계는 주민이 말씀한 지역 기준입니다. 좌표는 지원 정보에서 가상 위치로 구분하여 보여줍니다.
- 병원·지자체 담당자 이름은 시연용으로 고정되어 있습니다. 역할별 화면과 API는 구현했지만 로그인·권한 검증은 구현하지 않았습니다.
- 이 앱 안의 두 화면 사이에 데이터를 저장하는 연계이며 외부 기관에 실제 메시지를 발송하지 않습니다. 예약·원격진료·의료 판단·차량 자동 배차도 하지 않습니다.

## 상태

`received → checking → hospital_requested → response_received → resident_informed → completed`

- received: 접수
- checking: 담당자가 정보 확인 중
- hospital_requested: 병원 데스크에 요청 저장, 답변 대기
- response_received: 병원이 답변 저장, 주민 재안내 필요
- resident_informed: 병원 회신 이후 주민에게 ‘통화 연결·안내’ 기록
- completed: 안내 후 후속 조치 마무리

담당자를 지정하면 접수에서 확인 중으로 바뀝니다. 병원 회신은 병원 API에서만 기록합니다. 완료 업무에는 추가 메모만 허용합니다. 이력과 상태는 하나의 트랜잭션으로 저장하고 낡은 expected_version은 409로 거절합니다.

## API

| 메서드·경로 | 역할 |
|---|---|
| GET /api/municipality/bootstrap | 지자체 목록·전체 통화 기본정보·담당자·처리 기록 |
| GET /api/municipality/cases/{case_id} | 선택 업무·원 통화 발화·위치·처리 이력 |
| GET /api/municipality/calls/{call_id} | 업무 미등록 통화를 포함한 통화 원문 조회 |
| POST /api/municipality/cases | 기존 통화에 업무 등록 |
| POST /api/municipality/cases/{case_id}/actions | 배정·메모·병원 요청·주민 연락·완료 |
| GET /api/team/hospital/referrals | 병원에서 보는 지자체 요청 목록 |
| POST /api/team/hospital/referrals/{case_id}/responses | 병원 회신 저장 |

지자체 actions 예시:

```json
{
  "action_type": "hospital_requested",
  "expected_version": 2,
  "note": "혼자 방문할 어르신의 도착 후 접수 장소를 확인해 주세요."
}
```

지원 action_type: metadata_updated, note_added, hospital_requested, contact_logged, completed. metadata_updated는 assignee·priority·due_at을 받으며 due_at=null은 기한 미지정입니다. priority는 normal/attention입니다. contact_logged는 outcome=connected/no_answer/callback_requested와 note를 받습니다. 쓰기 전 최신 조회의 version을 expected_version으로 사용합니다.

병원 회신 예시:

```json
{
  "expected_version": 3,
  "response_text": "[가상 회신] 도착 후 1층 안내 데스크에서 문의해 주세요."
}
```

두 예시의 version 숫자는 예시이며 조회한 값을 사용해야 합니다. 상세 필드·제약은 /docs를 기준으로 확인합니다.

## DB·소스·정적 시연

- backend/coordination.py: 별도 SQLModel 테이블·상태 처리·API
- backend/fixtures/coordination.json: 가상 업무 12건과 초기 처리 이력
- src/municipality/: 지자체 화면 HTML·CSS·JS
- src/hospital-coordination.js: 병원 요청함·답변 창
- dist/municipality.html: 단일 HTML 시연본
- scripts/build.mjs: 병원·지자체 HTML을 함께 생성

`node scripts/build.mjs`로 두 화면을 빌드합니다. HTML을 직접 열면 브라우저 localStorage에 저장합니다. 같은 HTTP origin으로 정적 파일을 제공하면 두 정적 화면도 브라우저 저장을 공유합니다. file:// 직접 열기의 저장 공유 여부는 브라우저에 따라 다를 수 있으므로 병원↔지자체 연계 시연은 FastAPI 실행을 권장합니다. API 모드에서 서버 연결에 실패해도 정적 데이터로 몰래 대체하지 않습니다.

SQLite는 기존 backend/.local-data/hospital.sqlite3를 사용하며 coordination_cases와 coordination_actions 두 테이블을 추가합니다. 기존 환자 차트·통화·병원 직원 확인 상태는 수정하지 않습니다. 초기 업무는 동일 call_id가 없을 때만 삽입하여 재시작 때 담당자 작업을 보존합니다.

## 검증

```sh
backend/.venv/bin/python -m unittest backend.test_coordination backend.test_preparation backend.test_knowledge backend.test_app backend.test_chart_format -v
```

macOS·Python 3.13에서 총 23개 테스트가 통과했습니다. 신규 검증은 위기 대응의 미연결·인계·직접 안부 확인·재접수·경합 처리와 병원↔지자체 전체 상태 흐름, 부재중 처리, 충돌 방지, 재시작 보존, 과거 통화 업무 등록, 지자체 응답에서 차트 제외를 포함합니다. JS 구문 검사·HTML ID 참조 검사와 실제 로컬 HTTP 응답을 확인했습니다. Work 설정에서 브라우저 제어가 비활성화되어 브라우저 클릭 및 시각 검증은 수행하지 못했습니다.
