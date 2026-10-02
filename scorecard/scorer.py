"""Scorer sign-in and the match console."""

from datetime import datetime

from flask import (
    flash, jsonify, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash

from . import scorecard_bp
from .ems import db
from .auth import (
    SESSION_NAME, current_scorer_id, is_admin, may_score, scorer_required,
    sign_in, sign_out,
)
from .config import (
    STATUS_FINAL, STATUS_LIVE, STATUS_SCHEDULED, action_for, rules_for,
)
from .models import ScEvent, ScMatch, ScPlayer, ScScorer


def _console_payload(match):
    """Everything the console needs to redraw itself after a tap."""
    events = (
        ScEvent.query.filter_by(match_id=match.id, voided=False)
        .order_by(ScEvent.id.desc())
        .limit(40)
        .all()
    )
    return {
        "score_a": match.score_a or 0,
        "score_b": match.score_b or 0,
        "period": match.period,
        "status": match.status,
        "events": [
            {
                "id": event.id,
                "side": match.side_of(event.team_id),
                "kind": event.kind,
                "points": event.points or 0,
                "clock": event.clock,
                "player": event.player.name if event.player else None,
            }
            for event in events
        ],
    }


@scorecard_bp.route("/scorer/login", methods=["GET", "POST"])
def scorer_login():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        passcode = request.form.get("passcode") or ""
        # Matched case-insensitively: a volunteer given the name "mandu" will
        # type "Mandu" as often as not.
        scorer = ScScorer.query.filter(
            db.func.lower(ScScorer.name) == name.lower(),
            ScScorer.active.is_(True),
        ).first()
        if scorer and check_password_hash(scorer.passcode_hash, passcode):
            sign_in(scorer)
            return redirect(request.args.get("next") or url_for("scorecard.scorer_home"))
        flash("That name and passcode do not match. Check with the committee.", "error")
    return render_template("scorecard/scorer_login.html")


@scorecard_bp.route("/scorer/logout")
def scorer_logout():
    sign_out()
    flash("Signed out of scoring.", "info")
    return redirect(url_for("scorecard.scorer_login"))


@scorecard_bp.route("/scorer")
@scorer_required
def scorer_home():
    scorer_id = current_scorer_id()
    query = ScMatch.query.filter(ScMatch.status.in_([STATUS_SCHEDULED, STATUS_LIVE]))
    if scorer_id:
        query = query.filter(
            db.or_(
                ScMatch.assigned_scorer_id.is_(None),
                ScMatch.assigned_scorer_id == scorer_id,
            )
        )
    matches = query.order_by(ScMatch.starts_at.asc(), ScMatch.id.desc()).all()
    return render_template(
        "scorecard/scorer_home.html",
        matches=matches,
        scorer_name=session.get(SESSION_NAME, "Scorer"),
    )


@scorecard_bp.route("/scorer/match/<int:match_id>")
@scorer_required
def console(match_id):
    match = ScMatch.query.get_or_404(match_id)
    if not may_score(match):
        flash("That match is assigned to another scorer.", "error")
        return redirect(url_for("scorecard.scorer_home"))
    rules = rules_for(match.sport)
    players = {
        "a": ScPlayer.query.filter_by(team_id=match.team_a_id, active=True).order_by(ScPlayer.name).all(),
        "b": ScPlayer.query.filter_by(team_id=match.team_b_id, active=True).order_by(ScPlayer.name).all(),
    }
    events = (
        ScEvent.query.filter_by(match_id=match.id, voided=False)
        .order_by(ScEvent.id.desc())
        .limit(40)
        .all()
    )
    return render_template(
        "scorecard/console.html",
        match=match, rules=rules, players=players, events=events,
    )


def _guard(match_id):
    """Shared checks for every write. Returns (match, error_response)."""
    match = ScMatch.query.get_or_404(match_id)
    if not may_score(match):
        return None, (jsonify({"error": "This match is assigned to another scorer."}), 403)
    if match.status == STATUS_FINAL:
        return None, (jsonify({"error": "This match is finished."}), 400)
    return match, None


@scorecard_bp.route("/scorer/match/<int:match_id>/event", methods=["POST"])
@scorer_required
def add_event(match_id):
    match, error = _guard(match_id)
    if error:
        return error

    side = request.form.get("side")
    if side not in ("a", "b"):
        return jsonify({"error": "Pick a team."}), 400

    action = action_for(match.sport, request.form.get("action") or "")
    if not action:
        return jsonify({"error": "That action is not valid for this sport."}), 400

    team_id = match.team_a_id if side == "a" else match.team_b_id

    player_id = request.form.get("player_id") or None
    if player_id:
        player = ScPlayer.query.filter_by(id=int(player_id), team_id=team_id).first()
        player_id = player.id if player else None

    event = ScEvent(
        match_id=match.id,
        team_id=team_id,
        player_id=player_id,
        clock=(request.form.get("clock") or "").strip()[:8] or None,
        kind=action["label"],
        points=action["points"],
    )
    db.session.add(event)

    # A first event means the match is under way.
    if match.status == STATUS_SCHEDULED:
        match.status = STATUS_LIVE
        match.period = match.period or rules_for(match.sport)["periods"][0]

    db.session.flush()
    match.recalculate()
    db.session.commit()
    return jsonify(_console_payload(match))


@scorecard_bp.route("/scorer/match/<int:match_id>/undo", methods=["POST"])
@scorer_required
def undo_event(match_id):
    match, error = _guard(match_id)
    if error:
        return error

    last = (
        ScEvent.query.filter_by(match_id=match.id, voided=False)
        .order_by(ScEvent.id.desc())
        .first()
    )
    if not last:
        return jsonify({"error": "Nothing to undo."}), 400

    last.voided = True
    db.session.flush()
    match.recalculate()
    db.session.commit()
    return jsonify(_console_payload(match))


@scorecard_bp.route("/scorer/match/<int:match_id>/period", methods=["POST"])
@scorer_required
def set_period(match_id):
    match, error = _guard(match_id)
    if error:
        return error

    periods = rules_for(match.sport)["periods"]
    wanted = request.form.get("period")
    if wanted in periods:
        match.period = wanted
    else:  # no explicit choice — step to the next one
        current = periods.index(match.period) if match.period in periods else -1
        match.period = periods[min(current + 1, len(periods) - 1)]

    match.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_console_payload(match))


@scorecard_bp.route("/scorer/match/<int:match_id>/status", methods=["POST"])
@scorer_required
def set_status(match_id):
    match = ScMatch.query.get_or_404(match_id)
    if not may_score(match):
        return jsonify({"error": "This match is assigned to another scorer."}), 403

    wanted = request.form.get("status")
    if wanted not in (STATUS_LIVE, STATUS_FINAL):
        return jsonify({"error": "Unknown status."}), 400

    match.status = wanted
    if wanted == STATUS_LIVE and not match.period:
        match.period = rules_for(match.sport)["periods"][0]
    if wanted == STATUS_FINAL:
        if (match.score_a or 0) > (match.score_b or 0):
            match.winner_team_id = match.team_a_id
        elif (match.score_b or 0) > (match.score_a or 0):
            match.winner_team_id = match.team_b_id
        else:
            match.winner_team_id = None

    match.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_console_payload(match))
