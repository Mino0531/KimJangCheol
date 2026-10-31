from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommend_api import public_recommendations
from app.recommend_hira import HiraDetails, HiraHospital
from app.recommendation import RecommendationQuery
from app.services import MockLLM, MockTTS


class GeneralHospitalSource:
    def __init__(self, key):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def list_nearby(self, latitude, longitude, radius, *, specialty_code=None, classification_code=None):
        assert specialty_code is None
        if classification_code == '11':
            return [HiraHospital(id='general', name='한빛병원', address='가상 지역', phone='000',
                                 latitude=latitude + .01, longitude=longitude, classification_code='11'),
                    HiraHospital(id='clinic', name='내과의원', address='', phone='',
                                 latitude=latitude, longitude=longitude, classification_code='31')]
        return []

    async def details(self, identity):
        return HiraDetails()


@pytest.mark.anyio
async def test_general_hospital_search_does_not_replace_it_with_a_closer_clinic(monkeypatch):
    monkeypatch.setattr('app.recommend_api.HiraClient', GeneralHospitalSource)
    result = await public_recommendations(RecommendationQuery(latitude=35, longitude=127,
        specialty='정형외과', facility_type='general_hospital'), 'test')
    assert result.facility_type == 'general_hospital'
    assert [hospital.id for hospital in result.hospitals] == ['general']


def test_call_recommends_general_hospital_even_when_a_department_was_previously_suggested(tmp_path: Path, monkeypatch):
    monkeypatch.setattr('app.recommend_api.HiraClient', GeneralHospitalSource)
    settings = Settings(environment='development', service_token='s', public_data_key='test',
                        database_path=str(tmp_path / 'general.sqlite'))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={'Authorization':'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone':'+821000000000'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text':'동의합니다'})
        response = client.post(f'/calls/{call_id}/recommendations', json={
            'latitude':35, 'longitude':127, 'location_consent':True, 'specialty':'정형외과'})
        assert response.status_code == 200
        result = response.json()
        assert result['facility_type'] == 'general_hospital'
        assert result['specialty'] is None
        assert result['hospitals'][0]['name'] == '한빛병원'


@pytest.mark.anyio
async def test_no_general_hospital_does_not_fall_back_to_a_clinic(monkeypatch):
    class EmptySource(GeneralHospitalSource):
        async def list_nearby(self, *args, **kwargs):
            return []

    monkeypatch.setattr('app.recommend_api.HiraClient', EmptySource)
    result = await public_recommendations(RecommendationQuery(latitude=35, longitude=127,
        facility_type='general_hospital'), 'test')
    assert result.status == 'unavailable'
    assert result.hospitals == ()
