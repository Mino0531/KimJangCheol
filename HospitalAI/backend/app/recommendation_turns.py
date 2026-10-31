"""Bind hospital-derived answers to the location permission that produced them."""

import sqlite3
from contextlib import closing

from app.db import Database
from app.recommendation_context import RecommendationContext


def initialize_context_bindings(con: sqlite3.Connection) -> None:
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    con.execute("""CREATE TABLE IF NOT EXISTS recommendation_turn_contexts (
        turn_id TEXT NOT NULL REFERENCES turns(id),
        event_id TEXT NOT NULL REFERENCES recommendation_outbox(event_id),
        generation INTEGER NOT NULL CHECK(generation > 0), PRIMARY KEY(turn_id,event_id))""")
    columns = {str(row["name"]): int(row["pk"]) for row in con.execute("PRAGMA table_info(recommendation_turn_contexts)")}
    if columns["event_id"] == 0:
        con.execute("ALTER TABLE recommendation_turn_contexts RENAME TO previous_recommendation_turn_contexts")
        con.execute("""CREATE TABLE recommendation_turn_contexts (
            turn_id TEXT NOT NULL REFERENCES turns(id),
            event_id TEXT NOT NULL REFERENCES recommendation_outbox(event_id),
            generation INTEGER NOT NULL CHECK(generation > 0), PRIMARY KEY(turn_id,event_id))""")
        con.execute("""INSERT INTO recommendation_turn_contexts(turn_id,event_id,generation)
            SELECT turn_id,event_id,generation FROM previous_recommendation_turn_contexts""")
        con.execute("DROP TABLE previous_recommendation_turn_contexts")
    # Older local builds did not record the permission generation. Their derived
    # answers cannot safely be assigned to a later location permission.
    legacy = con.execute("""SELECT t.id FROM turns t
        WHERE instr(COALESCE(t.sources_json,''),'clinic:recent-recommendation') > 0
        AND NOT EXISTS (SELECT 1 FROM recommendation_turn_contexts b WHERE b.turn_id=t.id)""").fetchall()
    for row in legacy:
        invalidate_context_turn(con, str(row["id"]))


def invalidate_context_turn(con: sqlite3.Connection, turn_id: str, reason: str = 'location_context_revoked') -> None:
    """Discard derived outputs while retaining the caller's words and active call."""
    con.execute("""UPDATE turns SET status='interrupted',initial_text=NULL,final_text=NULL,
        sources_json=NULL,tts_json=NULL,guidance_json=NULL,guidance_delivered=0,error=?,
        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=? AND status IN ('pending','processing','completed','degraded')""", (reason, turn_id))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generated_audio'").fetchone():
        con.execute("DELETE FROM generated_audio WHERE turn_id=?", (turn_id,))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='municipal_interruptions'").fetchone():
        con.execute("""INSERT OR IGNORE INTO municipal_interruptions(event_id)
            SELECT event_id FROM municipal_outbox WHERE event_id IN (?,?) AND state='pending'""",
            (f"turn:{turn_id}", f"playback:{turn_id}"))
        con.execute("""UPDATE municipal_outbox SET body=json_set(body,
            '$.assistant_text','','$.sources',json('[]')) WHERE event_id IN (?,?)
            AND state='pending' AND json_valid(body)""", (f"turn:{turn_id}", f"playback:{turn_id}"))


