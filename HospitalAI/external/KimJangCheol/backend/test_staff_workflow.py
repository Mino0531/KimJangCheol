"""Employee workflow, access controls and concurrency over actual SQLite."""

from backend.test_municipal_handoff import add_call, setup


def test_municipal_review_can_start_and_hand_off(setup):
    client, engine = setup
    add_call(engine, urgent=True)
    staff = {'Authorization': 'Bearer municipal-test'}
    path = '/api/municipality/inbox/call-1/workflow'
    result = client.post(path, headers=staff, json={'expected_version': 3, 'action': 'start', 'actor_name': '직원', 'note': '확인 중'})
    assert result.status_code == 200
    assert result.json()['case']['status'] == 'in_progress'
    assert result.json()['case']['workflow_history'][-1]['actor_name'] == '직원'
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff,
                       json={'expected_version': 4}).status_code == 200
    completed = client.post(path, headers=staff, json={'expected_version': 5, 'action': 'complete', 'actor_name': '직원', 'note': '인계 확인'})
    assert completed.status_code == 200 and completed.json()['case']['status'] == 'completed'


def test_workflow_rejects_stale_or_wrong_role(setup):
    client, engine = setup
    add_call(engine)
    body = {'expected_version': 3, 'action': 'start', 'actor_name': '직원', 'note': ''}
    path = '/api/municipality/inbox/call-1/workflow'
    assert client.post(path, headers={'Authorization': 'Bearer hospital-test'}, json=body).status_code == 401
    assert client.post(path, headers={'Authorization': 'Bearer municipal-test'}, json=body).status_code == 200
    assert client.post(path, headers={'Authorization': 'Bearer municipal-test'}, json=body).status_code == 409


def test_hospital_workflow_requires_handoff_and_is_visible_on_handoff(setup):
    client, engine = setup
    add_call(engine)
    hospital = {'Authorization': 'Bearer hospital-test'}
    staff = {'Authorization': 'Bearer municipal-test'}
    path = '/api/team/municipality-cases/call-1/workflow'
    body = {'expected_version': 0, 'action': 'start', 'actor_name': '병원직원', 'note': '검토 중'}
    assert client.post(path, headers=hospital, json=body).status_code == 404
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3}).status_code == 200
    assert client.post(path, headers=staff, json=body).status_code == 401
    started = client.post(path, headers=hospital, json=body)
    assert started.status_code == 200 and started.json()['workflow']['status'] == 'in_progress'
    assert client.post(path, headers=hospital, json=body).status_code == 409
    completed = client.post(path, headers=hospital, json={**body, 'expected_version': 1, 'action': 'complete'})
    assert completed.status_code == 200 and completed.json()['workflow']['status'] == 'completed'
    row = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]
    assert row['workflow']['status'] == 'completed'


def test_new_handoff_reopens_hospital_review(setup):
    client, engine = setup
    add_call(engine)
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    path = '/api/team/municipality-cases/call-1/workflow'
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3}).status_code == 200
    body = {'expected_version': 0, 'action': 'complete', 'actor_name': '병원직원'}
    assert client.post(path, headers=hospital, json=body).status_code == 200
    from sqlmodel import Session
    from backend.municipal_intake import MunicipalCase
    with Session(engine) as session:
        case = session.get(MunicipalCase, 'call-1')
        case.status = 'acknowledged'
        case.version = 5
        session.add(case)
        session.commit()
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 5}).status_code == 200
    row = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]
    assert row['workflow']['status'] == 'received' and row['workflow']['version'] == 2


def test_location_revocation_redacts_already_handed_off_answer(setup):
    from sqlmodel import Session
    from backend.location_tombstones import record_location_revocation

    client, engine = setup
    add_call(engine)
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3}).status_code == 200
    with Session(engine) as session:
        record_location_revocation(session, 'hospital-ai', 'call-1', 1)
        session.commit()
    entry = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]['events'][0]
    assert entry['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
    assert entry['assistant_text'] == '' and entry['sources'] == [] and entry['location_withdrawn']


def test_handoff_snapshots_reviewed_call_end(setup):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from backend.municipal_intake import MunicipalEvent

    client, engine = setup
    add_call(engine)
    at = datetime.now(timezone.utc).isoformat()
    payload = {'source': 'hospital-ai', 'event_id': 'end:call-1', 'call_id': 'call-1',
               'occurred_at': at, 'event_type': 'call_ended', 'end_reason': 'hangup',
               'phone': '010-0000-0000', 'user_text': '', 'assistant_text': '', 'answer_status': None, 'sources': []}
    with Session(engine) as session:
        session.add(MunicipalEvent(id='event-end', source='hospital-ai', event_id='end:call-1',
                                   call_id='call-1', occurred_at=at, event_type='call_ended', data=payload))
        session.commit()
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3}).status_code == 200
    row = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]
    assert row['call_end'] == {'end_reason': 'hangup', 'occurred_at': at}


