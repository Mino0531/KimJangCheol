import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.preparation import compare_words, FIXTURES


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = TestClient(create_app(Path(self.temp.name) / 'preparation.sqlite3'))
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def preparation(self, call='demo-call-003', q=''):
        response = self.client.get(f'/api/team/calls/{call}/preparation', params={'q': q})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_chart_matches_and_traceable_preparation_sources(self):
        result = self.preparation()
        ids = {c['document_id'] for c in result['cards']}
        self.assertTrue({'guide_9', 'guide_10', 'guide_11'} <= ids)
        self.assertTrue({'guide_20', 'guide_22', 'guide_23', 'guide_24'} <= ids)
        self.assertEqual(result['knowledge_document_count'], 100)
        for card in result['cards']:
            doc = self.client.get(f"/api/team/knowledge/documents/{card['document_id']}").json()
            self.assertEqual(card['scope'], 'visit_preparation_only')
            self.assertIn(card['easy_text'], doc['content'])
            self.assertIn(card['staff_question'], doc['content'])
            self.assertTrue(card['reasons'])
            self.assertTrue(all(not r['used_for_action_generation'] for r in card['reference_documents']))
        different = self.preparation('demo-call-004')
        self.assertTrue({'guide_14','guide_15'} <= {c['document_id'] for c in different['cards']})
        self.assertNotIn('guide_10', {c['document_id'] for c in different['cards']})

    def test_plain_language_alias_and_query_do_not_change_chart(self):
        before = self.client.get('/api/team/patients/demo-patient-003/chart').json()
        result = self.preparation(q='피검사 하러 가요')
        self.assertIn('guide_27', {c['document_id'] for c in result['cards']})
        words = self.client.get('/api/team/knowledge/word-levels', params={'q':'피검사 하러 가요'}).json()['terms']
        self.assertTrue(any(t['canonical'] == '채혈' and t['match_type']=='alias' for t in words))
        self.assertTrue(all(t['professional_level'] >= t['easy_level'] for t in words))
        after = self.client.get('/api/team/patients/demo-patient-003/chart').json()
        self.assertEqual(before, after)
        no_inference = self.preparation('demo-call-004', '혈당이 높음')
        self.assertNotIn('guide_10', {c['document_id'] for c in no_inference['cards']})

    def test_medication_and_fasting_questions_never_generate_clinical_commands(self):
        result = self.preparation(q='내시경 피검사 약 먹어도 돼요')
        self.assertTrue({'guide_27','guide_29','guide_33'} <= {c['document_id'] for c in result['cards']})
        self.assertFalse(result['clinical_instructions_generated'])
        self.assertFalse(result['llm_used'])
        self.assertFalse(result['embedding_used'])
        text=' '.join(c['easy_text'] for c in result['cards'])
        for command in ['아침 약을 꼭', '8시간', '금식하세요', '약을 중단하세요', '이석증일 확률']:
            self.assertNotIn(command, text)

    def test_empty_chart_and_missing_call(self):
        result = self.preparation('demo-call-008')
        ids = {c['document_id'] for c in result['cards']}
        self.assertIn('guide_19', ids)
        self.assertNotIn('guide_10', ids)
        self.assertNotIn('guide_8', ids)
        self.assertEqual(self.client.get('/api/team/calls/no-such-call/preparation').status_code,404)

    def test_vocabulary_limits_and_no_false_matches(self):
        terms = json.loads((FIXTURES/'vocabulary-levels.json').read_text())['terms']
        self.assertEqual(len(terms),27)
        self.assertEqual(compare_words('행복한 가을입니다',terms),[])
        self.assertEqual(compare_words('!!!',terms),[])


if __name__=='__main__':unittest.main()
