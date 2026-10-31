"""Same-call recommendation navigation scoped to current location consent."""

import json
from contextlib import closing
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from app.db import Database, Row
from app.recommendation import RecommendationResult


def only_acknowledgements_since(history: list[Row], last_turn_id: str | None) -> bool:
    """Never inherit a tentative specialty over new substantive caller input."""
    if last_turn_id is None:
        return not history
    index = next((i for i, row in enumerate(history) if row.get("id") == last_turn_id), None)
    if index is None:
        return False
    acknowledgements = {"네", "예", "응", "그래요", "알겠어요", "고마워요", "감사합니다"}
    return all("".join(str(row.get("input_text") or "").split()).rstrip(".?!") in acknowledgements
               for row in history[index + 1:])


@dataclass(frozen=True, slots=True)
class NavigationSnapshot:
    chosen_specialty: str | None
    selected_specialty: str | None
    selection_source: Literal["chosen", "suggested", "unknown"]
    last_turn_id: str | None
    revision: int
    generation: int
    seen: tuple[str, ...]
    current: RecommendationResult | None


def snapshot(database: Database, call_id: str, generation: int) -> NavigationSnapshot:
    """Load previous choices without reviving an earlier location generation."""
    with closing(database.connect()) as con, con:
        con.execute("""CREATE TABLE IF NOT EXISTS call_recommendation_navigation (
            call_id TEXT PRIMARY KEY REFERENCES calls(id), chosen_specialty TEXT,
            revision INTEGER NOT NULL, generation INTEGER NOT NULL,
            seen_json TEXT NOT NULL, current_json TEXT)""")
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(call_recommendation_navigation)")}
        for column in ("selected_specialty", "selection_source", "last_turn_id"):
            if column not in columns:
                con.execute(f"ALTER TABLE call_recommendation_navigation ADD COLUMN {column} TEXT")
        row = con.execute("SELECT * FROM call_recommendation_navigation WHERE call_id=?", (call_id,)).fetchone()
        if row is None:
            return NavigationSnapshot(None, None, "unknown", None, 0, generation, (), None)
        chosen, revision = row["chosen_specialty"], int(row["revision"])
        if row["generation"] != generation:
            return NavigationSnapshot(chosen, None, "unknown", None, revision, generation, (), None)
        source = row["selection_source"] if row["selection_source"] in ("chosen", "suggested", "unknown") else "unknown"
        selected, last_turn = row["selected_specialty"], row["last_turn_id"]
        try:
            seen = json.loads(row["seen_json"])
            current = RecommendationResult.model_validate_json(row["current_json"]) if row["current_json"] else None
            if not isinstance(seen, list) or any(not isinstance(item, str) for item in seen):
                return NavigationSnapshot(chosen, None, "unknown", None, revision, generation, (), None)
        except (json.JSONDecodeError, ValidationError):
            return NavigationSnapshot(chosen, None, "unknown", None, revision, generation, (), None)
        return NavigationSnapshot(chosen, selected, source, last_turn, revision, generation, tuple(seen), current)


