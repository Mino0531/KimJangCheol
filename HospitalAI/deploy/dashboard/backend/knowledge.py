"""Store supplied RAG source material without executing it or approving its medical claims."""
import ast
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Query
from sqlalchemy import Column, JSON, UniqueConstraint, or_
from sqlmodel import Field, Session, SQLModel, select

SOURCE = Path(__file__).parent / 'fixtures/rag/medical-knowledge-source.txt'
EXTRA_SOURCE = SOURCE.parent / 'visit-preparation-extra.txt'
GROUPS = {'emerg': '농어촌 응급·외상', 'freq': '외래·방문 준비', 'spec': '중증·감염·특화', 'guide': '의료 이용 안내'}


class KnowledgeImport(SQLModel, table=True):
    __tablename__ = 'rag_imports'
    id: str = Field(primary_key=True)
    source_name: str
    imported_at: str
    document_count: int
    original_source: str


class KnowledgeDocument(SQLModel, table=True):
    __tablename__ = 'rag_documents'
    id: str = Field(primary_key=True)
    group_code: str = Field(index=True)
    category: str = Field(index=True)
    content: str
    keywords: list[str] = Field(sa_column=Column(JSON, nullable=False))
    content_sha256: str
    current_import_id: str = Field(foreign_key='rag_imports.id')
    source_kind: str = 'user_supplied'
    source_url: str | None = None
    review_status: str = Field(default='pending_review', index=True)
    verified_at: str | None = None
    retrieval_enabled: bool = False
    created_at: str
    updated_at: str


class KnowledgeRevision(SQLModel, table=True):
    __tablename__ = 'rag_document_versions'
    __table_args__ = (UniqueConstraint('document_id', 'import_id'),)
    id: str = Field(primary_key=True)
    document_id: str = Field(foreign_key='rag_documents.id', index=True)
    import_id: str = Field(foreign_key='rag_imports.id')
    imported_at: str
    snapshot: dict = Field(sa_column=Column(JSON, nullable=False))


class KnowledgeChunk(SQLModel, table=True):
    __tablename__ = 'rag_chunks'
    __table_args__ = (UniqueConstraint('document_id', 'ordinal'),)
    id: str = Field(primary_key=True)
    document_id: str = Field(foreign_key='rag_documents.id', index=True)
    ordinal: int = 0
    text: str
    content_sha256: str
    embedding_status: str = 'not_generated'


def parse_source(raw: bytes):
    """Only accept the expected literal list assignment; never exec/eval the attachment."""
    text = raw.decode('utf-8-sig')
    tree = ast.parse(text)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        raise ValueError('medical_knowledge_docs 리터럴 할당 하나만 허용합니다.')
    assignment = tree.body[0]
    if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name) or assignment.targets[0].id != 'medical_knowledge_docs':
        raise ValueError('medical_knowledge_docs 변수만 허용합니다.')
    rows = ast.literal_eval(assignment.value)
    if not isinstance(rows, list) or not rows:
        raise ValueError('비어 있지 않은 문서 목록이 필요합니다.')
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'category', 'content'}:
            raise ValueError('각 문서는 id/category/content 필드만 가져야 합니다.')
        if not all(isinstance(v, str) and v.strip() for v in row.values()):
            raise ValueError('문서 필드는 비어 있지 않은 문자열이어야 합니다.')
        if row['id'] in ids:
            raise ValueError('문서 ID가 중복됐습니다.')
        if row['id'].split('_')[0] not in GROUPS:
            raise ValueError('알 수 없는 그룹입니다.')
        ids.add(row['id'])
    return text, rows


def extract_keywords(content):
    head = content.split('해결방안 및 준비물:', 1)[0]
    if not head.startswith('관련단어:'):
        return []
    return [word.strip().rstrip('.') for word in head[len('관련단어:'):].split(',') if word.strip().rstrip('.')]


