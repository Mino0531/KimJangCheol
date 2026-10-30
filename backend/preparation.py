"""Patient-specific lexical retrieval + constrained visit-preparation templates.

No diagnostic inference, medication instructions, embedding model, or generative LLM.
"""
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import HTTPException, Query
from sqlalchemy import Column, JSON
from sqlmodel import Field, Session, SQLModel, select
from backend.knowledge import KnowledgeDocument

FIXTURES = Path(__file__).parent / 'fixtures/rag'


class PreparationPolicy(SQLModel, table=True):
    __tablename__ = 'rag_preparation_policies'
    document_id: str = Field(primary_key=True, foreign_key='rag_documents.id')
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class VocabularyTerm(SQLModel, table=True):
    __tablename__ = 'rag_vocabulary'
    canonical: str = Field(primary_key=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


def seed_preparation_policies(engine):
    policies = json.loads((FIXTURES / 'preparation-policies.json').read_text())['policies']
    terms = json.loads((FIXTURES / 'vocabulary-levels.json').read_text())['terms']
    with Session(engine) as session:
        for policy in policies:
            doc = session.get(KnowledgeDocument, policy['document_id'])
            if not doc or doc.source_kind != 'service_authored':
                raise ValueError('방문 준비 정책의 서비스 작성 원문이 없습니다.')
            # Projections must correspond exactly to the stored document; never substitute raw clinical prose.
            if policy['easy_text'] not in doc.content or policy['staff_question'] not in doc.content:
                raise ValueError('방문 준비 정책과 DB 원문이 일치하지 않습니다.')
            row = session.get(PreparationPolicy, policy['document_id'])
            if row:
                row.data = policy
            else:
                row = PreparationPolicy(document_id=policy['document_id'], data=policy)
            session.add(row)
        for term in terms:
            row = session.get(VocabularyTerm, term['canonical'])
            if row:
                row.data = term
            else:
                row = VocabularyTerm(canonical=term['canonical'], data=term)
            session.add(row)
        session.commit()


def normalize(text):
    return re.sub(r'[^가-힣a-z0-9]', '', text.lower())


def compare_words(text, vocabulary):
    normalized = normalize(text)
    tokens = [normalize(t) for t in re.split(r'[\s,.;!?/]+', text) if normalize(t)]
    matches = []
    for term in vocabulary:
        match = None
        for expression in [term['canonical'], *term['aliases']]:
            target = normalize(expression)
            if target and target in normalized:
                match = {'input_expression': expression, 'match_type': 'canonical' if expression == term['canonical'] else 'alias', 'lexical_similarity': 1.0}
                break
        if not match:
            # Character similarity is only a lexical suggestion. Short ambiguous tokens are excluded.
            for expression in [term['canonical'], *term['aliases']]:
                target = normalize(expression)
                candidates = [(token, SequenceMatcher(None, token, target).ratio()) for token in tokens if len(token) >= 4 and len(target) >= 4]
                if candidates:
                    token, ratio = max(candidates, key=lambda pair: pair[1])
                    if ratio >= .84:
                        match = {'input_expression': token, 'match_type': 'spelling_similarity', 'lexical_similarity': round(ratio, 3)}
                        break
        if match:
            matches.append({**match, **term, 'interpretation': '검색 표현 연결이며 환자 진단이 아님'})
    return matches


def _reason(triggers, patient, call, explicit_text):
    conditions = patient.get('conditions', []) if patient else []
    meds = patient.get('current_medications', []) if patient else []
    prep = patient.get('visit_preparation', {}) if patient else {}
    reasons = []
    weight = 0
    if triggers.get('always'):
        reasons.append('모든 방문의 기본 준비')
        weight += 1
    if 'conditions' in triggers:
        names = [c['name'] for c in conditions if c.get('status', 'active') == 'active' and any(normalize(t) in normalize(c['name']) for t in triggers['conditions'])]
        if not names:
            return None
        reasons.append('기록된 지병: ' + ', '.join(names))
        weight += 9
    if 'medications_min' in triggers:
        if not patient or len(meds) < triggers['medications_min']:
            return None
        reasons.append(f'현재 복용약 {len(meds)}종이 차트에 기록됨')
        weight += 8 if triggers['medications_min'] >= 3 else 6
    if triggers.get('medications_unknown'):
        if not patient or patient.get('knowledge_status', {}).get('medications') != 'unknown':
            return None
        reasons.append('복용약 정보 미확인')
        weight += 7
    if 'allergy_status' in triggers:
        if not patient or patient.get('allergies', {}).get('knowledge_status') != triggers['allergy_status']:
            return None
        reasons.append('알레르기 정보 ' + ('기록 있음' if triggers['allergy_status'] == 'recorded' else '미확인'))
        weight += 8 if triggers['allergy_status'] == 'recorded' else 3
    for key, accepted in triggers.get('preparation', {}).items():
        if not patient or prep.get(key) not in accepted:
            return None
        reasons.append({'mobility': '차트에 이동 지원 정보가 있음', 'hearing': '천천히 설명 요청', 'phone_type': '폴더폰 이용 기록', 'companion': '동행 계획 없음'}[key])
        weight += 7
    if 'visit_types' in triggers:
        if call.get('visit_type') not in triggers['visit_types']:
            return None
        reasons.append('방문 구분: ' + call['visit_type'])
        weight += 4
    if 'prescriptions_min' in triggers:
        if not patient or len(patient.get('prescriptions', [])) < triggers['prescriptions_min']:
            return None
        reasons.append('이전 처방 이력이 기록됨')
        weight += 3
    if triggers.get('has_visit_plan'):
        if not call.get('planned_visit_text'):
            return None
        reasons.append('사용자가 전달한 방문 계획이 있음')
        weight += 2
    if 'query_terms' in triggers:
        matches = [t for t in triggers['query_terms'] if normalize(t) in normalize(explicit_text)]
        if not matches:
            return None
        reasons.append('방문 목적·발화에서 찾은 표현: ' + ', '.join(matches))
        weight += 10
    return {'reasons': reasons, 'priority_score': weight}


def build_preparation(session, patient, call, messages, query=''):
    vocabulary = [r.data for r in session.exec(select(VocabularyTerm).order_by(VocabularyTerm.canonical)).all()]
    explicit_text = ' '.join([call.get('purpose', ''), query] + [m['text'] for m in messages if m.get('speaker') == 'caller'])
    chart_terms = ' '.join(c['name'] for c in (patient or {}).get('conditions', []) if c.get('status', 'active') == 'active')
    word_comparison = compare_words(' '.join([explicit_text, chart_terms]), vocabulary)
    # Alias expansion only contributes to searching stated topics; chart conditions alone never imply an examination.
    query_matches = compare_words(explicit_text, vocabulary)
    expanded_query = explicit_text + ' ' + ' '.join(m['canonical'] for m in query_matches if m['match_type'] != 'spelling_similarity')
    docs = {d.id: d for d in session.exec(select(KnowledgeDocument)).all()}
    cards = []
    for policy_row in session.exec(select(PreparationPolicy)).all():
        p = policy_row.data
        doc = docs.get(policy_row.document_id)
        if not doc or doc.source_kind != 'service_authored' or p.get('scope') != 'visit_preparation_only':
            continue
        if p['easy_text'] not in doc.content or p['staff_question'] not in doc.content:
            continue
        reason = _reason(p['triggers'], patient, call, expanded_query)
        if not reason:
            continue
        matched_terms = [t for t in p['terms'] if normalize(t) in normalize(expanded_query + ' ' + chart_terms)]
        raw_references = [{'document_id': related, 'category': docs[related].category,
                           'review_status': docs[related].review_status, 'used_for_action_generation': False}
                          for related in p['related_original_ids'] if related in docs]
        cards.append({'document_id': doc.id, 'title': doc.category, 'easy_text': p['easy_text'], 'staff_question': p['staff_question'],
                      **reason, 'matched_terms': matched_terms, 'language_level': p['language_level'],
                      'source_kind': doc.source_kind, 'review_status': doc.review_status,
                      'scope': p['scope'], 'reference_documents': raw_references})
    cards.sort(key=lambda c: (-c['priority_score'], c['document_id']))
    return {'call_id': call['id'], 'patient_id': patient['id'] if patient else None, 'method': 'lexical_alias_retrieval_and_constrained_templates',
            'word_level_method': 'service_authored_heuristic', 'word_level_scale': {'1': '일상 표현', '2': '진료·행정 용어', '3': '전문 용어'},
            'query': query, 'cards': cards[:10], 'matched_document_count': len(cards),
            'word_comparison': word_comparison[:16], 'knowledge_document_count': len(docs),
            'clinical_instructions_generated': False, 'embedding_used': False, 'llm_used': False,
            'notice': '방문 준비 참고용 초안입니다. 미검토 의료 원문은 행동 지시 생성에 사용하지 않습니다.'}


def register_preparation_routes(app, engine, Call, Message, Patient, call_dict, patient_dict):
    @app.get('/api/team/calls/{call_id}/preparation')
    def preparation(call_id: str, q: str = Query('', max_length=500)):
        with Session(engine) as session:
            call = session.get(Call, call_id)
            if not call:
                raise HTTPException(404, '통화가 없습니다.')
            row = session.get(Patient, call.patient_id) if call.patient_id else None
            patient = patient_dict(session, row) if row else None
            messages = [m.data for m in session.exec(select(Message).where(Message.call_id == call_id).order_by(Message.sequence)).all()]
            return build_preparation(session, patient, call_dict(call), messages, q)

    @app.get('/api/team/knowledge/word-levels')
    def words(q: str = Query('', max_length=500)):
        with Session(engine) as session:
            terms = [v.data for v in session.exec(select(VocabularyTerm).order_by(VocabularyTerm.canonical)).all()]
            return {'method': 'service_authored_heuristic_not_validated_readability_grade',
                    'terms': compare_words(q, terms) if q.strip() else terms,
                    'note': '문자 유사도는 의료적 동등성·진단 확률을 의미하지 않습니다.'}
