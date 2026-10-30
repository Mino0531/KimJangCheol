import copy
import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, select
from backend.app import create_app, ChartRevision
from backend.chart_schema import ChartBundle

DATA = Path(__file__).parent / 'fixtures/charts-v1.json'


class ChartFormatTests(unittest.TestCase):
    def setUp(self):
        self.bundle = json.loads(DATA.read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.context = TestClient(create_app(Path(self.temp.name) / 'charts.sqlite3'))
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_common_format_export_roundtrip_and_medication_status(self):
        ChartBundle.model_validate(self.bundle)
        data = self.client.get('/api/team/patients/demo-patient-003/chart').json()
        self.assertEqual(data['schema_version'], '1.0')
        self.assertEqual(data['identity']['name'], '이정례')
        self.assertEqual(data['visit_preparation']['phone_type'], 'feature_phone')
        self.assertEqual(len(data['current_medications']), 3)
        self.assertEqual(len(data['medication_history']), 4)
        self.assertNotIn('이전시연정 Z', [m['name'] for m in data['current_medications']])
        exported = self.client.get('/api/team/patients/demo-patient-003/chart/export').json()
        ChartBundle.model_validate(exported)
        self.assertEqual(exported['patients'][0]['patient_id'], 'demo-patient-003')
        unknown = self.client.get('/api/team/patients/demo-patient-008/chart').json()
        reported_none = self.client.get('/api/team/patients/demo-patient-005/chart').json()
        self.assertEqual(unknown['allergies']['knowledge_status'], 'unknown')
        self.assertEqual(reported_none['allergies']['knowledge_status'], 'none_reported')

    def test_invalid_records_rejected_before_import(self):
        changes = [
            lambda b: b['patients'][0]['identity'].update(birth_date='2099-01-01'),
            lambda b: b['patients'][0]['allergies'].update(knowledge_status='unknown'),
            lambda b: b['patients'][0]['prescriptions'][0]['items'][0].update(medication_id='does-not-exist'),
            lambda b: b['patients'][0]['observations'][0].update(glucose_unit='mmol/L'),
            lambda b: b['patients'][0].update(unexpected_field='wrong'),
            lambda b: b['patients'].append(copy.deepcopy(b['patients'][0])),
        ]
        for change in changes:
            altered = copy.deepcopy(self.bundle)
            change(altered)
            with self.assertRaises(ValidationError):
                ChartBundle.model_validate(altered)
            self.assertEqual(self.client.post('/api/team/chart-imports', json=altered).status_code, 422)
        self.assertEqual(len(self.client.get('/api/team/chart-imports').json()), 1)

    def test_idempotent_import_revisions_and_staff_record_preservation(self):
        first = self.client.post('/api/team/chart-imports', json=self.bundle)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['duplicate'])
        note = self.client.post('/api/team/calls/demo-call-003/desk-actions', json={
            'action_type': 'note_added', 'expected_version': 1, 'note': '차트 갱신 후에도 남는 메모'})
        self.assertEqual(note.status_code, 200)
        altered = copy.deepcopy(self.bundle)
        altered['patients'][2]['visit_preparation']['companion'] = 'planned'
        result = self.client.post('/api/team/chart-imports', json=altered)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['applied'])
        detail = self.client.get('/api/team/calls/demo-call-003').json()
        self.assertEqual(detail['patient']['visit_preparation']['companion'], 'planned')
        self.assertEqual(detail['call']['version'], 2)
        self.assertTrue(any(a.get('note') == '차트 갱신 후에도 남는 메모' for a in detail['actions']))
        with Session(self.context.app.state.engine) as session:
            revisions = session.exec(select(ChartRevision).where(ChartRevision.patient_id == 'demo-patient-003')).all()
            self.assertEqual(len(revisions), 2)
            self.assertTrue(any(r.previous.get('visit_preparation', {}).get('companion') == 'not_planned' for r in revisions))

    def test_conflicting_identity_rejects_entire_batch(self):
        altered = copy.deepcopy(self.bundle)
        altered['patients'][0]['visit_preparation']['staff_note'] = '이 변경도 롤백되어야 합니다.'
        altered['patients'][2]['identity']['name'] = '다른 가상 환자'
        result = self.client.post('/api/team/chart-imports', json=altered)
        self.assertEqual(result.status_code, 409)
        saved = self.client.get('/api/team/patients/demo-patient-001/chart').json()
        self.assertNotEqual(saved['visit_preparation']['staff_note'], '이 변경도 롤백되어야 합니다.')
        self.assertEqual(len(self.client.get('/api/team/chart-imports').json()), 1)


if __name__ == '__main__':
    unittest.main()
