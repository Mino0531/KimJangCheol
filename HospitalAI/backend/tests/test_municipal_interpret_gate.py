from contextlib import closing
from pathlib import Path

from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalOutbox


def setup_outbox(path: Path):
    db = Database(path)
    db.initialize()
    store = CallStore(db)
    store.initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    return db, store, outbox


def accepted_call(db, store, outbox, phone):
    call_id = str(db.create_session(phone, None)['id'])
    store.create(call_id)
    store.decide(call_id, 'accepted')
    outbox.enqueue_start(call_id)
    return call_id


def test_pending_intent_blocks_only_same_call_until_gate_release(tmp_path: Path):
    db, store, outbox = setup_outbox(tmp_path / 'gate.sqlite')
    first = accepted_call(db, store, outbox, '+821000000001')
    second = accepted_call(db, store, outbox, '+821000000002')
    token = outbox.begin_interpretation(first)
    assert token
    assert outbox._next()[1] == second
    assert not outbox._eligible(f'start:{first}', first)
    outbox.end_interpretation(token)
    assert outbox._eligible(f'start:{first}', first)


def test_urgent_bypasses_gate_and_other_lease_remains(tmp_path: Path):
    db, store, outbox = setup_outbox(tmp_path / 'urgent.sqlite')
    call_id = accepted_call(db, store, outbox, '+821000000001')
    first = outbox.begin_interpretation(call_id)
    second = outbox.begin_interpretation(call_id)
    outbox.end_interpretation(first)
    assert not outbox._eligible(f'start:{call_id}', call_id)
    store.mark_urgent(call_id)
    outbox.enqueue_urgent(call_id)
    assert outbox._eligible(f'urgent:{call_id}', call_id)
    assert outbox._next()[0] == f'urgent:{call_id}'
    outbox.end_interpretation(second)


def test_stale_gate_expires_and_unaccepted_call_cannot_open_gate(tmp_path: Path):
    db, store, outbox = setup_outbox(tmp_path / 'expire.sqlite')
    call_id = accepted_call(db, store, outbox, '+821000000001')
    token = outbox.begin_interpretation(call_id)
    assert not outbox._eligible(f'start:{call_id}', call_id)
    with closing(db.connect()) as con, con:
        con.execute('UPDATE municipal_interpretation_gates SET expires_at=0 WHERE id=?', (token,))
    assert outbox._eligible(f'start:{call_id}', call_id)
    with closing(db.connect()) as con, con:
        con.execute("UPDATE calls SET state='declined' WHERE id=?", (call_id,))
    assert outbox.begin_interpretation(call_id) is None
