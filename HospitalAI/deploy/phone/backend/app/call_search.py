"""Call-specific retrieval over the user-supplied file and verified patient records."""

from pathlib import Path

from app.call_knowledge import KnowledgeBase
from app.db import Database, Row


class CallDatabase(Database):
    def __init__(self, database: Database, path: Path, *, allow_unreviewed: bool = False) -> None:
        super().__init__(database.path)
        self.knowledge = KnowledgeBase(path)
        self.allow_unreviewed = allow_unreviewed

    def search(self, patient_id: str | None, query: str) -> list[Row]:
        sources: list[Row] = list(self.knowledge.search(query)) if self.allow_unreviewed else []
        if patient_id is not None:
            sources.extend(row for row in super().search(patient_id, query) if row.get("type") == "patient")
        return sources[:8]
