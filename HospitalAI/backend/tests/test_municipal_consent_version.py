from pathlib import Path

from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalOutbox


def test_new_calls_record_municipality_consent_notice_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "calls.sqlite")
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    session = database.create_session("+821000000001")
    call_id = str(session["id"])

    calls.create(call_id)

    assert calls.get(call_id)["consent_version"] == "municipality-v3"


def test_prior_hospital_only_consent_is_not_transferred_to_municipality(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.sqlite")
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    outbox = MunicipalOutbox(database)
    outbox.initialize()
    session = database.create_session("+821000000001")
    call_id = str(session["id"])
    calls.create(call_id)
    with database.connect() as con:
        con.execute("UPDATE calls SET state='accepted', consent_version='prototype-v1' WHERE id=?", (call_id,))
        con.commit()

    outbox.recover()

    with database.connect() as con:
        assert con.execute("SELECT count(*) FROM municipal_outbox").fetchone()[0] == 0
