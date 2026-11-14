"""Public scorecard pages and the JSON the live view polls."""

import threading
import time
from datetime import datetime

from flask import abort, jsonify, render_template
from sqlalchemy.orm import selectinload

from . import scorecard_bp
from .config import STATUS_FINAL, STATUS_LIVE, STATUS_SCHEDULED
from .models import ScMatch, event_order

# The page polls for new scores. A shared cache may answer for a couple of
# seconds; scores still reach spectators within a few seconds of the tap.
LIVE_CACHE = "public, s-maxage=2, stale-while-revalidate=5"


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
        "updated_at": match.updated_at.isoformat() + "Z" if match.updated_at else None,
    }


@scorecard_bp.app_template_filter("sc_minutes_ago")
def minutes_ago(when):
    """Whole minutes since a stored (UTC) time, or None."""
    if not when:
        return None
    return int((datetime.utcnow() - when).total_seconds() // 60)


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


# Every spectator's phone asks for the latest score every few seconds. Answers
# are kept for a moment and shared, so a hundred phones cost the database about
# one query per match every CACHE_SECONDS instead of a hundred — which keeps the
# server free for the scorers' uploads.
CACHE_SECONDS = 2.5
_cache = {}
_cache_lock = threading.Lock()


def _shared(key, build):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = build()
    with _cache_lock:
        _cache[key] = (now + CACHE_SECONDS, value)
        if len(_cache) > 500:  # never grows without bound
            for stale in [k for k, (exp, _v) in _cache.items() if exp <= now]:
                _cache.pop(stale, None)
    return value


@scorecard_bp.route("/")
def index():
    # One query for all matches (teams joined) plus one for all their events,
    # instead of several per match.
    everything = (
        ScMatch.query.options(selectinload(ScMatch.events))
        .order_by(ScMatch.starts_at.asc(), ScMatch.id.desc()).all()
    )
    live = [m for m in everything if m.status == STATUS_LIVE]
    upcoming = [m for m in everything if m.status == STATUS_SCHEDULED]
    results = [m for m in everything if m.status == STATUS_FINAL]
    results.sort(key=lambda m: (m.starts_at is not None, m.starts_at or datetime.min, m.id), reverse=True)
    return render_template("scorecard/index.html", live=live, upcoming=upcoming, results=results)


@scorecard_bp.route("/match/<int:match_id>")
def match(match_id):
    record = (ScMatch.query.options(selectinload(ScMatch.events))
              .filter_by(id=match_id).first_or_404())
    events = sorted((e for e in record.events if not e.voided), key=event_order, reverse=True)
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
    def build():
        rows = (ScMatch.query.options(selectinload(ScMatch.events)).filter_by(status=STATUS_LIVE)
                .order_by(ScMatch.starts_at.asc(), ScMatch.id.desc()).all())
        out = []
        for m in rows:
            brief = _match_brief(m)
            if m.sport == "Badminton":
                brief["line"] = m.badminton_stats["live_text"]  # e.g. "Men's Doubles: 9–7"
            out.append(brief)
        return {"matches": out}

    return _cached(_shared("live", build))


@scorecard_bp.route("/api/match/<int:match_id>.json")
def api_match(match_id):
    payload = _shared(("match", match_id), lambda: _match_payload(match_id))
    if payload is None:
        abort(404)
    return _cached(payload)


def _match_payload(match_id):
    record = (ScMatch.query.options(selectinload(ScMatch.events))
              .filter_by(id=match_id).first())
    if record is None:
        return None
    events = sorted((e for e in record.events if not e.voided), key=event_order, reverse=True)[:60]
    payload = _match_brief(record)
    payload["events"] = [_event_brief(event, record) for event in events]
    payload["scorers_a"] = record.scorers_for_side("a")
    payload["scorers_b"] = record.scorers_for_side("b")
    if record.sport == "Badminton":
        bs = record.badminton_stats
        payload["badminton"] = {
            "tally": bs["tally"], "unit": bs["unit"], "result_text": bs["result_text"], "live_text": bs["live_text"],
            "active_label": bs["active_label"],
            "rounds": [{k: g[k] for k in ("number", "label", "a", "b", "winner", "status")}
                       for g in bs["games_list"]],
        }
    potm = record.player_of_the_match
    payload["potm"] = {
        "player": potm["player"].name if potm and potm.get("player") else None,
        "team": potm["team"].name if potm and potm.get("team") else None,
        "points": potm["points"] if potm else 0,
        "metric_label": potm["metric_label"] if potm else "",
    } if potm else None
    return payload