def test_call_end_after_handoff_updates_hospital_metadata(setup):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from backend.municipal_intake import MunicipalEvent

    client, engine = setup
    add_call(engine)
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3}).status_code == 200
    at = datetime.now(timezone.utc).isoformat()
    payload = {'source': 'hospital-ai', 'event_id': 'end:call-1', 'call_id': 'call-1',
               'occurred_at': at, 'event_type': 'call_ended', 'end_reason': 'hangup'}
    with Session(engine) as session:
        session.add(MunicipalEvent(id='event-late-end', source='hospital-ai', event_id='end:call-1',
                                   call_id='call-1', occurred_at=at, event_type='call_ended', data=payload))
        session.commit()
    row = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]
    assert row['call_end'] == {'end_reason': 'hangup', 'occurred_at': at}


def test_late_playback_receipt_does_not_modify_reviewed_snapshot(setup):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from backend.municipal_intake import MunicipalEvent

    client, engine = setup
    add_call(engine)
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3}).status_code == 200
    at = datetime.now(timezone.utc).isoformat()
    with Session(engine) as session:
        for event_id in ('playback:turn-1', 'playback:unreviewed'):
            session.add(MunicipalEvent(id=f'event-{event_id}', source='hospital-ai', event_id=event_id,
                call_id='call-1', occurred_at=at, event_type='playback_completed', data={'event_id': event_id}))
        session.commit()
    row = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]
    assert row['playback_completed_event_ids'] == []


def test_handoff_after_withdrawal_does_not_copy_derived_answer(setup):
    from sqlmodel import Session, select
    from backend.consultations import ConsultationRecord
    from backend.location_tombstones import record_location_revocation

    client, engine = setup
    add_call(engine)
    with Session(engine) as session:
        record_location_revocation(session, 'hospital-ai', 'call-1', 1)
        session.commit()
    staff = {'Authorization': 'Bearer municipal-test'}
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3}).status_code == 200
    with Session(engine) as session:
        row = session.exec(select(ConsultationRecord)).first()
        assert row.data['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'
        assert row.data['assistant_text'] != '의료진과 확인하세요.'
        assert row.data['sources'] == []


def test_withdrawal_never_downgrades_urgent_status_in_handoff(setup):
    from sqlmodel import Session, select
    from backend.consultations import ConsultationRecord
    from backend.location_tombstones import record_location_revocation

    client, engine = setup
    add_call(engine, urgent=True)
    with Session(engine) as session:
        record_location_revocation(session, 'hospital-ai', 'call-1', 1)
        session.commit()
    assert client.post('/api/municipality/inbox/call-1/handoff', headers={'Authorization': 'Bearer municipal-test'},
                       json={'expected_version': 3}).status_code == 200
    with Session(engine) as session:
        assert session.exec(select(ConsultationRecord)).first().data['answer_status'] == 'emergency'


def test_recommendation_handoff_disappears_after_withdrawal(setup):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from backend.location_tombstones import record_location_revocation
    from backend.municipal_intake import MunicipalEvent

    client, engine = setup
    add_call(engine)
    at = datetime.now(timezone.utc).isoformat()
    payload = {'source': 'hospital-ai', 'event_id': 'recommendation-1', 'call_id': 'call-1',
               'occurred_at': at, 'event_type': 'recommendation', 'location_generation': 1,
               'recommendation': {'specialty': '내과', 'hospitals': [{'name': '가상 병원'}], 'warning': None}}
    with Session(engine) as session:
        session.add(MunicipalEvent(id='event-reco', source='hospital-ai', event_id='recommendation-1',
                                   call_id='call-1', occurred_at=at, event_type='recommendation', data=payload))
        session.commit()
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3}).status_code == 200
    assert len(client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]['recommendations']) == 1
    with Session(engine) as session:
        record_location_revocation(session, 'hospital-ai', 'call-1', 1)
        session.commit()
    assert client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]['recommendations'] == []


