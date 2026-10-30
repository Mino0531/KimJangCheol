팀원분들, 최신 v3 공유합니다.

이번 버전에는 병원 대시보드와 지자체의 병원 연결·위기 대응 화면, 가상 DB, 환자 12명·통화 15건·연계 업무 12건·안부 확인 사례 2건, RAG 100개를 포함했습니다.

GitHub: https://github.com/Mino0531/KimJangCheol
ZIP: downloads/kimjangcheol-hospital-team-2026-10-30-v3.zip

처음 받는 분은 저장소를 git clone하거나 ZIP을 모두 풀고 프로젝트 폴더에서 실행해 주세요.

macOS / Linux:

```sh
python3 setup.py
python3 run.py
```

Windows:

```text
py setup.py
py run.py
```

Python 3.13 권장입니다. 기본 실행에는 Node.js나 AWS API 키가 필요하지 않습니다.

- 병원: http://127.0.0.1:8767/
- 지자체: http://127.0.0.1:8767/municipality
- API 테스트: http://127.0.0.1:8767/docs

기존에 복제한 분은 작업 내용을 현재 브랜치에 커밋하고 서버를 종료한 뒤 main에서 최신 코드를 받아 주세요.

```sh
git switch main
git pull --ff-only origin main
python3 setup.py
python3 run.py
```

Windows는 Python 명령을 py로 바꾸면 됩니다. 기존 로컬 DB는 보존됩니다. 최신 API 반영을 위해 서버 재시작이 필요합니다. 각자 PC의 DB는 서로 별개입니다.

시연은 지자체에서 이정례 님의 요청 확인 → 병원 화면 상단 요청함에서 답변 저장 → 지자체에서 주민 재안내·완료 순서입니다. 안전·위기 대응은 별도로 현재 위치와 전달받은 상황, 실제 연락 결과·인계·후속 확인을 기록합니다. 실제 전화 발신·신고·출동은 실행하지 않습니다.

전화 AI 담당자 연결:

- 확정된 텍스트 이벤트 전송: POST /api/team/call-events
- 순서: call_started → message_finalized 반복 → call_ended → summary_ready
- 위치 이벤트는 선택이며 현재 가상 좌표만 허용합니다.
- 통화 전체는 같은 call_id, 이벤트마다 고유 event_id를 사용합니다. 재전송 시 event_id와 본문을 유지합니다.
- 자세한 JSON·필드·중복 처리: docs/PHONE_AI_API.md
- 바로 전송하는 가상 예제: examples/phone_ai_sender.py
- 지자체는 통화를 조회하고 별도로 연계 업무를 등록합니다. AI가 자동으로 위급도를 판정하거나 신고하지 않습니다.

코드 수정은 각자 브랜치에서 커밋·푸시하고 PR로 합쳐 주세요. docs/TEAM_COLLABORATION.md에 전체 명령을 정리했습니다. 프론트 변경 시 node scripts/build.mjs로 두 HTML을 빌드하고 변경된 dist 파일을 함께 올립니다. 실행용 DB·가상환경은 Git에 올리지 않습니다.

상세 실행: README.md / 실행방법.md
지자체 기능·API: docs/municipality-dashboard.md
전화 AI API: docs/PHONE_AI_API.md
Git 협업: docs/TEAM_COLLABORATION.md

23개 API·DB 테스트를 검증했습니다. Windows·Python 3.12와 브라우저 클릭·시각 검증은 직접 수행하지 않았습니다. 방문 준비는 어휘 검색·정해진 문장 조합이며, 실제 전화망·EMR·LLM·예약·차량 배차는 연결되지 않았습니다.
