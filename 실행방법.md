# 김장철병원 팀 전달 패키지

가상 환자 차트와 RAG 지식을 연결한 **로컬 실행용 해커톤 시연 앱**입니다. 압축을 모두 푼 뒤 이 README가 있는 폴더에서 명령을 실행하세요.

팀 공유 메시지는 [docs/TEAM_MESSAGE.md](docs/TEAM_MESSAGE.md), 전화 AI 연결 계약과 JSON 예제는 [docs/PHONE_AI_API.md](docs/PHONE_AI_API.md)에 있습니다. 이벤트 전송 예제는 [examples/phone_ai_sender.py](examples/phone_ai_sender.py)입니다.

GitHub에서 복제했다면 `KimJangCheol` 폴더가 실행 루트입니다. 아래 `cd kimjangcheol-hospital-team` 대신 `cd KimJangCheol`을 사용하세요.

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

- 대시보드: http://127.0.0.1:8767/
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

## 7. 단일 HTML만 볼 때

`dist/index.html`을 더블클릭하면 서버 없이 화면을 볼 수 있습니다. 이 모드는 미리 계산한 시연 카드와 브라우저 저장을 사용합니다. **SQLite 연결·실시간 표현 재검색을 보려면 반드시 run.py로 실행한 주소를 사용하세요.**

## 8. 동봉한 데이터

- 환자 12명, 기본 통화 15건과 통화 위치·대화·직원 처리 기록
- 공통 환자 차트 v1.0: 지병·알레르기·현재/중단 약·처방·측정·진료·방문 준비
- RAG 원문 73개 + 서비스 작성 방문 준비 문서 27개 = 총 100개
- 전문·진료 용어 ↔ 쉬운 표현 27쌍

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
backend/.venv/bin/python -m unittest backend.test_preparation backend.test_knowledge backend.test_app backend.test_chart_format -v
```

Windows:

```text
backend\.venv\Scripts\python.exe -m unittest backend.test_preparation backend.test_knowledge backend.test_app backend.test_chart_format -v
```

테스트는 임시 SQLite를 사용합니다. 실행용 DB를 삭제하거나 초기화하지 않습니다. `python3 run.py --check` / `py run.py --check`는 설치 환경과 파일만 확인합니다.

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
dist/index.html              서버 없이도 보는 단일 HTML
docs/                        팀 공유 메시지·전화 AI API·차트·RAG 설명
examples/                    전화 AI 가상 이벤트 전송 예제
VERIFY.json                   패키지 검증 결과
MANIFEST.sha256               동봉 파일별 SHA-256
```

개인 개발 가상환경·캐시·Git 메타데이터·Sites 프로젝트 설정·배포 인증정보는 포함하지 않았습니다.
