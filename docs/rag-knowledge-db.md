# RAG 원문 DB 반영

사용자가 제공한 `medical_knowledge_docs` 73개 항목을 원문 그대로 보관했습니다. 농어촌 응급·외상 26개, 외래·방문 준비 27개, 중증·감염·특화 14개, 의료 이용 안내 6개입니다.

원본 파일은 `backend/fixtures/rag/medical-knowledge-source.txt`입니다. Python 코드로 실행하지 않고 AST와 literal_eval로 리터럴 목록만 파싱합니다. 중복 ID, 필드 누락, 알 수 없는 그룹을 검증합니다.

## 테이블

- `rag_imports`: 원본 파일 전체·SHA-256·가져온 시각·문서 수
- `rag_documents`: 현재 문서 ID·그룹·분류·본문·관련단어·출처·검토 상태
- `rag_document_versions`: 가져오기별 문서 스냅샷
- `rag_chunks`: 문서별 청크 1개. 짧은 원문 전체를 보존하며 임베딩은 미생성 상태

기존 환자·통화 테이블과 분리했습니다. 환자 지병·약·진단을 RAG 문서에서 추정하거나 갱신하지 않습니다. 이번 반영 전 기존 13개 테이블을 해시로 비교해 변경되지 않았음을 확인했습니다.

원본에서 관련단어 부분만 검색용 keywords로 별도 추출합니다. 해결방안·금식·복약·응급 지시는 수정하거나 검증된 사실로 승격하지 않았습니다. 원문이 바뀌면 이전 버전을 보존하고 검토 상태를 대기로 되돌립니다. 같은 파일을 다시 가져오면 중복 생성하지 않습니다.

현재 전체 문서는 `source_kind=user_supplied`, `source_url=null`, `review_status=pending_review`, `verified_at=null`, `retrieval_enabled=false`입니다. 이 상태는 DB 반영 실패를 뜻하지 않습니다. 검토용 조회와 실제 답변 근거 사용을 분리한 것입니다. 승인 API·자동 진단·응급 판단·LLM 답변·임베딩 검색은 구현하지 않았습니다.

## 조회 API

| 경로 | 내용 |
|---|---|
| GET /api/team/knowledge/stats | 문서·그룹·청크·검토 상태 건수 |
| GET /api/team/knowledge/documents?q=당뇨 | 원문 검토용 부분 문자열 검색. 미검토 원문도 반환 |
| GET /api/team/knowledge/documents?group=guide | 그룹 필터 |
| GET /api/team/knowledge/documents/freq_3 | 원문 한 건과 검토 메타데이터 |
| GET /api/team/knowledge/retrieve?q=당뇨 | 승인·출처·검색 활성화 조건을 충족한 문서만 반환 |

검토용 검색은 벡터 검색이 아닌 단순 문자열 검색입니다. retrieve는 현재 승인된 문서가 없어 빈 결과를 반환합니다. RAG 답변 생성은 연결되지 않았습니다.

## 가져오기

```sh
backend/.venv/bin/python -m backend.import_knowledge --validate-only
backend/.venv/bin/python -m backend.import_knowledge
```

CLI는 SQLite 백업 후 트랜잭션으로 반영합니다. 서버 시작 시에도 같은 원본을 확인하되 이미 반영한 파일은 다시 쓰지 않습니다. 누락된 기존 문서를 자동 삭제하지 않습니다.

검증: `backend/.venv/bin/python -m unittest backend.test_knowledge backend.test_app backend.test_chart_format -v`
