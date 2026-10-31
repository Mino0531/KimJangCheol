"""Admit copied conversation history under the current consent transaction."""

from contextlib import closing
from dataclasses import dataclass

from app.db import Database, Row
from app.call_facts import FactSnapshot, read_snapshot
from app.recommendation_context import RecommendationContext, load_recommendation_context
from app.recommendation_turns import bind_context_in_transaction, context_turn_current
from app.utterance_location import user_generation_visible, validate_source_generation


@dataclass(frozen=True, slots=True)
class ConversationScope:
    call_id: str
    current_turn_id: str | None = None
    utterance_id: str | None = None
    source_location_generation: int | None = None
    source_location_revision: int | None = None
    enforce_source_generation: bool = False
    include_all_facts: bool = False


@dataclass(frozen=True, slots=True)
class AdmittedConversation:
    history: list[Row]
    recommendation: RecommendationContext | None
    active: bool
    facts: FactSnapshot | None = None
    location_generation: int | None = None
    utterance_visible: bool = True
    patient_subject_changed: bool = False


def admit_conversation(database: Database, scope: ConversationScope, copied_history: list[Row]) -> AdmittedConversation:
    """Refresh assistant outputs and bind their dependencies before model dispatch."""
    with closing(database.connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        call = con.execute("SELECT state,urgent,ended_at FROM calls WHERE id=?", (scope.call_id,)).fetchone()
        if call is None or call["state"] != "accepted" or call["urgent"] or call["ended_at"]:
            return AdmittedConversation([], None, False)
        if scope.enforce_source_generation and not validate_source_generation(
                con, scope.call_id, scope.source_location_generation, scope.source_location_revision):
            return AdmittedConversation([], None, False)
        current_rows = {str(row["id"]): dict(row) for row in con.execute(
            "SELECT * FROM turns WHERE session_id=?", (scope.call_id,))}
        admitted: list[Row] = []
        for copied in copied_history:
            row = current_rows.get(str(copied["id"]))
            if row is None or copied.get("session_id") != scope.call_id:
                continue
            if not user_generation_visible(con, scope.call_id, row.get('user_location_generation')):
                row = {**row, 'input_text': '', 'final_text': None, 'sources_json': None, 'guidance_json': None, 'guidance_delivered': 0}
            if not context_turn_current(con, str(row["id"])) and row["status"] in ("completed", "degraded"):
                row = {**row, "status": "interrupted", "final_text": None, "sources_json": None, "guidance_json": None, "guidance_delivered": 0}
            admitted.append(row)
            if scope.current_turn_id == row["id"]:
                break
            if scope.current_turn_id is not None and row["status"] in ("completed", "degraded"):
                con.execute("""INSERT OR IGNORE INTO recommendation_turn_contexts(turn_id,event_id,generation)
                    SELECT ?,event_id,generation FROM recommendation_turn_contexts WHERE turn_id=?""",
                    (scope.current_turn_id, row["id"]))
        recommendation = load_recommendation_context(database, scope.call_id, connection=con)
        facts = (read_snapshot(database, scope.call_id, through_utterance_id=scope.utterance_id, connection=con)
                 if scope.utterance_id is not None or scope.include_all_facts else None)
        location = con.execute("SELECT state,generation FROM call_location_consent WHERE call_id=?", (scope.call_id,)).fetchone()
        generation = int(location['generation']) if location is not None and location['state'] == 'accepted' else None
        visible = True
        if scope.utterance_id is not None:
            columns = {row['name'] for row in con.execute('PRAGMA table_info(pending_utterances)')}
            if 'user_location_generation' in columns:
                utterance = con.execute('SELECT user_location_generation FROM pending_utterances WHERE call_id=? AND request_id=?',
                                        (scope.call_id, scope.utterance_id)).fetchone()
                if utterance is not None:
                    visible = user_generation_visible(con, scope.call_id, utterance['user_location_generation'])
        reset_table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_location_resets'").fetchone()
        subject_changed = bool(reset_table and con.execute('SELECT 1 FROM call_location_resets WHERE call_id=? LIMIT 1',
                                                           (scope.call_id,)).fetchone())
        # Local-only conversations cannot determine whether a different patient
        # was mentioned. Never reuse the original caller's chart afterwards.
        turn_columns = {row['name'] for row in con.execute('PRAGMA table_info(turns)')}
        if 'user_location_generation' in turn_columns:
            subject_changed = subject_changed or bool(con.execute(
                'SELECT 1 FROM turns WHERE session_id=? AND user_location_generation=0 LIMIT 1',
                (scope.call_id,)).fetchone())
        pending_columns = {row['name'] for row in con.execute('PRAGMA table_info(pending_utterances)')}
        if 'user_location_generation' in pending_columns:
            subject_changed = subject_changed or bool(con.execute(
                "SELECT 1 FROM pending_utterances WHERE call_id=? AND state='accepted' AND user_location_generation=0 LIMIT 1",
                (scope.call_id,)).fetchone())
        active = True
        if scope.current_turn_id is not None:
            if recommendation is not None:
                active = bind_context_in_transaction(con, scope.current_turn_id, recommendation)
            active = active and context_turn_current(con, scope.current_turn_id)
        return AdmittedConversation(admitted, recommendation, active, facts, generation, visible, subject_changed)