def context_turn_current(con: sqlite3.Connection, turn_id: str) -> bool:
    """Check under the caller's transaction before writes, audio, or outbox admission."""
    invalid_question = con.execute("""WITH RECURSIVE ancestors(id) AS (
        SELECT guidance_parent_id FROM turns WHERE id=? AND guidance_parent_id IS NOT NULL
        UNION SELECT t.guidance_parent_id FROM turns t JOIN ancestors a ON t.id=a.id
              WHERE t.guidance_parent_id IS NOT NULL)
        SELECT 1 FROM ancestors a LEFT JOIN turns p ON p.id=a.id
        WHERE p.id IS NULL OR p.status NOT IN ('completed','degraded') OR p.final_text IS NULL
           OR p.guidance_json IS NULL OR p.guidance_delivered != 1 LIMIT 1""", (turn_id,)).fetchone()
    if invalid_question is not None:
        invalidate_context_turn(con, turn_id, 'question_context_invalidated')
        return False
    invalid = con.execute("""SELECT 1 FROM recommendation_turn_contexts b
        WHERE b.turn_id=? AND NOT EXISTS (SELECT 1 FROM turns t JOIN calls c ON c.id=t.session_id
        JOIN call_location_consent l ON l.call_id=c.id
        JOIN recommendation_outbox o ON o.event_id=b.event_id AND o.call_id=c.id
        JOIN municipal_recommendation_generations g ON g.event_id=o.event_id
        WHERE t.id=b.turn_id AND l.state='accepted' AND l.generation=b.generation
        AND g.generation=b.generation
        AND NOT EXISTS (SELECT 1 FROM recommendation_cancellations x WHERE x.event_id=b.event_id)
        AND NOT EXISTS (SELECT 1 FROM recommendation_failures f WHERE f.event_id=b.event_id))""", (turn_id,)).fetchone()
    if invalid is None:
        return True
    invalidate_context_turn(con, turn_id)
    return False


def bind_context_turn(database: Database, turn_id: str, context: RecommendationContext) -> bool:
    """Atomically admit the exact event/generation that was passed to the model."""
    with closing(database.connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        return bind_context_in_transaction(con, turn_id, context)


def bind_context_in_transaction(con: sqlite3.Connection, turn_id: str, context: RecommendationContext) -> bool:
    con.execute("""INSERT OR IGNORE INTO recommendation_turn_contexts(turn_id,event_id,generation)
        SELECT t.id,?,? FROM turns t JOIN calls c ON c.id=t.session_id
        WHERE t.id=? AND t.status IN ('pending','processing')
        AND c.state='accepted' AND c.urgent=0 AND c.ended_at IS NULL""",
        (context.event_id, context.location_generation, turn_id))
    binding = con.execute("""SELECT 1 FROM recommendation_turn_contexts
        WHERE turn_id=? AND event_id=?""", (turn_id, context.event_id)).fetchone()
    return binding is not None and context_turn_current(con, turn_id)


def invalidate_location_contexts(con: sqlite3.Connection, call_id: str) -> None:
    """Revoke all bound outputs in the same transaction as location withdrawal."""
    from app.call_facts import invalidate_location_facts

    invalidate_location_facts(con, call_id)
    con.execute("UPDATE turns SET guidance_json=NULL,guidance_delivered=0 WHERE session_id=?", (call_id,))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_recommendation_navigation'").fetchone():
        con.execute("""UPDATE call_recommendation_navigation SET seen_json='[]', current_json=NULL,
            revision=revision+1 WHERE call_id=?""", (call_id,))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_recommendation_pending'").fetchone():
        con.execute("DELETE FROM call_recommendation_pending WHERE call_id=?", (call_id,))
    rows = con.execute("""SELECT DISTINCT b.turn_id FROM recommendation_turn_contexts b
        JOIN turns t ON t.id=b.turn_id WHERE t.session_id=?""", (call_id,)).fetchall()
    for row in rows:
        invalidate_context_turn(con, str(row["turn_id"]))


def municipal_context_current(con: sqlite3.Connection, event_id: str) -> bool:
    """Apply the same generation gate to an already queued answer or playback event."""
    row = con.execute("""SELECT t.id FROM turns t JOIN municipal_outbox o
        ON o.event_id=('turn:' || t.id) OR o.event_id=('playback:' || t.id)
        WHERE o.event_id=?""", (event_id,)).fetchone()
    return row is None or context_turn_current(con, str(row["id"]))
