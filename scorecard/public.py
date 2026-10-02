"""Public scorecard pages and the JSON the live view polls."""

from flask import jsonify, render_template

from . import scorecard_bp
from .config import STATUS_FINAL, STATUS_LIVE, STATUS_SCHEDULED
from .models import ScEvent, ScMatch

# Serverless cannot hold a socket open, so the page polls. Letting the CDN
# answer for five seconds turns a hundred viewers into roughly one function
# call per five seconds, which is the difference between this being free and
# this being expensive on a match night.
LIVE_CACHE = "public, s-maxage=5, stale-while-revalidate=10"


def _cached(payload):
    response = jsonify(payload)
    response.headers["Cache-Control"] = LIVE_CACHE
    return response


def _match_brief(match):
    return {
        "id": match.id,
        "sport": match.sport,
        "gender": match.gender,
        "stage": match.stage,
        "status": match.status,
        "period": match.period,
        "venue": match.venue,
        "starts_at": match.starts_at.isoformat() if match.starts_at else None,
        "team_a": {"name": match.team_a.name, "tag": match.team_a.tag, "score": match.score_a or 0},
        "team_b": {"name": match.team_b.name, "tag": match.team_b.tag, "score": match.score_b or 0},
    }


def _event_brief(event, match):
    return {
        "id": event.id,
        "side": match.side_of(event.team_id),
        "team": event.team.tag if event.team else "",
        "player": event.player.name if event.player else None,
        "jersey": event.player.jersey_no if event.player else None,
        "clock": event.clock,
        "kind": event.kind,
        "points": event.points or 0,
        "note": event.note,
    }


def _ordered(status, descending=False):
    query = ScMatch.query.filter_by(status=status)
    order = ScMatch.starts_at.desc() if descending else ScMatch.starts_at.asc()
    return query.order_by(order, ScMatch.id.desc()).all()


@scorecard_bp.route("/")
def index():
    return render_template(
        "scorecard/index.html",
        live=_ordered(STATUS_LIVE),
        upcoming=_ordered(STATUS_SCHEDULED),
        results=_ordered(STATUS_FINAL, descending=True),
    )


@scorecard_bp.route("/match/<int:match_id>")
def match(match_id):
    record = ScMatch.query.get_or_404(match_id)
    events = (
        ScEvent.query.filter_by(match_id=record.id, voided=False)
        .order_by(ScEvent.id.desc())
        .all()
    )
    return render_template(
        "scorecard/match.html",
        match=record,
        events=events,
        team_a_scorers=record.scorers_for_side("a"),
        team_b_scorers=record.scorers_for_side("b"),
        potm=record.player_of_the_match,
    )


@scorecard_bp.route("/api/live.json")
def api_live():
    return _cached({"matches": [_match_brief(m) for m in _ordered(STATUS_LIVE)]})


@scorecard_bp.route("/api/match/<int:match_id>.json")
def api_match(match_id):
    record = ScMatch.query.get_or_404(match_id)
    events = (
        ScEvent.query.filter_by(match_id=record.id, voided=False)
        .order_by(ScEvent.id.desc())
        .limit(60)
        .all()
    )
    payload = _match_brief(record)
    payload["events"] = [_event_brief(event, record) for event in events]
    payload["scorers_a"] = record.scorers_for_side("a")
    payload["scorers_b"] = record.scorers_for_side("b")
    potm = record.player_of_the_match
    payload["potm"] = {
        "player": potm["player"].name if potm and potm.get("player") else None,
        "team": potm["team"].name if potm and potm.get("team") else None,
        "points": potm["points"] if potm else 0,
        "metric_label": potm["metric_label"] if potm else "",
    } if potm else None
    return _cached(payload)
