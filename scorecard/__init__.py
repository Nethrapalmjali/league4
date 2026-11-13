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
        # Lightweight bootstrap for new sc_match columns on existing databases.
        try:
            engine = db.engine
            dialect = engine.dialect.name
            with engine.begin() as conn:
                if dialect == "sqlite":
                    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(sc_match)")).fetchall()}
                    if "scorer_can_edit_later" not in cols:
                        conn.execute(text("ALTER TABLE sc_match ADD COLUMN scorer_can_edit_later BOOLEAN DEFAULT 0"))
                else:
                    conn.execute(text(
                        "ALTER TABLE sc_match ADD COLUMN IF NOT EXISTS scorer_can_edit_later BOOLEAN DEFAULT FALSE"
                    ))
        except Exception as exc:  # noqa: BLE001
            current_app.logger.warning("Scorecard column bootstrap skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001 — never take the site down over this
        current_app.logger.exception("Scorecard table setup failed: %s", exc)


# Imported for their side effects — each module hangs routes off the blueprint.
from . import admin, public, scorer, stats  # noqa: E402,F401
