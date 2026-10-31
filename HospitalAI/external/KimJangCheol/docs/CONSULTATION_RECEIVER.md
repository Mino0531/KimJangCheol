# 전화 상담 결과 수신 · 로컬 프로토타입

`POST /api/team/consultation-results`는 동의된 상담의 결과를 별도 SQLite 테이블에 저장합니다. `GET /api/team/consultation-results`는 이를 병원 대시보드에 표시합니다. 두 경로 모두 `Authorization: Bearer <공유 토큰>`이 필요합니다. 동일한 `source`, `event_id`, 본문을 재전송하면 중복으로 처리하며, 동일 ID에 다른 본문이 오면 409를 반환합니다.

결과는 기존의 **인증되지 않은 데모** `/api/team/bootstrap`이나 `/api/team/calls`에 합쳐지지 않습니다. 화면의 별도 상담 결과 영역에서 토큰을 입력해야 조회할 수 있습니다. 사용자 발화와 AI가 생성한 답변은 저장하지만, 답변이 실제 통화에서 재생되었는지는 확인하지 않습니다. 이름, 나이, 지역을 추정하거나 차트에 자동 반영하지 않습니다.

## 로컬 실행

전화 AI 백엔드와 **다른 PowerShell 창**에서, 이 저장소의 루트로 이동해 실행합니다.

```powershell
cd 'C:\Users\nccoss\Documents\ChatGPT\HospitalAI\external\KimJangCheol'
$env:HOSPITAL_DASHBOARD_TOKEN = Read-Host -MaskInput '전화 AI 백엔드와 동일한 공유 토큰'
$env:HOSPITAL_DB_PATH = Join-Path (Get-Location) 'backend\.local-data\hospital.sqlite3'
uv run --with fastapi --with sqlmodel --with uvicorn python -m uvicorn backend.app:app --host 127.0.0.1 --port 8767
```

두 백엔드는 모두 `HOSPITAL_DB_PATH`라는 환경변수를 사용하지만 스키마가 서로 다릅니다. **반드시 서로 다른 SQLite 파일**을 지정하고 환경변수가 공유된 터미널에서 경로를 실수로 재사용하지 마세요. 전화 AI 백엔드에는 같은 `HOSPITAL_DASHBOARD_TOKEN`과 이 서버의 주소를 설정해야 합니다. 토큰은 비밀값으로 취급하고 Git이나 대화에 붙여 넣지 마세요.

브라우저에서 `http://127.0.0.1:8767`을 열어 상담 결과 패널에 동일한 토큰을 입력합니다. 토큰은 페이지 메모리에만 보관하고 연결을 끊으면 지웁니다. 브라우저의 비밀번호 저장 기능은 별개입니다.

이 저장소는 병원 EMR이 아닌 로컬 데모입니다. 저장 데이터 암호화, 의료진 개인별 인증, 권한·감사·삭제 및 실제 병원 시스템 연동은 아직 제공하지 않습니다. 실제 환자 정보로 사용하거나 외부에 서비스를 공개하기 전 별도의 설계·검증이 필요합니다.
