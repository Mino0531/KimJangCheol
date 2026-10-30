import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app import create_app


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'coordination.sqlite3'
        self.context = TestClient(create_app(self.path))
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.directory.cleanup()

    def detail(self, case_id='coordination-001'):
        result = self.client.get(f'/api/municipality/cases/{case_id}')
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def act(self, kind, case_id='coordination-001', **values):
        version = self.detail(case_id)['case']['version']
        return self.client.post(f'/api/municipality/cases/{case_id}/actions', json={
            'action_type': kind, 'expected_version': version, **values})

    def test_shared_calls_without_chart_payload_and_built_pages(self):
        bootstrap = self.client.get('/api/municipality/bootstrap').json()
        self.assertEqual(len(bootstrap['cases']), 12)
        self.assertEqual(len(bootstrap['calls']), 15)
        for c in bootstrap['cases']:
            self.assertNotIn('patient', c)
            self.assertNotIn('conditions', c['call'])
            self.assertNotIn('patient_id', c['call'])
        detail = self.detail('coordination-003')
        self.assertEqual(detail['case']['call']['name'], '이정례')
        self.assertEqual(len(detail['messages']), 5)
        self.assertNotIn('patient', detail)
        page = self.client.get('/municipality')
        self.assertEqual(page.status_code, 200)
        self.assertIn('municipality-data-source', page.text)
        self.assertNotIn('HospitalCommonChartFixtures', page.text)
        self.assertIn('coordination-inbox', self.client.get('/').text)
        self.assertEqual(self.client.get('/municipality/').status_code, 200)
        past = self.client.get('/api/municipality/calls/past-call-1').json()
        self.assertEqual(past['call']['name'], '이정례')
        self.assertTrue(past['messages'])

    def test_complete_roundtrip_hospital_response_and_resident_contact(self):
        self.assertEqual(self.act('completed', note='조기 완료 시도').status_code, 409)
        self.assertEqual(self.act('hospital_requested', note='접수 창구 확인 요청').status_code, 409)
        self.assertEqual(self.act('metadata_updated', assignee='김하늘', priority='attention', due_at='2026-10-31T11:00:00+09:00').status_code, 200)
        self.assertEqual(self.act('hospital_requested', note='접수 창구를 알려 주세요.').status_code, 200)
        referrals = self.client.get('/api/team/hospital/referrals').json()['referrals']
        sent = next(c for c in referrals if c['id'] == 'coordination-001')
        self.assertEqual(sent['hospital_request'], '접수 창구를 알려 주세요.')
        response = self.client.post('/api/team/hospital/referrals/coordination-001/responses', json={
            'expected_version': sent['version'], 'response_text': '가상 회신: 1층 안내 데스크에서 문의해 주세요.'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.detail()['case']['status'], 'response_received')
        self.assertEqual(self.act('contact_logged', outcome='no_answer', note='부재중으로 재연락 필요').status_code, 200)
        self.assertEqual(self.detail()['case']['status'], 'response_received')
        self.assertEqual(self.act('completed', note='부재중인데 완료 시도').status_code, 409)
        self.assertEqual(self.act('contact_logged', outcome='connected', note='병원 답변을 천천히 전달했습니다.').status_code, 200)
        self.assertEqual(self.detail()['case']['status'], 'resident_informed')
        self.assertEqual(self.act('completed', note='주민 안내와 요청 확인을 마쳤습니다.').status_code, 200)
        self.assertEqual(self.detail()['case']['status'], 'completed')
        self.assertEqual(self.act('hospital_requested', note='완료 후 재전송').status_code, 409)
        self.assertEqual(self.act('note_added', note='후속 참고 메모').status_code, 200)
        with TestClient(create_app(self.path)) as restarted:
            result = restarted.get('/api/municipality/cases/coordination-001').json()
            self.assertEqual(result['case']['status'], 'completed')
            self.assertEqual(result['case']['assignee'], '김하늘')
            self.assertTrue(any(a['note'] == '후속 참고 메모' for a in result['actions']))
            self.assertEqual(len(restarted.get('/api/municipality/bootstrap').json()['cases']), 12)

    def test_optimistic_lock_invalid_actions_and_payloads(self):
        c = self.detail()['case']
        body = {'action_type': 'note_added', 'expected_version': c['version'], 'note': '첫 번째 메모'}
        self.assertEqual(self.client.post('/api/municipality/cases/coordination-001/actions', json=body).status_code, 200)
        self.assertEqual(self.client.post('/api/municipality/cases/coordination-001/actions', json=body).status_code, 409)
        self.assertEqual(sum(a['note'] == '첫 번째 메모' for a in self.detail()['actions']), 1)
        self.assertEqual(self.act('metadata_updated', assignee='김하늘', priority='normal', due_at='2026-10-30T15:00:00').status_code, 422)
        self.assertEqual(self.act('note_added', note='  ').status_code, 422)
        self.assertEqual(self.act('hospital_responded', note='잘못된 역할 API').status_code, 422)
        self.assertEqual(self.act('contact_logged', note='결과 누락').status_code, 422)
        self.assertEqual(self.client.get('/api/municipality/cases/missing').status_code, 404)
        self.assertEqual(self.client.get('/api/municipality/calls/missing').status_code, 404)
        sent = self.detail('coordination-003')['case']
        body = {'expected_version': sent['version'], 'response_text': '가상 병원 답변'}
        url = '/api/team/hospital/referrals/coordination-003/responses'
        self.assertEqual(self.client.post(url, json=body).status_code, 200)
        self.assertEqual(self.client.post(url, json=body).status_code, 409)

    def test_register_old_call_duplicate_and_unknown_call(self):
        body = {'call_id': 'past-call-1', 'category': '연락 지원', 'request_text': '이전 통화의 안내 재확인'}
        response = self.client.post('/api/municipality/cases', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        c = response.json()['case']
        self.assertEqual(c['status'], 'received')
        self.assertEqual(c['support']['device'], '미확인')
        self.assertEqual(self.client.post('/api/municipality/cases', json=body).status_code, 409)
        self.assertEqual(self.client.post('/api/municipality/cases', json={**body, 'call_id': 'missing'}).status_code, 404)
        self.assertEqual(self.client.post('/api/municipality/cases', json={**body, 'request_text': ' '}).status_code, 422)
        self.assertEqual(len(self.client.get('/api/team/bootstrap').json()['calls']), 15)
        self.assertEqual(len(self.client.get('/api/municipality/bootstrap').json()['cases']), 13)

    def test_safety_handoff_requires_actual_contact_and_preserves_visit_workflow(self):
        c = self.detail()['case']
        self.assertIsNone(c['safety'])
        self.assertEqual(self.act('safety_opened', situation_type='health_concern', location_text='현재 위치 미확인', note='가상 전달: 갑작스러운 상태 변화로 즉시 연락 지원이 필요합니다.').status_code, 200)
        self.assertEqual(self.detail()['case']['safety']['status'], 'checking')
        self.assertEqual(self.detail()['case']['status'], c['status'])
        self.assertEqual(self.act('hospital_requested', note='위기 상황인데 병원 답변 대기 시도').status_code, 409)
        self.assertEqual(self.act('safety_opened', situation_type='other', note='중복 접수').status_code, 409)
        self.assertEqual(self.act('safety_handoff_recorded', note='연락 전 인계 시도').status_code, 409)
        self.assertEqual(self.act('safety_contact_logged', contact_target='119', contact_result='no_answer', note='가상 시연: 연결되지 않았다고 입력').status_code, 200)
        self.assertEqual(self.detail()['case']['safety']['status'], 'checking')
        self.assertEqual(self.act('safety_closed', resolution='handoff_confirmed', note='인계 전 종료 시도').status_code, 409)
        self.assertEqual(self.act('safety_contact_logged', contact_target='119', contact_result='connected', note='가상 시연: 직접 통화하고 상황·위치를 전달했다고 입력').status_code, 200)
        self.assertEqual(self.detail()['case']['safety']['status'], 'contacted')
        self.assertEqual(self.act('safety_closed', resolution='safe_confirmed', note='119 연결만으로 안부 확정 시도').status_code, 409)
        self.assertEqual(self.act('safety_handoff_recorded', note='가상 인계: 관계기관 담당자에게 전달·확인한 내용').status_code, 200)
        self.assertEqual(self.act('safety_closed', resolution='handoff_confirmed', note='가상 인계 확인을 마치고 후속 연락 담당자를 기록').status_code, 200)
        final = self.detail()['case']
        self.assertEqual(final['safety']['status'], 'closed')
        self.assertEqual(final['status'], c['status'])
        self.assertEqual(final['safety']['handoff_target'], '119')
        with TestClient(create_app(self.path)) as restarted:
            saved = restarted.get('/api/municipality/cases/coordination-001').json()['case']
            self.assertEqual(saved['safety']['status'], 'closed')

    def test_safety_direct_welfare_confirmation_and_record_validation(self):
        case_id = 'coordination-012'
        self.assertEqual(self.detail(case_id)['case']['safety']['category'], 'unreachable')
        self.assertEqual(self.act('safety_closed', case_id, resolution='safe_confirmed', note='미연락 종료').status_code, 409)
        self.assertEqual(self.act('safety_contact_logged', case_id, contact_target='주민 본인', note='결과 누락').status_code, 422)
        self.assertEqual(self.act('safety_contact_logged', case_id, contact_target='주민 본인', contact_result='connected', note='가상 시연: 주민 본인과 직접 안부를 확인했다고 입력').status_code, 200)
        self.assertEqual(self.act('safety_closed', case_id, resolution='safe_confirmed', note='가상 시연: 직접 확인한 안부와 다음 연락 계획을 기록').status_code, 200)
        self.assertEqual(self.act('safety_updated', case_id, note='닫힌 대응 변경 시도', location_text='새 위치').status_code, 409)
        self.assertEqual(self.act('safety_opened', case_id, situation_type='other', note='새로운 가상 추가 상황 접수').status_code, 200)
        action = next(a for a in self.detail(case_id)['actions'] if a['note'] == '새로운 가상 추가 상황 접수')
        self.assertEqual(action['previous_safety']['status'], 'closed')
        self.assertEqual(self.detail(case_id)['case']['safety']['location_text'], '현재 위치 미확인')

    def test_safety_no_automatic_triage_and_conflicting_records_rejected(self):
        self.assertEqual(self.act('note_added', note='가상 전달: 쓰러짐이라는 말을 들었습니다.').status_code, 200)
        self.assertIsNone(self.detail()['case']['safety'])
        version = self.detail()['case']['version']
        body = {'action_type': 'safety_opened', 'expected_version': version, 'situation_type': 'other', 'note': '담당자가 직접 확인 필요로 등록'}
        path = '/api/municipality/cases/coordination-001/actions'
        self.assertEqual(self.client.post(path, json=body).status_code, 200)
        self.assertEqual(self.client.post(path, json=body).status_code, 409)
        self.assertEqual(self.act('safety_opened', situation_type='invalid', note='지원하지 않는 유형').status_code, 422)
        bootstrap = self.client.get('/api/municipality/bootstrap').json()
        self.assertIn('checked_at', bootstrap['safety_guidance'])
        self.assertTrue(bootstrap['safety_guidance']['resources'][0]['url'].startswith('https://easylaw.go.kr/'))
        self.assertNotIn('triage_score', self.detail()['case']['safety'])



if __name__ == '__main__':
    unittest.main()
