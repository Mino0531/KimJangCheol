# Hospital AI 백엔드 프로토타입

SQLite에서 상담 정보와 개발용 의료 자료를 찾아 답변하는 FastAPI 프로토타입입니다. 브라우저는 Azure Speech를 사용해 음성을 인식하고, 서버는 인식된 텍스트를 처리합니다. Azure STT 모델 학습이나 실제 전화망 연결은 포함하지 않습니다. `.env.example`은 키를 입력하면 저지연 Gemini와 ElevenLabs를 선택하며, 고추론 모델은 별도 설정 전까지 **모의 구현**입니다. 실제 의료 상담, 진단 또는 환자 진료에 사용하면 안 됩니다.

낙상 후 다리 통증은 먼저 보행 가능 여부를 확인하고, 답변에 따라 응급 안내 또는 진료과 후보와 병원 찾기를 제안합니다. 이는 제한된 시연 규칙이며 다른 모든 위험 신호를 판별하지 않습니다. 질문은 음성 재생 완료가 기록된 경우에만 다음 짧은 답변과 연결합니다. 메시지 조회 응답에는 새 상담 흐름에 한해 선택적 `guidance` 객체가 추가되며, `action`, `question_id`, `specialty`로 다음 행동과 후보를 표시합니다. 기존 응답 필드는 유지됩니다.

## 처리 흐름

1. 브라우저 발신 준비 화면에서 음성 인식 전송을 고지하고, 마이크와 기기 위치 권한을 요청합니다. 전화번호는 인증 수단이 아닙니다.
2. 새 통화는 전화번호·건강 상담 내용·위치 이용과 지자체 보관, 직원 확인 후 병원 전달, 외부 음성·AI 처리를 한 번에 안내하고 음성 동의를 받습니다. 동의 전 발화는 Azure로 인식하지만 Google 의도 분류에는 보내지 않으며, 동의 판정은 서버에서 보수적으로 처리합니다.
3. 동의 후 사용자 발화와 같은 통화의 최근 문맥은 저지연 Gemini가 대화 의도 분류에 사용합니다. 모호하거나 분류에 실패하면 재질문하고, 긴급 표현은 모델 결과를 기다리지 않고 먼저 처리합니다.
4. 일반 상담에서는 저지연 안내와 SQLite 검색 후 고추론 답변을 생성합니다. 답변과 검색 자료, 처리 시간, TTS 상태를 SQLite에 기록하고 ElevenLabs를 설정하면 MP3를 저장합니다. 동의된 상담은 지자체 수신함으로 전달되며, 직원 확인 후 병원 수신함에 인계됩니다.
5. 별도로 작성된 의료정보 후보는 관리자 검토를 거쳐야 환자 기록에 반영됩니다. 모델 답변은 자동으로 확정 환자 기록이 되지 않습니다.

새 `municipality-v3` 통합 동의는 상담·위치 동의를 한 번에 기록하며, 이전 `municipality-v2` 통화의 위치 동의는 기존 별도 기록을 따릅니다. 위치 또는 정보 제공 철회 시 새로운 처리와 미전송 대기 항목을 중단합니다. 이미 지자체·병원에 전달됐거나 동시에 전송 중인 정보는 자동으로 회수되지 않습니다. 브라우저에서는 재생 중 Azure의 중간 인식 결과로 안내를 중단하고 최종 인식으로 대화를 이어갈 수 있습니다. 마이크 에코 제거가 지원되지 않거나 실제 재생 문구가 확인되지 않으면 일반 끼어들기는 제한됩니다. 자세한 화면·서버 실행 방법은 [프로젝트 실행 안내](../RUN_INTEGRATION.md)를 참고하세요.

브라우저 마이크는 지원 시 시스템 출력 전체에 대한 `echoCancellation: "all"`을 먼저 요청하고, 지원하지 않으면 `true`로 전환합니다. 이 설정이 적용된다고 실제 스피커 음성과 사용자 음성이 완전히 분리되는 것은 아닙니다. 시험 기기에서는 스피커 신호가 마이크에 들어오는 경로를 확인하지 못해 실제 에코 억제 효과를 측정하지 못했습니다. 물리 기기와 실제 전화 회선에서 동시 발화·응급 표현의 누락 및 오탐에 대한 추가 검증이 필요합니다.

