"""Request revision distinguishes separate periods without location permission."""

import sqlite3
from contextlib import closing
from pathlib import Path

from app import utterance_location


def test_old_unlocated_request_is_rejected_after_second_withdrawal() -> None:
    with closing(sqlite3.connect(':memory:')) as con:
        con.execute('CREATE TABLE call_location_consent(call_id TEXT, state TEXT, generation INTEGER)')
        con.execute('CREATE TABLE location_consent_events(id INTEGER PRIMARY KEY, call_id TEXT, state TEXT, generation INTEGER)')
        con.execute("INSERT INTO call_location_consent VALUES ('call','revoked',2)")
        con.executemany('INSERT INTO location_consent_events(call_id,state,generation) VALUES (?,?,?)', [
            ('call', 'accepted', 1), ('call', 'revoked', 1), ('call', 'accepted', 2), ('call', 'revoked', 2),
        ])
        assert not utterance_location.validate_source_generation(con, 'call', 0, 2)
        assert not utterance_location.validate_source_generation(con, 'call', 0, None)
        assert utterance_location.validate_source_generation(con, 'call', 0, 4)


def test_legacy_unlocated_requests_require_revision_after_withdrawal() -> None:
    with closing(sqlite3.connect(':memory:')) as con:
        con.execute('CREATE TABLE call_location_consent(call_id TEXT, state TEXT, generation INTEGER)')
        con.execute('CREATE TABLE location_consent_events(id INTEGER PRIMARY KEY, call_id TEXT, state TEXT, generation INTEGER)')
        con.execute("INSERT INTO call_location_consent VALUES ('call','revoked',1)")
        con.executemany('INSERT INTO location_consent_events(call_id,state,generation) VALUES (?,?,?)', [
            ('call', 'accepted', 1), ('call', 'revoked', 1),
        ])
        assert not utterance_location.validate_source_generation(con, 'call', 0, None)
        assert utterance_location.validate_source_generation(con, 'call', 0, utterance_location.capture_location_revision(con, 'call'))


def test_initial_unlocated_legacy_request_remains_compatible() -> None:
    with closing(sqlite3.connect(':memory:')) as con:
        con.execute('CREATE TABLE call_location_consent(call_id TEXT, state TEXT, generation INTEGER)')
        con.execute('CREATE TABLE location_consent_events(id INTEGER PRIMARY KEY, call_id TEXT, state TEXT, generation INTEGER)')
        assert utterance_location.validate_source_generation(con, 'call', 0, None)


def test_legacy_missing_location_table_still_rejects_ambiguous_epoch() -> None:
    with closing(sqlite3.connect(':memory:')) as con:
        con.execute('CREATE TABLE location_consent_events(id INTEGER PRIMARY KEY, call_id TEXT, state TEXT, generation INTEGER)')
        con.execute("INSERT INTO location_consent_events(call_id,state,generation) VALUES ('call','revoked',1)")
        assert not utterance_location.validate_source_generation(con, 'call', 0, None)


def test_validation_does_not_combine_different_consent_snapshots(tmp_path: Path) -> None:
    path = tmp_path / 'epochs.sqlite'
    with closing(sqlite3.connect(path, isolation_level=None)) as reader, closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute('CREATE TABLE call_location_consent(call_id TEXT, state TEXT, generation INTEGER)')
        writer.execute('CREATE TABLE location_consent_events(id INTEGER PRIMARY KEY, call_id TEXT, state TEXT, generation INTEGER)')
        writer.execute("INSERT INTO call_location_consent VALUES ('call','revoked',1)")
        writer.executemany('INSERT INTO location_consent_events(call_id,state,generation) VALUES (?,?,?)', [
            ('call', 'accepted', 1), ('call', 'revoked', 1),
        ])
        changed = False

        def interleave(statement: str) -> None:
            nonlocal changed
            if not changed and 'SELECT generation FROM call_location_consent' in statement:
                changed = True
                writer.execute("UPDATE call_location_consent SET state='accepted', generation=2 WHERE call_id='call'")
                writer.execute("INSERT INTO location_consent_events(call_id,state,generation) VALUES ('call','accepted',2)")
                writer.execute("UPDATE call_location_consent SET state='revoked' WHERE call_id='call'")
                writer.execute("INSERT INTO location_consent_events(call_id,state,generation) VALUES ('call','revoked',2)")

        reader.set_trace_callback(interleave)
        assert not utterance_location.validate_source_generation(reader, 'call', 0, 2)
        assert changed
