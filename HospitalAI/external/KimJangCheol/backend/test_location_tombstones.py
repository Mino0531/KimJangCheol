"""Location withdrawal suppresses both recommendations and derived answers."""

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine
from fastapi.testclient import TestClient
from hashlib import sha256
import json
import sqlite3

from backend.location_tombstones import initialize_location_tombstones, redact_location_event, record_location_revocation
from backend.app import create_app


def test_revocation_masks_old_generations_but_not_new_permission():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    initialize_location_tombstones(engine)
    with Session(engine) as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        record_location_revocation(session, 'hospital-ai', 'call', 1)
        session.commit()
    with Session(engine) as session:
        old = {'source': 'hospital-ai', 'call_id': 'call', 'event_type': 'recommendation',
               'location_generation': 1, 'recommendation': {'hospitals': [{'name': 'Private'}]}}
        assert redact_location_event(session, old)['recommendation'] is None
        new = {**old, 'location_generation': 2}
        assert redact_location_event(session, new)['recommendation'] == new['recommendation']


def test_derived_and_legacy_answers_are_redacted_after_revocation():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    initialize_location_tombstones(engine)
    with Session(engine) as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        record_location_revocation(session, 'hospital-ai', 'call', 2)
        record_location_revocation(session, 'hospital-ai', 'call', 1)
        session.commit()
    with Session(engine) as session:
        base = {'source': 'hospital-ai', 'call_id': 'call', 'event_type': 'turn_completed',
                'user_text': '말씀', 'assistant_text': 'Private', 'sources': []}
        for generations in (None, [1], [1, 3]):
            event = dict(base)
            if generations is not None:
                event['location_generations'] = generations
            result = redact_location_event(session, event)
            assert result['assistant_text'] == ''
            assert result['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
        assert redact_location_event(session, {**base, 'location_generations': [3]})['assistant_text'] == ''
        assert redact_location_event(session, {**base, 'location_generations': []})['assistant_text'] == 'Private'
        unbound = {**base, 'location_generations': [], 'sources': [{'id': 'clinic:recent-recommendation'}]}
        assert redact_location_event(session, unbound)['assistant_text'] == ''
        unbound_reference = {**base, 'location_generations': [], 'recommendation_event_ids': ['event']}
        assert redact_location_event(session, unbound_reference)['assistant_text'] == ''


def test_inbox_masks_late_location_event_and_preserves_new_generation(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'withdrawal.sqlite')
    sender = {'Authorization': 'Bearer test-intake'}
    staff = {'Authorization': 'Bearer test-staff'}
    base = {'source': 'hospital-ai', 'call_id': 'call-1', 'phone': '01000000000',
            'occurred_at': '2026-10-31T00:00:00Z', 'user_text': '', 'assistant_text': '',
            'answer_status': None, 'sources': []}
    revoke = {**base, 'event_type': 'location_revoked', 'event_id': 'location-revoked:call-1:1',
              'location_generation': 1}
    recommendation = {'specialty': None, 'hospitals': [], 'warning': 'Private'}
    with TestClient(app) as client:
        assert client.post('/api/municipality/intake', json=revoke, headers=sender).status_code == 201
        for generation in (1, 2):
            event = {**base, 'event_type': 'recommendation', 'event_id': f'recommendation:{generation}',
                     'location_generation': generation, 'recommendation': recommendation}
            assert client.post('/api/municipality/intake', json=event, headers=sender).status_code == 201
        answer = {**base, 'event_type': 'turn_completed', 'event_id': 'turn:one',
                  'user_text': 'Patient words', 'assistant_text': 'Private', 'answer_status': 'completed',
                  'location_generations': [1], 'recommendation_event_ids': ['recommendation:1']}
        assert client.post('/api/municipality/intake', json=answer, headers=sender).status_code == 201
        assert client.post('/api/municipality/intake', json=answer, headers=sender).status_code == 200
        mismatch = {**answer, 'event_id': 'turn:mismatch', 'location_generations': [2],
                    'recommendation_event_ids': ['recommendation:1']}
        valid = {**answer, 'event_id': 'turn:valid', 'location_generations': [2],
                 'recommendation_event_ids': ['recommendation:2']}
        assert client.post('/api/municipality/intake', json=mismatch, headers=sender).status_code == 201
        assert client.post('/api/municipality/intake', json=valid, headers=sender).status_code == 201
        detail = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        by_id = {event['event_id']: event for event in detail['events']}
        assert by_id['recommendation:1']['recommendation'] is None
        assert by_id['recommendation:2']['recommendation'] == recommendation
        assert by_id['turn:one']['assistant_text'] == ''
        assert by_id['turn:one']['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
        assert by_id['turn:mismatch']['assistant_text'] == ''
        assert by_id['turn:valid']['assistant_text'] == 'Private'
        delayed = {**answer, 'event_id': 'turn:delayed', 'location_generations': [3],
                   'recommendation_event_ids': ['recommendation:3']}
        assert client.post('/api/municipality/intake', json=delayed, headers=sender).status_code == 201
        before = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert next(event for event in before['events'] if event['event_id'] == 'turn:delayed')['assistant_text'] == ''
        incoming = {**base, 'event_type': 'recommendation', 'event_id': 'recommendation:3',
                    'location_generation': 3, 'recommendation': recommendation}
        assert client.post('/api/municipality/intake', json=incoming, headers=sender).status_code == 201
        after = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert next(event for event in after['events'] if event['event_id'] == 'turn:delayed')['assistant_text'] == 'Private'


def test_revocation_after_handoff_hides_answers_in_both_hospital_views(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'handoff-withdrawal.sqlite')
    sender = {'Authorization': 'Bearer test-intake'}
    staff = {'Authorization': 'Bearer test-staff'}
    hospital = {'Authorization': 'Bearer test-hospital'}
    base = {'source': 'hospital-ai', 'call_id': 'call-1', 'phone': '01000000000',
            'occurred_at': '2026-10-31T00:00:00Z', 'user_text': '', 'assistant_text': '',
            'answer_status': None, 'sources': []}
    answer = {**base, 'event_type': 'turn_completed', 'event_id': 'turn:one', 'user_text': 'Patient words',
              'assistant_text': 'Private hospital', 'answer_status': 'completed',
              'location_generations': [1], 'recommendation_event_ids': ['recommendation:1']}
    revoke = {**base, 'event_type': 'location_revoked', 'event_id': 'location-revoked:call-1:1',
              'location_generation': 1}
    with TestClient(app) as client:
        assert client.post('/api/municipality/intake', json=answer, headers=sender).status_code == 201
        response = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=staff)
        assert response.status_code == 200
        version = response.json()['case']['version']
        assert client.post('/api/municipality/inbox/call-1/handoff',
                           json={'expected_version': version}, headers=staff).status_code == 200
        assert client.post('/api/municipality/intake', json=revoke, headers=sender).status_code == 201
        views = [client.get('/api/team/municipality-handoffs', headers=hospital),
                 client.get('/api/team/consultation-results', headers=hospital)]
        for response in views:
            assert response.status_code == 200
            assert 'Private hospital' not in response.text
            assert 'Patient words' not in response.text
            assert '위치 정보가 포함될 수 있어 원문 확인 제한' in response.text


def test_withdrawn_or_unproven_speech_is_hidden_and_fresh_generation_is_visible():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    initialize_location_tombstones(engine)
    with Session(engine) as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        record_location_revocation(session, 'hospital-ai', 'call', 2)
        session.commit()
    base = {'source': 'hospital-ai', 'call_id': 'call', 'event_type': 'caller_utterance',
            'user_text': '화순 동면에서 무릎 아파요', 'assistant_text': ''}
    with Session(engine) as session:
        for generation in (None, 0, 1, 2):
            item = {**base, 'user_location_generation': generation}
            assert redact_location_event(session, item)['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
        fresh = {**base, 'user_location_generation': 3}
        assert redact_location_event(session, fresh)['user_text'] == base['user_text']


def test_late_caller_utterance_redacted_after_revocation_and_new_consent_visible(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'utterance-withdrawal.sqlite')
    sender = {'Authorization': 'Bearer test-intake'}
    staff = {'Authorization': 'Bearer test-staff'}
    base = {'source': 'hospital-ai', 'call_id': 'call-1', 'phone': '01000000000',
            'occurred_at': '2026-10-31T00:00:00Z', 'user_text': '', 'assistant_text': '',
            'answer_status': None, 'sources': []}
    revoke = {**base, 'event_type': 'location_revoked', 'event_id': 'location-revoked:call-1:1',
              'location_generation': 1}
    with TestClient(app) as client:
        assert client.post('/api/municipality/intake', json=revoke, headers=sender).status_code == 201
        for identity, generation, revision in (('old', 1, 1), ('unknown', None, None),
                                               ('no_location', 0, 0), ('no_location_new', 0, 9),
                                               ('new', 2, 3)):
            digest = sha256(f'call-1:{identity}'.encode()).hexdigest()
            utterance = {**base, 'event_type': 'caller_utterance', 'event_id': f'utterance:{digest}',
                         'utterance_id': identity, 'user_text': '화순 동면에서 무릎 아파요',
                         'user_location_generation': generation, 'user_location_revision': revision}
            assert client.post('/api/municipality/intake', json=utterance, headers=sender).status_code == 201
            assert client.post('/api/municipality/intake', json=utterance, headers=sender).status_code == 200
        detail = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        events = {event['utterance_id']: event for event in detail['events'] if event['event_type'] == 'caller_utterance'}
        assert events['old']['user_text'] == events['unknown']['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
        assert events['no_location']['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
        assert events['no_location_new']['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
        assert events['new']['user_text'] == '화순 동면에서 무릎 아파요'
        urgent = {**base, 'event_id': 'urgent:call-1', 'event_type': 'urgent',
                  'user_text': '화순 동면', 'assistant_text': '긴급 안내', 'answer_status': 'emergency',
                  'user_location_generation': 1}
        assert client.post('/api/municipality/intake', json=urgent, headers=sender).status_code == 201
        urgent_detail = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert urgent_detail['urgent'] is True
        urgent_event = next(event for event in urgent_detail['events'] if event['event_id'] == 'urgent:call-1')
        assert urgent_event['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
        with sqlite3.connect(tmp_path / 'utterance-withdrawal.sqlite') as connection:
            stored = [json.loads(row[0]) for row in connection.execute("SELECT data FROM municipal_events WHERE event_type='caller_utterance'")]
        assert next(item for item in stored if item['utterance_id'] == 'old')['user_text'] == '화순 동면에서 무릎 아파요'