def import_knowledge(engine, source=SOURCE, source_kind='user_supplied'):
    raw = Path(source).read_bytes()
    original, rows = parse_source(raw)
    digest = hashlib.sha256(raw).hexdigest()
    at = datetime.now(timezone.utc).isoformat()
    with Session(engine) as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        if session.get(KnowledgeImport, digest):
            return {'applied': False, 'duplicate': True, 'document_count': len(rows), 'import_id': digest}
        session.add(KnowledgeImport(id=digest, source_name=Path(source).name, imported_at=at,
                                    document_count=len(rows), original_source=original))
        session.flush()
        for source_row in rows:
            row = dict(source_row)
            document = session.get(KnowledgeDocument, row['id'])
            content_hash = hashlib.sha256(row['content'].encode()).hexdigest()
            keywords = extract_keywords(row['content'])
            if document and document.content_sha256 == content_hash and document.category == row['category']:
                # Preserve review metadata of unchanged content.
                document.current_import_id = digest
            elif document:
                document.category = row['category']
                document.content = row['content']
                document.keywords = keywords
                document.content_sha256 = content_hash
                document.current_import_id = digest
                document.source_url = None
                document.review_status = 'pending_review'
                document.verified_at = None
                document.retrieval_enabled = False
                document.source_kind = source_kind
                document.updated_at = at
            else:
                document = KnowledgeDocument(**row, group_code=row['id'].split('_')[0], keywords=keywords,
                    content_sha256=content_hash, current_import_id=digest, source_kind=source_kind, created_at=at, updated_at=at)
            session.add(document)
            session.flush()
            chunk_id = f"{row['id']}:0"
            chunk = session.get(KnowledgeChunk, chunk_id)
            if chunk:
                if chunk.content_sha256 != content_hash:
                    chunk.text = row['content']
                    chunk.content_sha256 = content_hash
                    chunk.embedding_status = 'not_generated'
            else:
                chunk = KnowledgeChunk(id=chunk_id, document_id=row['id'], text=row['content'], content_sha256=content_hash)
            session.add(chunk)
            session.add(KnowledgeRevision(id=f"{digest}:{row['id']}", document_id=row['id'], import_id=digest,
                                          imported_at=at, snapshot=document.model_dump()))
        session.commit()
    return {'applied': True, 'duplicate': False, 'document_count': len(rows), 'import_id': digest}


def register_knowledge_routes(app, engine):
    @app.get('/api/team/knowledge/stats')
    def stats():
        with Session(engine) as session:
            docs = session.exec(select(KnowledgeDocument)).all()
            from backend.preparation import PreparationPolicy, VocabularyTerm
            return {'document_count': len(docs), 'groups': {g: sum(d.group_code == g for d in docs) for g in GROUPS},
                    'original_documents': sum(d.source_kind == 'user_supplied' for d in docs),
                    'service_authored_documents': sum(d.source_kind == 'service_authored' for d in docs),
                    'pending_review': sum(d.review_status == 'pending_review' for d in docs),
                    'retrieval_enabled': sum(d.retrieval_enabled for d in docs),
                    'chunks': len(session.exec(select(KnowledgeChunk)).all()), 'embedding_status': 'not_generated',
                    'answer_generation_connected': False,
                    'visit_preparation_policies': len(session.exec(select(PreparationPolicy)).all()),
                    'vocabulary_terms': len(session.exec(select(VocabularyTerm)).all())}

    @app.get('/api/team/knowledge/documents')
    def list_documents(q: str = Query('', max_length=200), group: Literal['emerg', 'freq', 'spec', 'guide'] | None = None,
                       limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        with Session(engine) as session:
            query = select(KnowledgeDocument)
            if group:
                query = query.where(KnowledgeDocument.group_code == group)
            if q.strip():
                term = q.strip()
                query = query.where(or_(KnowledgeDocument.category.contains(term, autoescape=True),
                                        KnowledgeDocument.content.contains(term, autoescape=True)))
            rows = session.exec(query.order_by(KnowledgeDocument.id).offset(offset).limit(limit)).all()
            return {'purpose': 'source_review', 'items': [d.model_dump() for d in rows], 'count': len(rows)}

    @app.get('/api/team/knowledge/documents/{document_id}')
    def detail(document_id: str):
        with Session(engine) as session:
            document = session.get(KnowledgeDocument, document_id)
            if not document:
                raise HTTPException(404, '지식 문서가 없습니다.')
            return document.model_dump()

    @app.get('/api/team/knowledge/retrieve')
    def retrieve(q: str = Query(min_length=1, max_length=200), limit: int = Query(5, ge=1, le=20)):
        term = q.strip()
        if not term:
            raise HTTPException(422, '검색어를 입력해 주세요.')
        with Session(engine) as session:
            query = select(KnowledgeDocument).where(
                KnowledgeDocument.retrieval_enabled == True, KnowledgeDocument.review_status == 'approved',
                KnowledgeDocument.source_url.is_not(None),
                or_(KnowledgeDocument.category.contains(term, autoescape=True), KnowledgeDocument.content.contains(term, autoescape=True)))
            rows = session.exec(query.order_by(KnowledgeDocument.id).limit(limit)).all()
            return {'method': 'lexical_substring', 'items': [d.model_dump() for d in rows],
                    'count': len(rows), 'answer_generation_connected': False,
                    'status': 'ready' if rows else 'no_approved_matching_documents'}