병원 추천은 진료과가 지정되면 등록 일치·미확인·불일치 순으로 그룹을 나누고 각 그룹에서 반올림 전 직선거리가 가까운 순서로 정렬합니다. 같은 거리에서 전문의 등록과 요일별 등록 진료시간을 보조 기준으로 사용합니다. 지원되는 과목 코드는 심평원 조회에 적용해 2km부터 최대 30km까지 확대하며, 해당 과목 후보가 없으면 일반 주변 목록으로 전환해 알립니다. 반경 내 가까운 최대 30개 기관만 상세 평가하므로 결과 바깥에 더 적합한 기관이 없다는 뜻은 아닙니다. 공공데이터 미확인은 해당 과목이나 전문의가 없다는 뜻이 아니며 실제 진료 여부는 병원에 확인해야 합니다.

참고자료가 부족하거나 모델이 실패한 경우에는 진단을 추측하지 않고 신분증과 복용 중인 약 또는 약봉투 등 병원 방문 준비를 짧게 안내하고 병원 찾기를 제안합니다. `degraded` 상태와 해당 오류 코드는 유지하며, 병원을 찾겠다는 후속 응답은 같은 통화 문맥으로 처리합니다. 이는 응급 여부 판정이나 의료진 진료를 대신하지 않습니다.

SQLite 검색은 한국어 문자열의 일부 일치에 기반한 개발용 검색입니다. 벡터 검색이나 의미 검색은 제공하지 않으며, 실제 환자 DB와 병원 진료 사유 DB에 연결하려면 원본 스키마에 맞춘 매핑과 검색 품질 검증이 필요합니다. 샘플 데이터는 가상 데이터입니다.

## 로컬 실행: PowerShell

이 디렉터리에서 실행합니다. `uv`와 Python 3.11이 필요합니다. `backend/.env`를 열어 `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`에 실제 값을 입력하고 저장합니다. Voice ID는 ElevenLabs에서 선택한 음성의 ID입니다.

```powershell
uv sync --python 3.11
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

서버는 실행 위치와 관계없이 `backend/.env`를 자동으로 읽습니다. 이미 프로세스에 설정된 환경변수가 있다면 `.env`보다 우선합니다. 설정을 바꾼 뒤에는 서버를 다시 시작하세요. 제공된 `auto` 설정에서는 Gemini 키가 있으면 Gemini를, ElevenLabs 키와 Voice ID가 모두 있으면 ElevenLabs를 사용합니다. 키를 입력하지 않으면 모의 공급업체를 사용하며, ElevenLabs 키와 Voice ID 중 하나만 입력하면 설정 오류가 발생합니다. 키가 있어도 모의 실험을 하려면 `.env`에서 해당 공급업체를 `mock`으로 지정하세요.

`.env`의 `EXAMPLE_HOSPITAL_SERVICE_TOKEN`, `EXAMPLE_HOSPITAL_ADMIN_TOKEN`, `EXAMPLE_HOSPITAL_DEV_VERIFICATION_TOKEN`는 **로컬 테스트 전용**입니다. 서비스와 관리자 토큰은 서로 달라야 하며, 미설정 시 보호된 API는 접근할 수 없습니다. 개발용 본인 확인 토큰과 샘플 데이터는 `HOSPITAL_ENV=development`일 때만 허용됩니다. DB 파일에는 대화와 전화번호가 평문으로 저장됩니다. 실제 환자 데이터를 테스트 DB에 넣지 마세요. `.env`는 Git에서 제외되며 키를 대화나 공개 저장소에 붙여 넣지 마세요.

API 문서는 실행 후 `http://127.0.0.1:8000/docs`에서 확인할 수 있습니다. 다음 명령은 별도의 PowerShell 창에서 실행합니다.

```powershell
$api = 'http://127.0.0.1:8000'
$serviceHeaders = @{ Authorization = 'Bearer EXAMPLE_HOSPITAL_SERVICE_TOKEN' }
$adminHeaders = @{ Authorization = 'Bearer EXAMPLE_HOSPITAL_ADMIN_TOKEN' }

# 가상 환자 세션 생성. 응답의 authenticated가 true인지 확인합니다.
$sessionBody = @{
    phone = '+821000000001'
    verification_token = 'EXAMPLE_HOSPITAL_DEV_VERIFICATION_TOKEN'
} | ConvertTo-Json
$session = Invoke-RestMethod -Method Post -Uri "$api/sessions" `
    -Headers $serviceHeaders -ContentType 'application/json; charset=utf-8' -Body $sessionBody
$session

