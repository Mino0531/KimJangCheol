"""Safety checks for the provided development knowledge and urgent symptoms."""

from pathlib import Path

from app.call_search import CallDatabase
from app.db import Database
from app.services import emergency_detected


def test_unreviewed_knowledge_disabled_for_production_search(tmp_path: Path) -> None:
    database = Database(tmp_path / "calls.sqlite")
    database.initialize()
    knowledge = Path(__file__).resolve().parent.parent / "data" / "call_knowledge.json"
    production = CallDatabase(database, knowledge, allow_unreviewed=False)
    assert production.search(None, "기침이 있어요") == []


def test_user_pesticide_example_is_prioritized() -> None:
    assert emergency_detected("농약을 어제 뿌렸는데 어질어질하다")
