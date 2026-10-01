"""Scorer sessions.

Deliberately separate from the EMS admin session. A scorer holds the session
key ``sc_scorer_id`` and nothing else — it grants the scoring console and
nothing in the auction, the player list or the exports. An admin can always
score as well, so a committee member never needs a second passcode.
"""

from functools import wraps

from flask import flash, jsonify, redirect, request, session, url_for

SESSION_KEY = "sc_scorer_id"
SESSION_NAME = "sc_scorer_name"


def current_scorer_id():
    return session.get(SESSION_KEY)


def is_admin():
    return bool(session.get("admin_logged_in"))


def sign_in(scorer):
    session[SESSION_KEY] = scorer.id
    session[SESSION_NAME] = scorer.name


def sign_out():
    session.pop(SESSION_KEY, None)
    session.pop(SESSION_NAME, None)


def scorer_required(view):
    """Allow a signed-in scorer, or an admin acting as one."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_scorer_id() or is_admin():
            return view(*args, **kwargs)
        if request.is_json or request.headers.get("X-CSRF-Token") is not None:
            return jsonify({"error": "Scorer login required."}), 401
        flash("Sign in with your scorer passcode to record a match.", "error")
        return redirect(url_for("scorecard.scorer_login", next=request.path))

    return wrapper


def may_score(match):
    """Admins score anything. A scorer scores unassigned matches or their own."""
    if is_admin():
        return True
    scorer_id = current_scorer_id()
    if not scorer_id:
        return False
    return match.assigned_scorer_id in (None, scorer_id)