# 같은 request_id와 같은 텍스트를 재전송해도 중복 상담이 생기지 않습니다.
# 같은 request_id를 다른 텍스트로 사용하면 409 응답을 받습니다.
$turnBody = @{
    request_id = 'local-test-1'
    text = '기침이 있어요'
} | ConvertTo-Json
$turn = Invoke-RestMethod -Method Post -Uri "$api/sessions/$($session.id)/turns" `
    -Headers $serviceHeaders -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($turnBody))
$turn

# 비동기 처리 상태, 초기 안내, 최종 답변과 근거를 조회합니다.
Invoke-RestMethod -Uri "$api/sessions/$($session.id)/turns/local-test-1" -Headers $serviceHeaders
Invoke-RestMethod -Uri "$api/sessions/$($session.id)/history" -Headers $serviceHeaders

# 확인이 필요한 내용을 별도 후보로 저장하고 관리자가 검토합니다.
$candidateBody = @{ turn_id = $turn.id; text = '기침 증상을 호소함' } | ConvertTo-Json
$candidate = Invoke-RestMethod -Method Post -Uri "$api/sessions/$($session.id)/candidates" `
    -Headers $serviceHeaders -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($candidateBody))
Invoke-RestMethod -Uri "$api/sessions/$($session.id)/candidates" -Headers $serviceHeaders
$reviewBody = @{ decision = 'approved'; reviewer = 'local-reviewer' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$api/candidates/$($candidate.id)/review" `
    -Headers $adminHeaders -ContentType 'application/json; charset=utf-8' -Body $reviewBody
```

저장된 단계 상태에는 `pending`, `processing`, `completed`, `degraded`, `emergency`, `interrupted`, `cancelled`가 있습니다. `initial_text`와 `final_text`가 단계별로 채워지고, `sources_json`, `timings_json`, `tts_json`은 JSON 문자열입니다. 테스트용 TTS의 `simulated`는 모의 처리, ElevenLabs의 `synthesized`는 오디오 생성과 저장을 뜻하며 둘 다 실제 재생 완료를 의미하지 않습니다. 완료 전에 조회하면 후속 조회가 필요합니다.

`timings_json`의 `initial_ms`와 `final_ms`는 각각 안내 텍스트와 최종 답변 텍스트가 준비된 시각입니다. `low_audio_ready_ms`와 `final_audio_ready_ms`는 실제 음성 합성과 SQLite 저장이 완료된 시각입니다. 모든 값은 해당 상담 처리 시작부터 지난 밀리초이며, 재생 시작이나 재생 완료 시각은 아닙니다. 음성이 생략되었거나 실패한 경우, 또는 모의 TTS를 사용하는 경우에는 해당 음성 준비 시간 항목이 없습니다.

ElevenLabs를 설정한 경우 각 단계의 MP3가 준비된 뒤 인증된 서비스에서 가져올 수 있습니다. 경로의 마지막 부분은 `low`, `final`, `emergency` 중 하나입니다. 최종 응답이 먼저 준비되면 저지연 안내가 생략될 수 있습니다. 오디오가 아직 없거나 해당 단계가 생략된 경우 404가 반환되므로 상담 상태와 `tts_json`을 확인하세요.

```powershell
# 가상 데이터 예시. 준비된 최종 오디오를 로컬 MP3로 내려받습니다.
Invoke-WebRequest -Uri "$api/sessions/$($session.id)/turns/local-test-1/audio/final" `
    -Headers $serviceHeaders -OutFile 'demo-final.mp3'
```

이 경로는 전체 MP3 생성과 저장이 끝난 후 파일을 반환합니다. 브라우저는 음성을 순차 재생하고, 중간 인식이 사용자 발화로 판단되면 재생을 중단할 수 있습니다. 전화 회선 전달이나 스트리밍 합성은 구현하지 않았습니다. 중단된 상담은 사용자 발화와 중단 상태를 기록하고 재생되지 않은 답변은 이후 문맥에 넣지 않습니다. 최종 오디오의 브라우저 `ended` 이벤트에 따른 재생 완료는 실제 청취 또는 이해를 뜻하지 않습니다. 이미 전송된 정보는 자동 회수되지 않습니다. 생성 오디오는 개발용 SQLite에 암호화되지 않은 BLOB로 저장되며 다운로드 파일도 별도 보호되지 않습니다. 실제 환자 음성이나 의료정보를 개발 DB에 저장하지 마세요.

## 모델과 외부 서비스 연결

저지연 모델과 고추론 모델은 각각 설정합니다. `.env.example`은 Gemini 키가 입력되면 저지연 Gemini를 사용하고, 고추론 모델은 모의 구현을 사용합니다. 고추론 모델에 Gemini 3.6 Flash를 사용하려면 `backend/.env`에 다음과 같이 설정합니다.

```dotenv
HOSPITAL_HIGH_PROVIDER=gemini
GEMINI_HIGH_MODEL=gemini-3.6-flash
GEMINI_HIGH_REASONING_EFFORT=high
HOSPITAL_ALLOW_MEDICAL_DATA=true
```

고추론 Gemini는 저지연 모델과 같은 `GEMINI_API_KEY`를 사용합니다. `GEMINI_HIGH_REASONING_EFFORT`는 `high`, `medium`, `low` 중 선택할 수 있으며 생략하면 `high`입니다. 다른 수준을 선택할 때는 지연과 답변 내용을 함께 검증해야 합니다. 고추론 Gemini 요청의 응답 읽기 제한은 120초, 전체 추론 제한은 130초입니다. 계정의 모델 접근 권한과 실제 지연은 호출로 확인해야 합니다. GPT-6 Luna를 포함한 OpenAI 호환 공급업체를 사용하려면 대신 다음 값을 설정합니다.

```dotenv
HOSPITAL_HIGH_PROVIDER=openai-compatible
HOSPITAL_HIGH_BASE_URL=https://api.openai.com/v1/
HOSPITAL_HIGH_API_KEY=<고추론 모델 API 키>
HOSPITAL_HIGH_MODEL=gpt-6-luna
HOSPITAL_ALLOW_MEDICAL_DATA=true
```

환경변수 설정 후 서버를 다시 시작합니다. `GEMINI_MODEL` 기본값은 `gemini-3.5-flash-lite`이며, Google의 `https://generativelanguage.googleapis.com/v1beta/openai/` 호환 엔드포인트를 사용합니다. 동의 후 저지연 Gemini의 의도 분류에는 현재 발화와 같은 통화의 최근 대화 문맥이 전송되며, 건강 정보가 포함될 수 있습니다. 대기 음성 생성은 별도로 짧은 안내 문구 중 하나를 선택합니다. 고추론 제공업체에도 질문과 검색된 자료가 전송될 수 있습니다. 외부 의료정보 전송은 `HOSPITAL_ALLOW_MEDICAL_DATA=true`일 때만 허용됩니다. 전송 권한과 제공업체의 데이터 처리 조건을 확인하고 가상 자료만 사용하세요. 실제 API 호출에 비용이 발생할 수 있으며 키를 소스 코드나 Git에 저장하지 마세요. Gemini 모델 ID는 [Google 공식 모델 목록](https://ai.google.dev/gemini-api/docs/models?hl=ko), 호환 API 사용법은 [Google 공식 문서](https://ai.google.dev/gemini-api/docs/openai)를 참조하세요.

실제 모델에 대한 접근 권한과 의료정보 전송 허용 여부, 응답 품질을 별도로 확인해야 합니다. API 키가 없는 모의 테스트는 실제 모델 호출이 아닙니다. Azure Speech 키와 지역이 설정되면 브라우저 음성 인식에 사용되지만, 실제 전화망 연결은 별도 작업입니다.

가상 기침 질문을 대상으로 실시한 단일 테스트에서 3.6 Flash `high`가 3.8 Flash `high`보다 빨랐습니다. 단일 측정은 일반적인 지연이나 의료 답변의 동등한 품질을 보장하지 않습니다. 실제 환자에게 사용하기 전에 다양한 임상 시나리오, 오류 상황 및 의료진 검토를 거쳐야 합니다.

## ElevenLabs 음성 설정

ElevenLabs API 키와 사용할 음성의 Voice ID를 `backend/.env`에 입력합니다. 관련 설정은 다음과 같습니다.

```dotenv
HOSPITAL_TTS_PROVIDER=auto
ELEVENLABS_API_KEY=<ElevenLabs API 키>
ELEVENLABS_VOICE_ID=<사용할 Voice ID>
ELEVENLABS_MODEL_ID=eleven_flash_v2_5
HOSPITAL_ALLOW_TTS_MEDICAL_DATA=true
```

`uv sync`로 HTTP/2 의존성을 설치하고 환경변수 설정 후 서버를 다시 시작합니다. 합성 요청에는 발화할 텍스트가 ElevenLabs로 전송됩니다. 의료정보가 포함될 수 있으므로 전송 권한과 제공업체의 데이터 처리 조건을 확인해야 합니다. `HOSPITAL_ALLOW_TTS_MEDICAL_DATA=true`는 이 전송을 명시적으로 허용하는 설정이며, 설정하지 않으면 ElevenLabs 연결을 시작하지 않습니다. `ELEVENLABS_MODEL_ID`로 다른 모델을 선택할 수 있지만 계정 접근, 한국어 품질, 지연과 모델별 기능은 실제 호출로 확인해야 합니다. 키를 소스 코드나 Git에 저장하지 마세요.

설정된 저지연 모델과 TTS를 각각 시험하려면 다음 명령을 실행합니다. 실제 공급업체를 선택한 경우 **API 호출과 비용이 발생할 수 있습니다.** 고정된 가상 문구만 사용하며 생성 음성은 저장하거나 재생하지 않습니다. 모의 공급업체가 설정되어 있으면 실제 외부 연결을 검증하지 않습니다.

```powershell
uv run python -m app.diagnostics
```

### Auto-tag와 Audio Tags

ElevenLabs의 Audio Tags는 지원 모델(`eleven_v3`, `eleven_v4`, `eleven_v4_turbo`)에 `[calmly]`처럼 태그가 포함된 텍스트를 전달해 사용할 수 있습니다. 기본 `eleven_flash_v2_5`에서 이 기능이 지원된다는 근거는 확인되지 않았습니다. Studio 화면의 **Auto-tag가 자동으로 태그를 삽입하는 기능**을 직접 실행할 공개 API 파라미터는 공식 API 명세에서 확인되지 않았습니다. 따라서 자동 태그 삽입은 이 백엔드에 구현되어 있지 않으며, Audio Tags를 사용하려면 지원 모델에 태그를 넣은 텍스트를 별도로 준비해야 합니다. 자세한 내용은 [ElevenLabs Audio Tags 안내](https://elevenlabs.io/docs/help-center/product/core-capabilities/text-to-speech/how-do-audio-tags-work-with-eleven-v3-and-v4)와 [음성 생성 API 명세](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)를 확인하세요.

ElevenLabs의 `enable_logging=false` 무보존 모드는 공식 문서상 Enterprise 고객에게 제공됩니다. 이 프로토타입은 해당 옵션을 설정하지 않으며, 외부 제공업체의 실제 데이터 보존 정책을 확인해야 합니다.

## 테스트

```powershell
uv run python -m pytest -q
```

테스트는 임시 SQLite 파일과 모의 모델·음성 어댑터를 사용합니다. 실제 의료 데이터 품질이나 모델의 의료적 안전성을 입증하지는 않습니다.

## 운영 전 필요한 작업

- 실제 환자 본인 확인 연동과 사용자·관리자별 인증, 권한, 접근 이력 및 세션 수명 관리가 필요합니다. 현재 개발 토큰은 안전한 본인 확인 방식이 아니며 운영 환경에는 실제 인증 어댑터가 없습니다. 전화번호가 여러 환자에 연결되거나 확인되지 않으면 환자별 자료를 조회하지 않습니다.
- 실제 SQLite 스키마 연결, 데이터 정합성·최신성 확인, 검색 품질과 환자별 격리 검증이 필요합니다. 출처 ID가 존재한다는 것만으로 답변의 의학적 정확성이 보장되지는 않습니다.
- 현재 응급 감지는 일부 문구에만 반응하는 단순 규칙입니다. 모든 응급 상황을 탐지하지 못하며 의료적 분류 시스템이 아닙니다. 의료진 검토와 별도의 안전 설계가 필요합니다.
- 실제 전화망에 맞는 스트리밍, 재생 상태 전달과 중단·실패 처리가 필요합니다. 현재 브라우저 재생의 에코 제거는 완전하지 않아 안내와 겹친 말이 누락되거나 긴급 발화를 오인할 수 있으며, 물리 스피커·마이크 조합과 실제 전화 회선에서는 검증되지 않았습니다. ElevenLabs 어댑터는 MP3 합성을 수행하며 모의 TTS는 오디오를 만들지 않습니다.
- 현재 백그라운드 처리는 서버 프로세스에 묶여 있어 종료·재시작 시 미완료 작업이 자동 복구되지 않습니다. 영속 작업 큐, 재시도 및 동시성 정책을 도입해야 합니다.
- SQLite와 백업, 네트워크, 외부 모델 전송에 대한 암호화·접근 통제·보존 및 삭제 정책을 구현하고, 적용되는 개인정보 및 의료정보 요건을 검토해야 합니다. 관리자는 현재 공유 토큰으로 구분하며, 개별 의료진 신원 확인은 구현되지 않았습니다.
