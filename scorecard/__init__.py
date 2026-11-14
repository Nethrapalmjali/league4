"""Live match scorecard — a self-contained package mounted at /scores.

Isolation rules this package keeps to:

* every table is prefixed ``sc_`` and declares no foreign key into the EMS
* every route lives under ``/scores``
* templates live under ``templates/scorecard/`` so no name can collide
* its stylesheet is served from the blueprint's own static folder
* the scorer session key is separate from the admin one

Registering it touches ``app.py`` in two lines and nothing else.
"""

from flask import Blueprint, current_app

scorecard_bp = Blueprint(
    "scorecard",
    __name__,
    url_prefix="/scores",
    template_folder="templates",
    static_folder="static",
    # Relative to url_prefix — spelling it "/scores/static" here yields
    # /scores/scores/static, because Flask joins it onto the prefix.
    static_url_path="/static",
)

_tables_ready = False


@scorecard_bp.before_request
def _ensure_tables():
    """Create the sc_ tables on the first scorecard request of a process.

    Deliberately not done at import time: the EMS learned that the hard way on
    Vercel, where a database that is momentarily unreachable turned an
    import-time query into FUNCTION_INVOCATION_FAILED with no traceback. Doing
    it here means a database problem degrades to a normal error page instead.

    ``create_all`` checks for each table first, so the EMS tables it also sees
    are left untouched.
    """
    global _tables_ready
    if _tables_ready:
        return
    _tables_ready = True  # set first: a failure must not retry on every request
    try:
        from sqlalchemy import text
        from .ems import db

        db.create_all()
        # Lightweight bootstrap for new columns on existing databases. Each
        # statement runs in its own transaction: on Postgres one failure would
        # otherwise abort every statement after it.
        engine = db.engine
        dialect = engine.dialect.name

        def _columns(conn, table):
            return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}

        steps = []
        if dialect == "sqlite":
            steps.append(lambda c: "scorer_can_edit_later" in _columns(c, "sc_match") or c.execute(
                text("ALTER TABLE sc_match ADD COLUMN scorer_can_edit_later BOOLEAN DEFAULT 0")))
            steps.append(lambda c: "client_id" in _columns(c, "sc_event") or c.execute(
                text("ALTER TABLE sc_event ADD COLUMN client_id VARCHAR(40)")))
            steps.append(lambda c: "actor" in _columns(c, "sc_sync_op") or c.execute(
                text("ALTER TABLE sc_sync_op ADD COLUMN actor VARCHAR(100)")))
            steps.append(lambda c: "potm_player_id" in _columns(c, "sc_match") or c.execute(
                text("ALTER TABLE sc_match ADD COLUMN potm_player_id INTEGER")))
            steps.append(lambda c: "match_format" in _columns(c, "sc_match") or c.execute(
                text("ALTER TABLE sc_match ADD COLUMN match_format VARCHAR(12)")))
            steps.append(lambda c: "result_type" in _columns(c, "sc_match") or c.execute(
                text("ALTER TABLE sc_match ADD COLUMN result_type VARCHAR(12)")))
        else:
            steps.append(lambda c: c.execute(text(
                "ALTER TABLE sc_match ADD COLUMN IF NOT EXISTS result_type VARCHAR(12)")))
            steps.append(lambda c: c.execute(text(
                "ALTER TABLE sc_match ADD COLUMN IF NOT EXISTS match_format VARCHAR(12)")))
            steps.append(lambda c: c.execute(text(
                "ALTER TABLE sc_match ADD COLUMN IF NOT EXISTS potm_player_id INTEGER")))
            steps.append(lambda c: c.execute(text(
                "ALTER TABLE sc_sync_op ADD COLUMN IF NOT EXISTS actor VARCHAR(100)")))
            steps.append(lambda c: c.execute(text(
                "ALTER TABLE sc_match ADD COLUMN IF NOT EXISTS scorer_can_edit_later BOOLEAN DEFAULT FALSE")))
            steps.append(lambda c: c.execute(text(
                "ALTER TABLE sc_event ADD COLUMN IF NOT EXISTS client_id VARCHAR(40)")))
        # Unique index: NULLs never collide, so web-console events are unaffected.
        steps.append(lambda c: c.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_sc_event_client_id ON sc_event (client_id)")))
        # Badminton fixtures that have not started yet use the new best-of-5
        # format; anything already scored keeps the format it was scored in.
        steps.append(lambda c: c.execute(text(
            "UPDATE sc_match SET match_format = 'bo5' WHERE sport = 'Badminton' "
            "AND match_format IS NULL AND status = 'scheduled' "
            "AND NOT EXISTS (SELECT 1 FROM sc_event e WHERE e.match_id = sc_match.id)")))

        for step in steps:
            try:
                with engine.begin() as conn:
                    step(conn)
            except Exception as exc:  # noqa: BLE001
                current_app.logger.warning("Scorecard column bootstrap step skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001 — never take the site down over this
        current_app.logger.exception("Scorecard table setup failed: %s", exc)


# Imported for their side effects — each module hangs routes off the blueprint.
from . import admin, app_api, errorlog, public, scorer, stats  # noqa: E402,F401
