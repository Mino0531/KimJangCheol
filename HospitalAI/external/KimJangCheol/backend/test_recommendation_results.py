"""Exercise the authenticated, idempotent hospital recommendation receiver."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backend.app import create_app
from backend.recommendations import RecommendationRecord


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital-token')
    with TestClient(create_app(tmp_path / 'hospital.sqlite3')) as value:
        yield value


@pytest.fixture
def recommendation() -> dict:
    return {
        'source': 'hospital-ai', 'event_id': 'recommendation-demo-1', 'call_id': 'call-demo-1',
        'occurred_at': '2026-10-30T06:00:00Z', 'specialty': '내과',
        'hospitals': [{'name': '가상 병원', 'phone': '010-0000-0000', 'distance_km': 2.3,
                       'specialist_count': 1, 'registered_schedule_status': 'within_hours'}],
        'warning': '등록된 정보이며 전화 확인이 필요합니다.',
    }


def test_authentication_required(client: TestClient, recommendation: dict) -> None:
    assert client.post('/api/team/recommendation-results', json=recommendation).status_code == 401
    assert client.get('/api/team/recommendation-results').status_code == 401


def test_recommendations_are_idempotent_and_isolated(client: TestClient, recommendation: dict) -> None:
    auth = {'Authorization': 'Bearer test-hospital-token'}
    path = '/api/team/recommendation-results'
    first = client.post(path, json=recommendation, headers=auth)
    assert first.status_code == 201
    duplicate = client.post(path, json=recommendation, headers=auth)
    assert duplicate.status_code == 200 and duplicate.json()['duplicate'] is True
    conflict = client.post(path, json={**recommendation, 'warning': 'changed'}, headers=auth)
    assert conflict.status_code == 409
    output = client.get(path, headers=auth)
    assert output.headers['cache-control'] == 'no-store'
    assert output.json()[0]['call_id'] == recommendation['call_id']
    assert 'call-demo-1' not in client.get('/api/team/bootstrap').text


def test_unapproved_coordinate_and_audio_fields_are_rejected(client: TestClient, recommendation: dict) -> None:
    auth = {'Authorization': 'Bearer test-hospital-token'}
    path = '/api/team/recommendation-results'
    assert client.post(path, json={**recommendation, 'latitude': 35.0}, headers=auth).status_code == 422
    assert client.post(path, json={**recommendation, 'audio_base64': 'abc'}, headers=auth).status_code == 422
    nested = {**recommendation['hospitals'][0], 'longitude': 127.0}
    assert client.post(path, json={**recommendation, 'hospitals': [nested]}, headers=auth).status_code == 422


def test_recommendation_hours_and_specialty_are_preserved(client: TestClient, recommendation: dict) -> None:
    auth = {'Authorization': 'Bearer test-hospital-token'}
    path = '/api/team/recommendation-results'
    hospital = {**recommendation['hospitals'][0], 'hours': {'mon': ['0900', '1800']},
                'specialty_match': 'yes', 'source': '건강보험심사평가원'}
    assert client.post(path, json={**recommendation, 'hospitals': [hospital]}, headers=auth).status_code == 201
    received = client.get(path, headers=auth).json()[0]
    assert received['specialty'] == '내과'
    assert received['hospitals'][0]['hours']['mon'] == ['0900', '1800']


def test_public_hospital_address_is_available_for_followup(client: TestClient, recommendation: dict) -> None:
    auth = {'Authorization': 'Bearer test-hospital-token'}
    path = '/api/team/recommendation-results'
    hospital = {**recommendation['hospitals'][0], 'address': '전남 가상군 가상로 10'}
    assert client.post(path, json={**recommendation, 'hospitals': [hospital]}, headers=auth).status_code == 201
    assert client.get(path, headers=auth).json()[0]['hospitals'][0]['address'] == hospital['address']
    excessive = {**hospital, 'address': '가' * 501}
    assert client.post(path, json={**recommendation, 'hospitals': [excessive]}, headers=auth).status_code == 422


def test_retry_of_older_record_without_address_is_still_idempotent(client: TestClient, recommendation: dict) -> None:
    auth = {'Authorization': 'Bearer test-hospital-token'}
    path = '/api/team/recommendation-results'
    assert client.post(path, json=recommendation, headers=auth).status_code == 201
    with Session(client.app.state.engine) as session:
        record = session.exec(select(RecommendationRecord)).one()
        legacy = dict(record.data)
        legacy['hospitals'] = [{key: value for key, value in item.items() if key != 'address'}
                               for item in legacy['hospitals']]
        record.data = legacy
        session.add(record)
        session.commit()
    retry = client.post(path, json=recommendation, headers=auth)
    assert retry.status_code == 200
    assert retry.json()['duplicate'] is True


def test_list_orders_by_actual_instant_across_timezones(client: TestClient, recommendation: dict) -> None:
    auth = {'Authorization': 'Bearer test-hospital-token'}
    path = '/api/team/recommendation-results'
    earlier = {**recommendation, 'event_id': 'earlier', 'occurred_at': '2026-10-30T14:00:00+09:00'}
    later = {**recommendation, 'event_id': 'later', 'occurred_at': '2026-10-30T06:00:00Z'}
    assert client.post(path, json=earlier, headers=auth).status_code == 201
    assert client.post(path, json=later, headers=auth).status_code == 201
    assert [item['event_id'] for item in client.get(path, headers=auth).json()] == ['later', 'earlier']


def test_rejects_untrusted_source_or_invalid_hours(client: TestClient, recommendation: dict) -> None:
    auth = {'Authorization': 'Bearer test-hospital-token'}
    path = '/api/team/recommendation-results'
    assert client.post(path, json={**recommendation, 'source': 'untrusted'}, headers=auth).status_code == 422
    for hours in ({'mon': ['9999', '1800']}, {'mon': ['0900', '2460']}, {'mon': ['2400', '0100']}):
        hospital = {**recommendation['hospitals'][0], 'hours': hours}
        assert client.post(path, json={**recommendation, 'hospitals': [hospital]}, headers=auth).status_code == 422
    valid = {**recommendation['hospitals'][0], 'hours': {'mon': ['0900', '2400']}}
    assert client.post(path, json={**recommendation, 'hospitals': [valid]}, headers=auth).status_code == 201
