"""SQLite audio output associated with a single consultation turn."""

from contextlib import closing
from dataclasses import dataclass, field

from app.db import Database


@dataclass(frozen=True, slots=True)
class Audio:
    data: bytes = field(repr=False)
    mime_type: str
    spoken_text: str | None = field(default=None, repr=False)


class AudioStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""CREATE TABLE IF NOT EXISTS generated_audio (
                turn_id TEXT NOT NULL REFERENCES turns(id),
                kind TEXT NOT NULL CHECK (kind IN ('low', 'final', 'emergency')),
                content BLOB NOT NULL, mime_type TEXT NOT NULL,
                PRIMARY KEY (turn_id, kind))""")
            columns = {row[1] for row in con.execute('PRAGMA table_info(generated_audio)')}
            if 'spoken_text' not in columns:
                con.execute('ALTER TABLE generated_audio ADD COLUMN spoken_text TEXT')

    def save(self, turn_id: str, kind: str, audio: Audio) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("""INSERT INTO generated_audio (turn_id, kind, content, mime_type, spoken_text)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(turn_id, kind)
                DO UPDATE SET content = excluded.content, mime_type = excluded.mime_type,
                              spoken_text = excluded.spoken_text""",
                (turn_id, kind, audio.data, audio.mime_type, audio.spoken_text))

    def get(self, turn_id: str, kind: str) -> Audio | None:
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT content, mime_type, spoken_text FROM generated_audio WHERE turn_id = ? AND kind = ?",
                              (turn_id, kind)).fetchone()
            return Audio(data=bytes(row[0]), mime_type=str(row[1]), spoken_text=row[2]) if row else None
