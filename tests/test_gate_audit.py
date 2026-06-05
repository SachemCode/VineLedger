"""gate_audit table is created by init_db."""

import os
import tempfile

import database as db


def test_init_db_creates_gate_audit():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        old = db.SQLITE_DB_PATH
        db.SQLITE_DB_PATH = path
        conn = db.init_db()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='gate_audit'"
            ).fetchone()
            assert row is not None
            row2 = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='app_action_audit'"
            ).fetchone()
            assert row2 is not None
            db.log_gate_event(conn, "user1", "login", None)
            n = conn.execute("SELECT COUNT(*) FROM gate_audit").fetchone()[0]
            assert n == 1
            db.log_app_action(conn, "user1", "Payment", "test summary", save_mode="immediate")
            n2 = conn.execute("SELECT COUNT(*) FROM app_action_audit").fetchone()[0]
            assert n2 == 1
        finally:
            conn.close()
    finally:
        db.SQLITE_DB_PATH = old
        os.unlink(path)
