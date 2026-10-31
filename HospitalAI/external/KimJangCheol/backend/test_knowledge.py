import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from backend.app import create_app
from backend.knowledge import SOURCE, KnowledgeDocument, KnowledgeRevision, import_knowledge, parse_source


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'rag.sqlite3'
        self.app = create_app(self.path)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_import_source_fidelity_and_pending_review(self):
        _, rows = parse_source(SOURCE.read_bytes())
        stats = self.client.get('/api/team/knowledge/stats').json()
        self.assertEqual(stats['document_count'], 100)
        self.assertEqual(stats['groups'], {'emerg': 26, 'freq': 27, 'spec': 14, 'guide': 33})
        self.assertEqual(stats['chunks'], 100)
        self.assertEqual(stats['pending_review'], 100)
        for expected in rows:
            row = self.client.get(f"/api/team/knowledge/documents/{expected['id']}").json()
            self.assertEqual(row['content'], expected['content'])
            self.assertEqual(row['category'], expected['category'])
            self.assertFalse(row['retrieval_enabled'])
            self.assertIsNone(row['source_url'])
        self.assertEqual(self.client.get('/api/team/knowledge/retrieve', params={'q': '당뇨'}).json()['count'], 0)
        self.assertTrue(self.client.get('/api/team/knowledge/documents', params={'q': '당뇨'}).json()['count'] > 0)
        self.assertEqual(self.client.get('/api/team/knowledge/documents', params={'q': '%'}).json()['count'], 0)

    def test_repeat_import_is_idempotent_and_preserves_patient_data(self):
        before = self.client.get('/api/team/bootstrap').json()
        self.assertTrue(import_knowledge(self.app.state.engine)['duplicate'])
        after = self.client.get('/api/team/bootstrap').json()
        self.assertEqual(before, after)
        with Session(self.app.state.engine) as session:
            self.assertEqual(len(session.exec(select(KnowledgeRevision)).all()), 100)

    def test_changed_content_creates_revision_and_clears_review(self):
        _, rows = parse_source(SOURCE.read_bytes())
        row = dict(rows[0])
        with Session(self.app.state.engine) as session:
            document = session.get(KnowledgeDocument, row['id'])
            document.review_status = 'approved'
            document.retrieval_enabled = True
            document.source_url = 'https://example.invalid/test-only'
            session.add(document)
            session.commit()
        row['content'] += ' [테스트용 수정]'
        source = Path(self.temp.name) / 'changed.txt'
        source.write_text('medical_knowledge_docs = ' + json.dumps([row], ensure_ascii=False))
        import_knowledge(self.app.state.engine, source)
        saved = self.client.get(f"/api/team/knowledge/documents/{row['id']}").json()
        self.assertEqual(saved['review_status'], 'pending_review')
        self.assertFalse(saved['retrieval_enabled'])
        with Session(self.app.state.engine) as session:
            versions = session.exec(select(KnowledgeRevision).where(KnowledgeRevision.document_id == row['id'])).all()
            self.assertEqual(len(versions), 2)
            self.assertTrue(any(v.snapshot['content'] == rows[0]['content'] for v in versions))

    def test_attachment_is_never_executed_and_duplicate_ids_rejected(self):
        for source in [b'medical_knowledge_docs = __import__("os").getcwd()',
                       b'import os\nmedical_knowledge_docs = []',
                       b'medical_knowledge_docs = [{"id":"emerg_1","category":"a","content":"a"},{"id":"emerg_1","category":"b","content":"b"}]']:
            with self.assertRaises((ValueError, SyntaxError)):
                parse_source(source)


if __name__ == '__main__':
    unittest.main()
