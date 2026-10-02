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
    """Allow a signed-in scorer. Viewing the scorer home requires an explicit scorer sign-in."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_scorer_id():
            return view(*args, **kwargs)
        # Viewing the scorer match dashboard (/scores/scorer) strictly requires signing in as a scorer
        if request.endpoint == "scorecard.scorer_home":
            flash("Sign in with your scorer name and passcode to view assigned matches.", "info")
            return redirect(url_for("scorecard.scorer_login", next=request.path))
        # Admin can access console actions directly from admin fixtures
        if is_admin():
            return view(*args, **kwargs)
        if request.is_json or request.headers.get("X-CSRF-Token") is not None:
            return jsonify({"error": "Scorer login required."}), 401
        flash("Sign in with your scorer name and passcode to record a match.", "error")
        return redirect(url_for("scorecard.scorer_login", next=request.path))

    return wrapper


def may_score(match):
    """A signed-in scorer scores their assigned matches or unassigned ones. An admin can score any match."""
    scorer_id = current_scorer_id()
    if scorer_id:
        return match.assigned_scorer_id in (None, scorer_id)
    if is_admin():
        return True
    return False
