"""Recover recommendation playback confirmations after an interrupted enqueue."""

from contextlib import closing
import logging

from pydantic import ValidationError

from app.db import Database, DuplicateRequest
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation import RecommendationResult


logger = logging.getLogger(__name__)


def recover_confirmed_recommendations(database: Database) -> int:
    """Retry only same-generation, confirmed snapshots missing an outbox entry.

    Enqueue rechecks consent, emergency state and pending turns in its own write
    transaction, so a withdrawal after this read cannot revive a snapshot.
    """
    with closing(database.connect()) as con:
        exists = con.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='call_recommendation_pending'""").fetchone()
        if not exists:
            return 0
        columns = {str(row[1]) for row in con.execute('PRAGMA table_info(call_recommendation_pending)')}
        if 'selected_specialty' not in columns:
            logger.warning('Confirmed recommendation recovery awaits pending-table migration')
            return 0
        rows = con.execute("""SELECT p.token,p.call_id,p.generation,p.selected_specialty,p.result_json
            FROM call_recommendation_pending p
            JOIN calls c ON c.id=p.call_id
            JOIN call_location_consent l ON l.call_id=c.id
            WHERE p.status='confirmed' AND c.state='accepted' AND c.urgent=0
              AND l.state='accepted' AND l.generation=p.generation
              AND NOT EXISTS (SELECT 1 FROM recommendation_outbox o WHERE o.event_id=('rec:' || p.token))
              AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=c.id
                              AND t.status IN ('pending','processing','emergency'))
            ORDER BY p.rowid""").fetchall()
    outbox = RecommendationOutbox(database)
    recovered = 0
    for row in rows:
        try:
            result = RecommendationResult.model_validate_json(row['result_json'])
            entry = outbox.enqueue(str(row['call_id']), result,
                specialty=row['selected_specialty'], location_consent=True,
                expected_generation=int(row['generation']), event_id=f"rec:{row['token']}",
                allow_confirmed_after_end=True)
        except (ValidationError, DuplicateRequest):
            logger.warning('Confirmed recommendation could not be recovered')
            continue
        if entry is not None:
            recovered += 1
    return recovered
