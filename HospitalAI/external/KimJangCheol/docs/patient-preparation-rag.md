# 환자 차트 × 방문 준비 RAG

기존 원문 73개를 보존하고 서비스 작성 방문 준비 문서 27개(`guide_7`~`guide_33`)를 추가하여 100개로 확장했습니다. 환자 차트는 변경하지 않고 명시적 patient_id로 통화와 연결합니다.

## 오른쪽 대시보드

통화를 선택하면 ‘방문 준비’ 탭이 먼저 열립니다. 차트에 기록된 지병, 현재 약의 개수, 과거 처방, 이동·청취·동행·휴대전화 정보, 통화 목적 및 어르신의 확정된 발화를 참고합니다. AI 응답이나 생성된 요약을 환자의 사실로 간주하지 않습니다.

각 카드는 쉬운 준비 문장, 선택 이유, 병원에 확인할 질문, 준비 문서 ID와 관련 원문 ID를 표시합니다. ‘피검사 하러 가요’처럼 표현을 추가하면 해당 통화에 대한 검색만 다시 실행합니다. 환자 지병이나 약 기록을 갱신하지 않습니다.

환자 차트·AI 요약·대화·과거 기록·직원 메모·연락처 기능은 유지합니다. 응답은 통화 ID·버전·환자 차트 가져오기 ID·준비 문서 버전·추가 검색어별로 구분하여, 늦게 온 결과가 다른 환자의 카드에 표시되지 않도록 합니다.

## 실제 연결 방식

- 기존 73개: 검토 대기 원문으로 보존, 의료 지시 생성에 사용하지 않음.
- 추가 27개: 자료 챙기기, 기록 확인, 이동·접수·설명 방식과 병원에 물을 질문으로 구성.
- `rag_preparation_policies`: 문서와 일치하는 준비 문장·질문·선택 조건·원문 참조를 보관.
- `rag_vocabulary`: 전문·진료 용어와 일상 표현 27쌍을 보관.
- 환자 차트의 구조화된 필드와 조건을 비교하고, 발화는 표현 사전으로 확장해 문서를 검색.
- 검색된 서비스 작성 문서의 정해진 준비 문장만 조합. 카드 점수는 규칙 우선순위이며 정확도·위험도·확률이 아님.

이 구현은 어휘 검색과 제한된 템플릿 조합입니다. 임베딩·벡터 검색·Bedrock·LLM 생성은 사용하지 않습니다. raw `/knowledge/retrieve`는 여전히 승인·출처 조건을 적용하며, 방문 준비 카드 전용 경로와 구분됩니다.

원문 의료정보가 승인되었다고 표시하지 않습니다. 금식 시간, 약 복용·중단, 질환 진단·응급 처치 지시를 새로 생성하지 않습니다. 검사 준비는 해당 병원에 확인할 질문으로 표현합니다. 가상 약 이름으로 성분이나 상호작용을 추정하지 않습니다.

## 단어 수준 비교

1은 일상 표현, 2는 진료·행정 용어, 3은 전문 용어로 정의했습니다. 서비스 내부의 수기 기준이며 학술적으로 검증된 문해력·독해 등급이 아닙니다.

예: 채혈(2) → ‘검사를 위해 피를 뽑는 일’(1), 재진(2) → ‘다시 받는 진료’(1). 입력의 ‘피검사’는 사전의 채혈 항목으로 연결됩니다.

정확 일치·별칭·철자 유사 후보를 구분합니다. 철자 유사 비교는 정규화 후 4자 이상, 문자 비율 0.84 이상으로 제한합니다. 철자 후보는 표시만 하고 검사 조건 확장에는 사용하지 않습니다. 증상 표현과 질환명이 연결되어 검색되더라도 새 진단으로 차트에 추가하지 않습니다.

## API

| 경로 | 기능 |
|---|---|
| GET /api/team/calls/{call_id}/preparation | 환자별 준비 카드·근거·단어 비교 |
| GET /api/team/calls/{call_id}/preparation?q=피검사 | 표현을 추가하여 재검색 |
| GET /api/team/knowledge/word-levels | 전체 표현 사전 |
| GET /api/team/knowledge/word-levels?q=피검사 | 해당 표현의 용어·수준 비교 |
| GET /api/team/knowledge/stats | 원문 73 + 추가 27, 준비 정책·표현 사전 개수 |

## 재생성 및 검증

```sh
backend/.venv/bin/python -m backend.generate_preparation_knowledge
backend/.venv/bin/python -m backend.export_preparation_fixtures
node scripts/build.mjs
backend/.venv/bin/python -m unittest backend.test_preparation backend.test_knowledge backend.test_app backend.test_chart_format -v
```

추가 27개만 반영하려면 `backend.import_knowledge backend/fixtures/rag/visit-preparation-extra.txt --source-kind service_authored`를 사용합니다. 서버 시작 시 추가 문서와 정책·표현 사전도 확인합니다. 기존 환자·통화 테이블을 초기화하지 않습니다.

독립 HTML은 같은 백엔드 코드에서 계산한 시연용 준비 카드를 포함합니다. 추가 표현으로 실시간 재검색하는 기능은 로컬 API 연결 버전에서 제공합니다. 브라우저 localStorage와 SQLite는 구분합니다.
