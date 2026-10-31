import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app import create_app


class DatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'test.sqlite3'
        self.client_context = TestClient(create_app(self.path))
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.directory.cleanup()

    def test_chart_seed_and_explicit_link(self):
        data = self.client.get('/api/team/bootstrap').json()
        self.assertEqual(len(data['patients']), 12)
        self.assertEqual(len(data['calls']), 15)
        detail = self.client.get('/api/team/calls/demo-call-003').json()
        self.assertEqual(detail['patient']['id'], 'demo-patient-003')
        self.assertEqual(len(detail['patient']['conditions']), 3)
        self.assertEqual(len(detail['patient']['current_medications']), 3)
        self.assertEqual(len(detail['patient']['prescriptions']), 3)
        self.assertIn('hospital-data-source', self.client.get('/').text)

    def test_notes_versions_and_restart_preservation(self):
        url = '/api/team/calls/demo-call-003/desk-actions'
        body = {'action_type': 'note_added', 'expected_version': 1, 'note': 'DB 보존 확인'}
        result = self.client.post(url, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.post(url, json=body).status_code, 409)
        self.assertEqual(self.client.post(url, json={'action_type': 'review_changed', 'expected_version': 2, 'new_status': 'reviewed'}).status_code, 200)
        with TestClient(create_app(self.path)) as restarted:
            detail = restarted.get('/api/team/calls/demo-call-003').json()
            self.assertEqual(detail['call']['review_status'], 'reviewed')
            self.assertTrue(any(a.get('note') == 'DB 보존 확인' for a in detail['actions']))
            self.assertEqual(len(restarted.get('/api/team/bootstrap').json()['calls']), 15)

    def test_events_idempotence_conflicts_and_location_history(self):
        call_id = 'integration-test-call'
        def send(kind, event_id, payload):
            return self.client.post('/api/team/call-events', json={
                'source': 'test', 'event_id': event_id, 'call_id': call_id,
                'event_type': kind, 'occurred_at': '2026-10-30T06:00:00Z', 'payload': payload})
        payload = {'patient_id': 'demo-patient-003', 'caller_key': 'demo-caller-3', 'name': '가상환자',
                   'age': 82, 'region': '가상 지역', 'purpose': '시연', 'visit_type': '재진',
                   'phone': '010-0000-1003', 'hospital_code': 'KJC_DEMO'}
        self.assertEqual(send('call_started', 'start', payload).status_code, 200)
        self.assertTrue(send('call_started', 'start', payload).json()['duplicate'])
        self.assertEqual(send('call_started', 'start', {**payload, 'name': '다른 내용'}).status_code, 409)
        self.assertEqual(send('message_finalized', 'message-1', {'sequence': 1, 'speaker': 'caller', 'text': '시연 대화'}).status_code, 200)
        self.assertEqual(send('message_finalized', 'message-2', {'sequence': 1, 'speaker': 'caller', 'text': '중복 순서'}).status_code, 409)
        for i, lat in enumerate([35.01, 35.02]):
            self.assertEqual(send('location_observed', f'loc-{i}', {'latitude': lat, 'longitude': 126.99, 'location_source': 'simulated'}).status_code, 200)
        self.assertEqual(send('location_observed', 'bad-loc', {'latitude': 200, 'longitude': 126.99, 'location_source': 'simulated'}).status_code, 422)
        self.assertEqual(send('call_ended', 'end', {}).status_code, 200)
        self.assertEqual(send('summary_ready', 'summary', {'summary_text': '종료 후 가상 요약'}).status_code, 200)
        detail = self.client.get(f'/api/team/calls/{call_id}').json()
        self.assertEqual(detail['call']['call_status'], 'ended')
        self.assertEqual(detail['call']['summary_status'], 'ready')
        self.assertEqual(len(detail['locations']), 2)
        self.assertEqual(len(detail['messages']), 1)
        self.assertEqual(detail['patient']['id'], 'demo-patient-003')


if __name__ == '__main__':
    unittest.main()
