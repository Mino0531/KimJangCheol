팀원분들, 병원 방문 준비 서비스 개발본 공유합니다.

첨부한 `김장철병원_팀전달_전체패키지_2026-10-30_v2.zip`에 대시보드, FastAPI 백엔드, SQLite DB 사본, 가상 환자 12명, RAG 문서 100개, 표현 사전 27쌍과 실행 안내를 넣었습니다.

1. 압축을 모두 풀고 `kimjangcheol-hospital-team` 폴더에서 실행해 주세요.

macOS/Linux:

```sh
python3 setup.py
python3 run.py
```

Windows:

```text
py setup.py
py run.py
```

Python 3.13 권장입니다. `setup.py`는 최초 한 번만 실행하면 됩니다. 기본 실행에는 Node.js·AWS API 키가 필요하지 않습니다.

2. 실행 후 접속합니다.

- 대시보드: http://127.0.0.1:8767/
- API 문서 및 요청 테스트: http://127.0.0.1:8767/docs
- 종료: 서버 터미널에서 Ctrl+C

현재는 각자 PC에서 실행하므로 DB도 각각 별개입니다. 다른 PC나 외부 전화 플랫폼에서 접속할 공용 서버 주소는 추후 정해야 합니다.

3. 화면에서 확인할 내용입니다.

- 환자를 누르면 오른쪽에서 환자별 방문 준비와 근거 문서를 확인할 수 있습니다.
- ‘피검사 하러 가요’를 추가 입력하면 표현에 맞춰 다시 검색합니다.
- 환자 차트에서 지병·현재 복용약·과거 처방·진료 기록을 볼 수 있습니다.
- 대화·과거 통화·위치·직원 메모·확인 상태도 조회하고 저장할 수 있습니다.
- ‘새 통화 시연’으로 약 17초 동안 전체 흐름을 확인할 수 있습니다.

4. 전화 AI 담당자 연결 지점입니다.

전화 수신·STT·AI·TTS 처리는 전화 AI 쪽에서 담당하고, 확정된 통화 이벤트는 `POST /api/team/call-events`로 보내주세요.

이벤트 순서:

`call_started` → `message_finalized` 반복 → `call_ended` → `summary_ready`

위치는 선택 사항이며, 현재는 `location_source=simulated`인 가상 좌표만 받습니다.

- 통화 전체에서 같은 call_id를 사용합니다.
- 이벤트마다 새 event_id를 쓰고, 재전송은 같은 event_id·같은 본문을 유지합니다.
- call_started 성공 응답 후 다음 이벤트를 보냅니다.
- speaker는 caller/assistant, sequence는 두 화자를 합쳐 1부터 증가합니다.
- 시간이 포함된 값에는 +09:00 또는 Z 등 시간대가 필요합니다.
- 환자 ID가 확인되지 않으면 null로 보내고, 전화번호만으로 차트를 연결하지 않습니다.
- 현재 시작 이벤트는 이름·나이·연락처·지역·방문 목적 등을 요구합니다. 수집 전부터 연결 상태를 저장해야 한다면 별도 API 확장이 필요합니다.

자세한 필드와 JSON 예제는 `docs/PHONE_AI_API.md`를 확인해 주세요. `examples/phone_ai_sender.py`로 가상 이벤트를 직접 보내볼 수 있습니다.

5. 전화 AI가 조회할 수 있는 API입니다.

- `GET /api/team/calls/{call_id}/preparation`: 해당 통화의 환자별 준비사항
- `GET /api/team/calls/{call_id}/preparation?q=피검사`: 추가 표현을 반영한 준비사항
- `GET /api/team/patients/{patient_id}/chart`: 환자 차트
- `GET /api/team/calls/{call_id}`: 통화·환자·대화·직원 처리 내역

대시보드는 기본적으로 5초마다 DB를 조회합니다. 입력 중이거나 자체 통화 시연 중에는 자동 갱신을 잠시 멈춥니다.

현재는 가상 데이터 기반이며, 방문 준비는 표현 검색과 정해진 문장 조합입니다. 임베딩·LLM 호출과 실제 전화망·EMR·예약은 연결되지 않았습니다. 실제 AI 요약의 출처와 실행 모드를 저장하려면 현재 시연용 메타데이터도 확장해야 합니다.

실행 방법은 `README.md`와 `실행방법.md`, API 연결 설명은 `docs/PHONE_AI_API.md`에 정리했습니다. macOS·Python 3.13에서 압축 해제 실행과 API·DB 테스트 16개를 검증했습니다. Windows·Python 3.12는 실행 안내만 포함되어 있고 직접 검증하지 않았습니다.
