"""Shared fixtures. Redirects queue state to a tmp path for every test.

Phase 2 migrated the source of truth from `state/queues.json` to a
SQLite DB at `state/repobot.db` (see `repobot/db.py`). The legacy
`queues.STATE_PATH` constant still exists for back-compat but no
longer gates reads/writes — tests must redirect `db.DB_PATH` and
reset the cached connection, or they'll all share the real DB.
"""
import pytest

from repobot import db, queues


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    json_path = tmp_path / "queues.json"
    db_path = tmp_path / "repobot.db"
    monkeypatch.setattr(queues, "STATE_PATH", json_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    # Force a fresh connection bound to the tmp DB. Restore on teardown
    # so subsequent tests don't accidentally reuse this one.
    prev_conn = db._CONN
    db._CONN = None
    try:
        yield json_path
    finally:
        try:
            if db._CONN is not None:
                db._CONN.close()
        except Exception:
            pass
        db._CONN = prev_conn