def save(database: Database, call_id: str, previous: NavigationSnapshot, chosen: str | None,
         selected: str | None, source: str, last_turn_id: str | None,
         seen: tuple[str, ...], current: RecommendationResult | None) -> bool:
    """Commit only while consent and the optimistic navigation revision remain current."""
    with closing(database.connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute("""SELECT 1 FROM calls c JOIN call_location_consent l ON l.call_id=c.id
            WHERE c.id=? AND c.state='accepted' AND c.urgent=0 AND c.ended_at IS NULL
            AND l.state='accepted' AND l.generation=?
            AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=c.id
                            AND t.status IN ('pending','processing','emergency'))""",
            (call_id, previous.generation)).fetchone()
        if active is None:
            return False
        existing = con.execute("SELECT revision FROM call_recommendation_navigation WHERE call_id=?", (call_id,)).fetchone()
        if (int(existing[0]) if existing else 0) != previous.revision:
            return False
        con.execute("""INSERT INTO call_recommendation_navigation
            (call_id,chosen_specialty,revision,generation,seen_json,current_json,selected_specialty,selection_source,last_turn_id)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(call_id) DO UPDATE SET chosen_specialty=excluded.chosen_specialty,
            revision=excluded.revision,generation=excluded.generation,seen_json=excluded.seen_json,
            current_json=excluded.current_json,selected_specialty=excluded.selected_specialty,
            selection_source=excluded.selection_source,last_turn_id=excluded.last_turn_id""",
            (call_id, chosen, previous.revision + 1, previous.generation, json.dumps(seen),
             current.model_dump_json() if current is not None else None, selected, source, last_turn_id))
        return True


@dataclass(frozen=True, slots=True)
class ConfirmedNavigation:
    outcome: Literal["confirmed", "duplicate", "conflict", "missing"]
    result: RecommendationResult | None = None
    specialty: str | None = None
    generation: int = 0


def stage(database: Database, call_id: str, previous: NavigationSnapshot, chosen: str | None,
          selected: str | None, source: str, last_turn_id: str | None,
          seen: tuple[str, ...], current: RecommendationResult) -> str | None:
    """Keep the candidate pending until playback completes."""
    with closing(database.connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("""CREATE TABLE IF NOT EXISTS call_recommendation_pending (
            token TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id), generation INTEGER NOT NULL,
            revision INTEGER NOT NULL, chosen_specialty TEXT, selected_specialty TEXT, seen_json TEXT NOT NULL,
            result_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')""")
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(call_recommendation_pending)")}
        for column in ("selected_specialty", "selection_source", "last_turn_id"):
            if column not in columns:
                con.execute(f"ALTER TABLE call_recommendation_pending ADD COLUMN {column} TEXT")
        active = con.execute("""SELECT 1 FROM calls c JOIN call_location_consent l ON l.call_id=c.id
            WHERE c.id=? AND c.state='accepted' AND c.urgent=0 AND c.ended_at IS NULL
            AND l.state='accepted' AND l.generation=?
            AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=c.id
                            AND t.status IN ('pending','processing','emergency'))""",
            (call_id, previous.generation)).fetchone()
        row = con.execute("SELECT revision FROM call_recommendation_navigation WHERE call_id=?", (call_id,)).fetchone()
        if active is None or (int(row[0]) if row else 0) != previous.revision:
            return None
        token = uuid4().hex
        con.execute("""INSERT INTO call_recommendation_pending
            (token,call_id,generation,revision,chosen_specialty,selected_specialty,selection_source,last_turn_id,seen_json,result_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (token, call_id, previous.generation, previous.revision, chosen, selected,
                                          source, last_turn_id, json.dumps(seen), current.model_dump_json()))
        return token


def confirm(database: Database, call_id: str, token: str) -> ConfirmedNavigation:
    """Make playback receipt and the matching navigation update atomic."""
    with closing(database.connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_recommendation_pending'").fetchone()
        if table is None:
            return ConfirmedNavigation("missing")
        pending = con.execute("SELECT * FROM call_recommendation_pending WHERE token=? AND call_id=?",
                              (token, call_id)).fetchone()
        if pending is None:
            return ConfirmedNavigation("missing")
        try:
            result = RecommendationResult.model_validate_json(pending["result_json"])
        except ValidationError:
            return ConfirmedNavigation("conflict")
        data = {"result": result, "specialty": pending["selected_specialty"], "generation": int(pending["generation"])}
        if pending["status"] == "confirmed":
            return ConfirmedNavigation("duplicate", **data)
        active = con.execute("""SELECT 1 FROM calls c JOIN call_location_consent l ON l.call_id=c.id
            WHERE c.id=? AND c.state='accepted' AND c.urgent=0 AND c.ended_at IS NULL
            AND l.state='accepted' AND l.generation=?
            AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=c.id
                            AND t.status IN ('pending','processing','emergency'))""",
            (call_id, pending["generation"])).fetchone()
        current = con.execute("SELECT revision FROM call_recommendation_navigation WHERE call_id=?", (call_id,)).fetchone()
        if active is None or (int(current[0]) if current else 0) != pending["revision"]:
            return ConfirmedNavigation("conflict")
        con.execute("""INSERT INTO call_recommendation_navigation
            (call_id,chosen_specialty,revision,generation,seen_json,current_json,selected_specialty,selection_source,last_turn_id)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(call_id) DO UPDATE SET chosen_specialty=excluded.chosen_specialty,
            revision=excluded.revision,generation=excluded.generation,seen_json=excluded.seen_json,
            current_json=excluded.current_json,selected_specialty=excluded.selected_specialty,
            selection_source=excluded.selection_source,last_turn_id=excluded.last_turn_id""",
            (call_id, pending["chosen_specialty"], pending["revision"] + 1, pending["generation"],
             pending["seen_json"], pending["result_json"], pending["selected_specialty"],
             pending["selection_source"], pending["last_turn_id"]))
        con.execute("UPDATE call_recommendation_pending SET status='confirmed' WHERE token=?", (token,))
        return ConfirmedNavigation("confirmed", **data)
