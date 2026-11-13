"""Keep the last server errors where the committee can read them.

The site's 500 handler calls ``record``. It must never raise: it runs while
the site is already failing, often because the database itself is the
problem, so every step is guarded and a failure here is simply dropped.
"""

import traceback
from datetime import datetime, timedelta

from flask import render_template, request

from . import scorecard_bp
from .ems import admin_required, db
from .models import ScErrorLog

KEEP_DAYS = 14


def record(error):
    try:
        original = getattr(error, "original_exception", None) or error
        detail = "".join(traceback.format_exception(type(original), original, original.__traceback__))
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        db.session.add(ScErrorLog(
            method=(request.method or "")[:8] if request else None,
            path=(request.full_path if request else "")[:200],
            error=f"{type(original).__name__}: {original}"[:300],
            detail=detail[-8000:],
        ))
        db.session.commit()
    except Exception:  # noqa: BLE001 — never make a broken page worse
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


@scorecard_bp.route("/admin/errors")
@admin_required
def admin_errors():
    cutoff = datetime.utcnow() - timedelta(days=KEEP_DAYS)
    try:
        ScErrorLog.query.filter(ScErrorLog.at < cutoff).delete()
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    errors = ScErrorLog.query.order_by(ScErrorLog.at.desc()).limit(100).all()
    return render_template("scorecard/admin_errors.html", errors=errors, now=datetime.utcnow())
