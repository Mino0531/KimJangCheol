"""Guard candidate chart writes when a call's patient identity is uncertain."""

import sqlite3


def session_subject_is_safe(con: sqlite3.Connection, session_id: str) -> bool:
    """Reject changed, unresolved, or legacy voice-call patient subjects."""
    names = {row[0] for row in con.execute("""SELECT name FROM sqlite_master
        WHERE type='table' AND name IN ('calls','pending_utterances','call_fact_subject_boundaries','call_location_resets')""")}
    if 'calls' not in names:
        return True
    call_columns = {row[1] for row in con.execute('PRAGMA table_info(calls)')}
    if not {'state', 'ended_at'} <= call_columns:
        # Other, non-call sessions in a legacy database remain usable.
        return con.execute('SELECT 1 FROM calls WHERE id=?', (session_id,)).fetchone() is None
    call = con.execute('SELECT state,ended_at FROM calls WHERE id=?', (session_id,)).fetchone()
    if call is None:
        return True
    if call['state'] != 'accepted' or call['ended_at'] is None or not {'calls', 'pending_utterances', 'call_fact_subject_boundaries'} <= names:
        return False
    if con.execute('SELECT 1 FROM call_fact_subject_boundaries WHERE call_id=? LIMIT 1',
                   (session_id,)).fetchone():
        return False
    if 'call_location_resets' in names and con.execute('SELECT 1 FROM call_location_resets WHERE call_id=? LIMIT 1',
                                                        (session_id,)).fetchone():
        return False
    if con.execute("SELECT 1 FROM pending_utterances WHERE call_id=? AND state='pending' LIMIT 1",
                   (session_id,)).fetchone():
        return False
    utterance_columns = {row[1] for row in con.execute('PRAGMA table_info(pending_utterances)')}
    turn_columns = {row[1] for row in con.execute('PRAGMA table_info(turns)')}
    if 'user_location_generation' not in utterance_columns or 'user_location_generation' not in turn_columns:
        return False
    # Private-mode speech is deliberately not interpreted by the external LLM;
    # therefore its patient subject cannot safely be linked to the caller's chart.
    if con.execute("""SELECT 1 FROM pending_utterances WHERE call_id=? AND state='accepted'
        AND (user_location_generation IS NULL OR user_location_generation=0) LIMIT 1""", (session_id,)).fetchone():
        return False
    if con.execute("""SELECT 1 FROM turns t WHERE t.session_id=? AND
        (t.user_location_generation=0 OR (t.user_location_generation IS NULL AND NOT EXISTS (
            SELECT 1 FROM pending_utterances u WHERE u.call_id=t.session_id AND u.request_id=t.request_id
            AND u.state='accepted' AND u.user_location_generation>0))) LIMIT 1""", (session_id,)).fetchone():
        return False
    return con.execute("""SELECT 1 FROM pending_utterances WHERE call_id=?
        AND state='accepted' AND intent='consultation' LIMIT 1""", (session_id,)).fetchone() is not None


def candidate_subject_is_safe(con: sqlite3.Connection, turn_id: str) -> bool:
    """Check the call subject and a candidate's source in the same transaction."""
    turn = con.execute('SELECT session_id,request_id FROM turns WHERE id=?', (turn_id,)).fetchone()
    if turn is None or not session_subject_is_safe(con, str(turn['session_id'])):
        return False
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone():
        return True
    if not con.execute('SELECT 1 FROM calls WHERE id=?', (turn['session_id'],)).fetchone():
        return True
    return con.execute("""SELECT 1 FROM pending_utterances WHERE call_id=? AND request_id=?
        AND state='accepted' AND intent='consultation'""", (turn['session_id'], turn['request_id'])).fetchone() is not None
