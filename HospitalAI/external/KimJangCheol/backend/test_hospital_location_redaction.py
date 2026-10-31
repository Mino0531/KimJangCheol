"""Hospital reads cannot reveal data derived from withdrawn locations."""

from fastapi.testclient import TestClient
from sqlmodel import Session

from backend.app import create_app
from backend.location_tombstones import initialize_location_tombstones, record_location_revocation
from backend.municipal_intake import MunicipalEvent


def test_withdrawal_redacts_user_location_but_preserves_new_generation(tmp_path, monkeypatch):
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'test.sqlite')
    initialize_location_tombstones(app.state.engine)
    headers = {'Authorization': 'Bearer test-hospital'}
    base = {'source': 'hospital-ai', 'event_id': 'turn:old', 'call_id': 'call',
            'occurred_at': '2026-10-30T06:00:00Z', 'phone': '+821000000001',
            'user_text': 'Old private location', 'assistant_text': 'Normal reply',
            'answer_status': 'completed', 'sources': []}
    new = {**base, 'event_id': 'turn:new', 'user_text': 'New location after consent'}
    with TestClient(app) as client:
        for row in (base, new):
            assert client.post('/api/team/consultation-results', json=row, headers=headers).status_code == 201
        with Session(app.state.engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            for row, generation in ((base, 1), (new, 2)):
                session.add(MunicipalEvent(id=row['event_id'], source=row['source'], event_id=row['event_id'],
                    call_id='call', occurred_at=row['occurred_at'], event_type='turn_completed',
                    data={**row, 'location_generations': [], 'recommendation_event_ids': [],
                          'user_location_generation': generation}))
            record_location_revocation(session, 'hospital-ai', 'call', 1)
            session.commit()
        output = {row['event_id']: row for row in client.get('/api/team/consultation-results', headers=headers).json()}
        assert output['turn:old']['user_text'] != base['user_text']
        assert output['turn:new']['user_text'] == new['user_text']


def test_hospital_read_hides_old_location_but_allows_later_generation(tmp_path, monkeypatch):
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    app = create_app(tmp_path / 'test.sqlite')
    initialize_location_tombstones(app.state.engine)
    headers = {'Authorization': 'Bearer test-hospital'}
    path = '/api/team/consultation-results'
    base = {'source': 'hospital-ai', 'event_id': 'turn:old', 'call_id': 'call',
            'occurred_at': '2026-10-30T06:00:00Z', 'phone': '+821000000001',
            'user_text': 'Caller words', 'assistant_text': 'Secret hospital',
            'answer_status': 'completed', 'sources': []}
    with TestClient(app) as client:
        assert client.post(path, json=base, headers=headers).status_code == 201
        later = {**base, 'event_id': 'turn:new', 'assistant_text': 'New hospital'}
        assert client.post(path, json=later, headers=headers).status_code == 201
        recommendation = {'source': 'hospital-ai', 'event_id': 'recommendation:2', 'call_id': 'call',
            'occurred_at': base['occurred_at'], 'phone': base['phone'], 'event_type': 'recommendation',
            'user_text': '', 'assistant_text': '', 'answer_status': None, 'sources': [],
            'location_generation': 2, 'recommendation': {'specialty': None, 'hospitals': [], 'warning': None}}
        assert client.post('/api/municipality/intake', json=recommendation,
                           headers={'Authorization': 'Bearer test-intake'}).status_code == 201
        with Session(app.state.engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            for item, generation in [(base, 1), (later, 2)]:
                session.add(MunicipalEvent(id=item['event_id'], source='hospital-ai', event_id=item['event_id'],
                    call_id='call', occurred_at=item['occurred_at'], event_type='turn_completed',
                    data={**item, 'event_type': 'turn_completed', 'location_generations': [generation],
                          'recommendation_event_ids': [f'recommendation:{generation}'],
                          'user_location_generation': generation}))
            record_location_revocation(session, 'hospital-ai', 'call', 1)
            session.commit()
        result = {row['event_id']: row for row in client.get(path, headers=headers).json()}
        assert result['turn:old']['assistant_text'] == ''
        assert result['turn:old']['user_text'] != 'Caller words'
        assert result['turn:new']['assistant_text'] == 'New hospital'
        assert result['turn:new']['user_text'] == 'Caller words'
        assert client.post(path, json=base, headers=headers).json()['duplicate']


def test_conflicting_provenance_cannot_unmask_an_existing_record(tmp_path, monkeypatch):
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'test.sqlite')
    initialize_location_tombstones(app.state.engine)
    headers = {'Authorization': 'Bearer test-hospital'}
    row = {'source': 'hospital-ai', 'event_id': 'turn:collision', 'call_id': 'call',
           'occurred_at': '2026-10-30T06:00:00Z', 'phone': '+821000000001',
           'user_text': 'Caller words', 'assistant_text': 'Secret hospital',
           'answer_status': 'completed', 'sources': []}
    with TestClient(app) as client:
        assert client.post('/api/team/consultation-results', json=row, headers=headers).status_code == 201
        with Session(app.state.engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            session.add(MunicipalEvent(id='collision', source='hospital-ai', event_id='turn:collision',
                call_id='call', occurred_at=row['occurred_at'], event_type='turn_completed',
                data={**row, 'assistant_text': 'Different answer', 'location_generations': [2]}))
            record_location_revocation(session, 'hospital-ai', 'call', 1)
            session.commit()
        assert client.get('/api/team/consultation-results', headers=headers).json()[0]['assistant_text'] == ''


def test_unbound_recommendation_reference_cannot_unmask_answer(tmp_path, monkeypatch):
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'test.sqlite')
    initialize_location_tombstones(app.state.engine)
    headers = {'Authorization': 'Bearer test-hospital'}
    row = {'source': 'hospital-ai', 'event_id': 'turn:unbound', 'call_id': 'call',
           'occurred_at': '2026-10-30T06:00:00Z', 'phone': '+821000000001',
           'user_text': 'Caller words', 'assistant_text': 'Secret hospital',
           'answer_status': 'completed', 'sources': []}
    with TestClient(app) as client:
        assert client.post('/api/team/consultation-results', json=row, headers=headers).status_code == 201
        with Session(app.state.engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            session.add(MunicipalEvent(id='unbound', source='hospital-ai', event_id='turn:unbound',
                call_id='call', occurred_at=row['occurred_at'], event_type='turn_completed',
                data={**row, 'location_generations': [], 'recommendation_event_ids': ['rec:secret']}))
            record_location_revocation(session, 'hospital-ai', 'call', 1)
            session.commit()
        assert client.get('/api/team/consultation-results', headers=headers).json()[0]['assistant_text'] == ''


def test_orphan_consultation_and_legacy_recommendation_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'test.sqlite')
    initialize_location_tombstones(app.state.engine)
    headers = {'Authorization': 'Bearer test-hospital'}
    old = {'source': 'hospital-ai', 'event_id': 'turn:orphan', 'call_id': 'call',
           'occurred_at': '2026-10-30T06:00:00Z', 'phone': '+821000000001',
           'user_text': 'Caller words', 'assistant_text': 'Secret hospital',
           'answer_status': 'completed', 'sources': []}
    recommendation = {'source': 'hospital-ai', 'event_id': 'r1', 'call_id': 'call',
           'occurred_at': '2026-10-30T06:00:00Z', 'specialty': None,
           'hospitals': [{'name': 'Secret hospital', 'distance_km': 1.0, 'registered_schedule_status': 'unknown'}]}
    with TestClient(app) as client:
        assert client.post('/api/team/consultation-results', json=old, headers=headers).status_code == 201
        assert client.post('/api/team/recommendation-results', json=recommendation, headers=headers).status_code == 201
        with Session(app.state.engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            record_location_revocation(session, 'hospital-ai', 'call', 1)
            session.commit()
        rows = client.get('/api/team/consultation-results', headers=headers).json()
        assert rows[0]['assistant_text'] == '' and rows[0]['user_text'] != 'Caller words'
        assert client.get('/api/team/recommendation-results', headers=headers).json() == []
        late = {**recommendation, 'event_id': 'r-late', 'location_generation': 1}
        assert client.post('/api/team/recommendation-results', json=late, headers=headers).status_code == 201
        assert client.get('/api/team/recommendation-results', headers=headers).json() == []
        later = {**recommendation, 'event_id': 'r2', 'location_generation': 2}
        assert client.post('/api/team/recommendation-results', json=later, headers=headers).status_code == 201
        assert [row['event_id'] for row in client.get('/api/team/recommendation-results', headers=headers).json()] == ['r2']
