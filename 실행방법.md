# KimJangCheol · 병원·지자체 연결·안전 지원

가상 환자 차트·방문 준비·RAG를 사용하는 병원 화면과, 병원 연결·위기 대응을 지원하는 지자체 화면을 함께 실행하는 **로컬 해커톤 시연 앱**입니다. 현재 팀 공유본은 **v3 (2026-10-30)**입니다.

[최신 전체 ZIP 다운로드](https://github.com/Mino0531/KimJangCheol/raw/refs/heads/main/downloads/kimjangcheol-hospital-team-2026-10-30-v3.zip) · [팀원 Git 협업 가이드](docs/TEAM_COLLABORATION.md) · [지자체 시연·API 안내](docs/municipality-dashboard.md)

팀 공유 메시지는 [docs/TEAM_MESSAGE.md](docs/TEAM_MESSAGE.md), 전화 AI 연결 계약과 JSON 예제는 [docs/PHONE_AI_API.md](docs/PHONE_AI_API.md)에 있습니다. 이벤트 전송 예제는 [examples/phone_ai_sender.py](examples/phone_ai_sender.py)입니다.

GitHub에서 복제했다면 `KimJangCheol` 폴더가 실행 루트입니다. 아래 `cd kimjangcheol-hospital-team` 대신 `cd KimJangCheol`을 사용하세요.

## 빠른 시작

GitHub 초대를 수락한 팀원은 다음과 같이 복제합니다.

```sh
git clone https://github.com/Mino0531/KimJangCheol.git
cd KimJangCheol
python3 setup.py
python3 run.py
```

Windows에서는 마지막 두 줄 대신 `py setup.py`, `py run.py`를 사용합니다. ZIP은 전체를 풀고 `kimjangcheol-hospital-team` 폴더 안에서 설치·실행합니다.

기존 팀원은 현재 브랜치의 작업을 커밋한 뒤 다음 순서로 갱신합니다. 진행 중인 서버는 먼저 종료합니다.

```sh
git switch main
git pull --ff-only origin main
python3 setup.py
python3 run.py
```

Windows에서는 위 Python 명령을 `py`로 바꿉니다. 새 API는 서버를 재시작해야 반영됩니다. 기존 실행용 DB는 보존되고 새 연계 테이블·초기 시연 업무가 추가됩니다. 기존 DB를 삭제하거나 배포용 사본으로 덮어쓰지 마세요.

## 두 화면의 역할

| 화면 | 중심 업무 | 주소 |
|---|---|---|
| 병원 | 환자 차트·지병·현재 약·과거 처방·개인별 방문 준비, 지자체 요청에 답변 | http://127.0.0.1:8767/ |
| 지자체 | 병원 연결·통화 기록·주민 재안내·위기 대응 연락·인계·후속 확인 | http://127.0.0.1:8767/municipality |

지자체 화면에 의료 차트·처방·복용약 목록은 포함하지 않습니다. 두 화면은 같은 로컬 SQLite를 사용합니다. 역할은 화면 구성으로 구분하며 로그인·권한 검증은 아직 구현하지 않았습니다.

## 1. 필요한 것

- Python 3.13 권장. 실제 검증 환경은 macOS / Python 3.13.2입니다.
- 코드의 최소 버전은 Python 3.12입니다. Python 3.12 및 Windows 실행은 이 환경에서 직접 검증하지 않았습니다.
- 첫 패키지 설치 때만 인터넷이 필요합니다. 이후 기본 앱 실행에는 외부 API 키나 인터넷이 필요하지 않습니다.
- Node.js는 소스 수정 후 화면을 다시 빌드할 때만 필요합니다. 기본 실행은 미리 빌드한 HTML을 사용합니다.
- 압축파일 내부에서 실행하지 말고 일반 폴더로 먼저 풀어 주세요.

## 2. macOS / Linux 실행

터미널에서 압축을 푼 폴더로 이동합니다.

```sh
cd kimjangcheol-hospital-team
python3 setup.py
python3 run.py
```

`setup.py`는 최초 한 번 실행하여 `backend/.venv`에만 패키지를 설치합니다. 가상환경 활성화 명령은 필요하지 않습니다. 다음부터는 `python3 run.py`만 실행하세요.

## 3. Windows 실행

압축을 푼 폴더에서 명령 프롬프트(CMD) 또는 PowerShell을 열고 실행합니다.

```text
cd kimjangcheol-hospital-team
py setup.py
py run.py
```

`py` 명령이 없고 `python` 명령이 Python 3.12 이상이라면 `python setup.py`, `python run.py`를 사용하세요. 실행 정책을 바꿀 필요는 없습니다.

## 4. 접속과 종료

- 병원 대시보드: http://127.0.0.1:8767/
- 지자체 대시보드: http://127.0.0.1:8767/municipality
- API 문서: http://127.0.0.1:8767/docs
- 서버 상태: http://127.0.0.1:8767/api/health
- RAG 통계: http://127.0.0.1:8767/api/team/knowledge/stats

서버 터미널은 켜 둡니다. 종료는 해당 터미널에서 `Ctrl+C`입니다.

8767 포트를 다른 프로그램이 사용 중이면 기존 서버를 종료하거나 다른 포트로 실행합니다.

```sh
python3 run.py --port 8777
```

Windows에서는 `py run.py --port 8777`입니다. 접속 주소도 http://127.0.0.1:8777/ 로 바뀝니다.

## 5. DB 사본과 데이터 보존

- 공유한 시점의 SQLite 사본: `data/hospital-demo.sqlite3`
- 실제 실행 중 저장하는 DB: `backend/.local-data/hospital.sqlite3`
- DB 테이블·건수·파일 해시: `data/DB_SUMMARY.json`
- 실제 DB 구조 SQL: `data/schema.sql`

`run.py`는 실행용 DB가 **없는 경우에만** 공유 사본을 복사합니다. 기존 실행용 DB가 있으면 덮어쓰지 않습니다. 메모·확인 상태·추가 통화는 실행용 DB에 남으므로 새로고침·서버 재시작 후에도 보존됩니다.

동봉 DB는 SQLite의 백업 기능으로 복사하여 WAL 내용까지 포함한 일관된 사본입니다. 운영 중인 DB를 단순 복사한 파일이 아닙니다. 모든 환자와 연락처·차트·측정값은 가상 시연 데이터입니다.

이 패키지를 각자 실행하면 팀원별로 별도 DB를 사용합니다. 이 상태에서 메모가 서로 실시간 공유되지는 않습니다. 현재 서버는 본인 PC의 127.0.0.1에만 연결됩니다. 팀 공용 서버 배포와 실제 전화망·EMR 연동은 별도 작업입니다.

## 6. 3분 시연 순서

1. 목록에서 **이정례**를 누릅니다.
2. 오른쪽 **방문 준비**에서 준비 문장, 선택 이유, 직원 질문과 문서 ID를 확인합니다.
3. 표현 입력란에 **피검사 하러 가요**를 입력하고 **다시 찾기**를 누릅니다.
4. **환자 차트**에서 지병·현재 약·과거 처방·혈압/혈당·진료 기록을 확인합니다.
5. **대화 내역**, **과거 기록**으로 이전 통화와 당시 위치를 살펴봅니다.
6. 직원 메모를 저장하고 확인 상태를 바꾼 뒤 새로고침하여 저장을 확인합니다.
7. **새 통화 시연**을 누르면 약 17초 동안 시작 → 위치 → 대화 → 종료 → 요약이 진행됩니다.
8. 연락처 복사, 검색·필터, CSV 내보내기를 확인합니다.

## 지자체 연결·위기 대응 시연

1. 지자체 화면에서 **이정례**의 병원 요청을 확인합니다.
2. 병원 화면의 **지자체 연계 요청 → 답변 작성**에서 가상 회신을 저장합니다.
3. 지자체를 새로고침하면 **재안내 필요**로 바뀝니다. 주민에게 전달했다고 가정하여 연락 결과를 기록하고 방문 지원을 완료합니다.
4. 지자체의 **안전·위기 대응**에서 가상 안부 확인 사례 2건을 살펴봅니다. 현재 위치와 전달받은 사실을 기록할 수 있습니다.
5. 연락 결과를 ‘연결되지 않음’으로 기록하면 인계 확인으로 넘어가지 않습니다. 실제 연결을 가정한 시연 기록 후 인계 확인 또는 주민·보호자 안부 확인을 남겨 후속 확인을 마칩니다.

위기 대응은 일반 방문 지원과 별도 상태로 저장합니다. 위급하면 화면 입력이나 병원 회신을 기다리지 않고 119에 도움을 요청하도록 안내합니다. 버튼은 실제 전화·신고·출동을 실행하지 않고, 담당자가 실제 수행한 연락·인계 결과를 기록하는 기능입니다. 자동 의학적 중증도 판별·진단은 하지 않습니다.

공식 신고 안내와 출처는 화면의 **대응 절차 보기**에 표시합니다. 지자체 API 계약과 저장 규칙은 [상세 안내](docs/municipality-dashboard.md)를 참고하세요.

## 7. 단일 HTML만 볼 때

`dist/index.html`은 병원, `dist/municipality.html`은 지자체의 서버 없는 시연 화면입니다. 두 파일은 같은 폴더에 두세요. 이 모드는 미리 계산한 시연 카드와 브라우저 저장을 사용합니다. **SQLite 연결·실시간 표현 재검색을 보려면 반드시 run.py로 실행한 주소를 사용하세요.**

정적 HTML의 브라우저 저장과 SQLite 저장은 별개입니다. file://에서 두 파일의 저장 공유 여부는 브라우저에 따라 달라서 병원↔지자체 연결 시연은 run.py를 권장합니다.

## 8. 동봉한 데이터

- 환자 12명, 기본 통화 15건과 통화 위치·대화·직원 처리 기록
- 공통 환자 차트 v1.0: 지병·알레르기·현재/중단 약·처방·측정·진료·방문 준비
- RAG 원문 73개 + 서비스 작성 방문 준비 문서 27개 = 총 100개
- 전문·진료 용어 ↔ 쉬운 표현 27쌍
- 지자체 연계 업무 12건, 안부 확인 추가 접수 사례 2건
- 공식 출처가 표시된 위기 대응 안내 (의료 RAG와 별도 관리)

`backend/fixtures`에는 DB 재구성과 개발용 원본이 있습니다. `data/RAG`에는 100개 문서의 JSON 내보내기와 단어 수준 비교 CSV가 있습니다. 차트 빈 양식과 JSON Schema는 `backend/schemas`에 있습니다.

## 9. 현재 구현 범위

방문 준비 결과는 **어휘·별칭 검색 + 정해진 방문 준비 문장 조합**입니다. 임베딩·벡터 검색·Bedrock·LLM 호출은 하지 않습니다. 미검토 의료 원문의 금식·복약·처치 문장을 행동 지시로 출력하지 않습니다. 단어 수준 1~3은 서비스 내부 기준이며 검증된 독해 등급이 아닙니다.

전화번호·STT·TTS·실제 예약·병원 접수·EMR은 연결되지 않았습니다. 지도는 개략도이고 위치는 시연용입니다. 기본 실행은 실제 의료서비스나 응급 판단을 제공하지 않습니다.

## 10. 개발·검증

프론트엔드 수정 후 Node.js 20 이상 환경에서 다음을 실행합니다. npm 설치는 필요하지 않습니다.

```sh
node scripts/build.mjs
```

백엔드·DB 테스트(macOS/Linux):

```sh
backend/.venv/bin/python -m unittest backend.test_coordination backend.test_preparation backend.test_knowledge backend.test_app backend.test_chart_format -v
```

Windows:

```text
backend\.venv\Scripts\python.exe -m unittest backend.test_coordination backend.test_preparation backend.test_knowledge backend.test_app backend.test_chart_format -v
```

테스트는 임시 SQLite를 사용합니다. 실행용 DB를 삭제하거나 초기화하지 않습니다. `python3 run.py --check` / `py run.py --check`는 설치 환경과 파일만 확인합니다.

23개 테스트를 macOS·Python 3.13에서 검증했습니다. Windows·Python 3.12와 브라우저 클릭·시각 검증은 수행하지 않았습니다.

## 11. 자주 생기는 문제

- **Python을 찾을 수 없음:** Python 3.13을 설치하고 새 터미널에서 버전을 확인하세요.
- **패키지가 없음:** `setup.py`를 먼저 실행하세요. 첫 설치에는 인터넷이 필요합니다.
- **페이지가 열리지 않음:** 서버 터미널의 실행 주소·포트와 오류를 확인하세요.
- **화면이 DB 모드가 아님:** 파일을 직접 여는 대신 run.py가 표시한 HTTP 주소로 접속하세요.
- **다른 PC에서 안 열림:** 127.0.0.1은 각 PC 자신입니다. 각자 실행하거나 별도 공용 서버가 필요합니다.
- **기존 DB 때문에 샘플 상태가 다름:** 저장된 변경이 유지되는 정상 동작입니다. 먼저 서버를 종료하고 DB를 백업한 뒤 비교하세요. DB를 무작정 삭제하지 마세요.

## 폴더 안내

```text
README.md / 실행방법.md / 실행방법.txt       먼저 읽을 실행 안내
setup.py / run.py             첫 설치 / 서버 실행
src/                          대시보드 소스
scripts/                      HTML 빌드 및 시연 데이터 도구
backend/                      FastAPI·SQLModel·테스트
backend/fixtures/             가상 환자·통화·RAG 원본
backend/schemas/              공통 환자 차트 양식
data/                        DB 사본·구조 SQL·RAG 내보내기
dist/index.html              병원 단일 HTML
dist/municipality.html       지자체 단일 HTML
docs/                        실행·협업·전화 AI API·지자체·차트·RAG 설명
examples/                    전화 AI 가상 이벤트 전송 예제
VERIFY.json                   패키지 검증 결과
MANIFEST.sha256               동봉 파일별 SHA-256
```

개인 개발 가상환경·캐시·Git 메타데이터·Sites 프로젝트 설정·배포 인증정보는 포함하지 않았습니다.