def test_handoff_preserves_utterance_without_claiming_answer(setup):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from backend.municipal_intake import MunicipalCase, MunicipalEvent

    client, engine = setup
    at = datetime.now(timezone.utc).isoformat()
    payload = {'source': 'hospital-ai', 'event_id': 'utterance:1', 'call_id': 'call-1', 'utterance_id': '1',
               'occurred_at': at, 'event_type': 'caller_utterance', 'user_text': '무릎이 아파요'}
    with Session(engine) as session:
        session.add(MunicipalCase(call_id='call-1', source='hospital-ai', phone='01000000000', created_at=at,
                                  updated_at=at, urgent=False, unread=False, version=1, status='acknowledged'))
        session.add(MunicipalEvent(id='utterance-db-1', source='hospital-ai', event_id='utterance:1',
                                   call_id='call-1', occurred_at=at, event_type='caller_utterance', data=payload))
        session.commit()
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 1}).status_code == 200
    row = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]
    assert row['events'] == []
    assert row['unanswered_utterances'][0]['user_text'] == '무릎이 아파요'
    from backend.location_tombstones import record_location_revocation
    with Session(engine) as session:
        record_location_revocation(session, 'hospital-ai', 'call-1', 1)
        session.commit()
    row = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]
    assert row['unanswered_utterances'][0]['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'


def test_redacted_only_handoff_never_claims_unanswered_or_interrupted(setup):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from backend.municipal_intake import MunicipalCase, MunicipalEvent

    client, engine = setup
    at = datetime.now(timezone.utc).isoformat()
    payload = {'source': 'hospital-ai', 'event_id': 'privacy:1', 'call_id': 'call-1',
               'occurred_at': at, 'event_type': 'privacy_redacted',
               'user_text': '위치 정보가 포함될 수 있어 원문 확인 제한', 'assistant_text': '',
               'answer_status': None, 'sources': [], 'phone': '01000000000'}
    with Session(engine) as session:
        session.add(MunicipalCase(call_id='call-1', source='hospital-ai', phone='01000000000', created_at=at,
                                  updated_at=at, urgent=False, unread=False, version=1, status='acknowledged'))
        session.add(MunicipalEvent(id='privacy-db-1', source='hospital-ai', event_id='privacy:1',
                                   call_id='call-1', occurred_at=at, event_type='privacy_redacted', data=payload))
        session.commit()
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    assert client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 1}).status_code == 200
    row = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'][0]
    assert row['events'] == [] and row['unanswered_utterances'] == []
    assert row['privacy_redactions'][0]['user_text'] == '위치 정보가 포함될 수 있어 원문 확인 제한'


def test_out_of_order_utterance_and_answer_are_not_duplicated_in_handoff(setup):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from backend.municipal_intake import MunicipalCase, MunicipalEvent

    client, engine = setup
    add_call(engine)
    at = datetime.now(timezone.utc).isoformat()
    with Session(engine) as session:
        turn = session.get(MunicipalEvent, 'event-1')
        turn.data = {**turn.data, 'utterance_id': 'u-1'}
        raw = {'source': 'hospital-ai', 'event_id': 'utterance:u-1', 'call_id': 'call-1',
               'utterance_id': 'u-1', 'occurred_at': at, 'event_type': 'caller_utterance',
               'user_text': turn.data['user_text'], 'phone': '010-0000-0000'}
        session.add(turn)
        session.add(MunicipalEvent(id='raw-1', source='hospital-ai', event_id=raw['event_id'],
                                   call_id='call-1', occurred_at=at, event_type='caller_utterance', data=raw))
        session.commit()
    assert client.post('/api/municipality/inbox/call-1/handoff', headers={'Authorization': 'Bearer municipal-test'},
                       json={'expected_version': 3}).status_code == 200
    row = client.get('/api/team/municipality-handoffs', headers={'Authorization': 'Bearer hospital-test'}).json()['handoffs'][0]
    assert row['events'][0]['utterance_id'] == 'u-1'
    assert row['unanswered_utterances'] == []


def test_late_answer_is_correlated_with_earlier_unanswered_handoff(setup):
    from sqlmodel import Session
    from backend.municipal_intake import MunicipalCase, MunicipalEvent

    client, engine = setup
    add_call(engine)
    with Session(engine) as session:
        turn = session.get(MunicipalEvent, 'event-1')
        original = dict(turn.data)
        original['utterance_id'] = 'u-1'
        raw = {**original, 'event_id': 'utterance:u-1', 'event_type': 'caller_utterance',
               'assistant_text': '', 'answer_status': None, 'sources': []}
        turn.event_type = 'caller_utterance'
        turn.event_id = raw['event_id']
        turn.data = raw
        session.add(turn)
        session.commit()
    staff, hospital = {'Authorization': 'Bearer municipal-test'}, {'Authorization': 'Bearer hospital-test'}
    endpoint = '/api/municipality/inbox/call-1/handoff'
    assert client.post(endpoint, headers=staff, json={'expected_version': 3}).status_code == 200
    with Session(engine) as session:
        case = session.get(MunicipalCase, 'call-1')
        case.status, case.version = 'acknowledged', 5
        session.add(case)
        session.add(MunicipalEvent(id='late-turn', source='hospital-ai', event_id=original['event_id'],
                                   call_id='call-1', occurred_at=original['occurred_at'],
                                   event_type='turn_completed', data=original))
        session.commit()
    assert client.post(endpoint, headers=staff, json={'expected_version': 5}).status_code == 200
    handoffs = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs']
    raw = [entry for handoff in handoffs for entry in handoff['unanswered_utterances']]
    answers = [entry for handoff in handoffs for entry in handoff['events']]
    assert raw[0]['utterance_id'] == answers[0]['utterance_id'] == 'u-1'
