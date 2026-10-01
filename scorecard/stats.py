"""Standings and scoring leaders.

Both are derived on read from matches and events that are already stored —
nothing here is cached or written back. That means correcting a result, or
voiding an event days later, fixes the table and the leaderboard with it, and
there is no second copy of the truth to drift out of step.
"""

from flask import render_template, request
from sqlalchemy import func

from . import scorecard_bp
from .config import (
    POINTS_DRAW, POINTS_LOSS, POINTS_WIN, STATUS_FINAL,
)
from .ems import SPORTS, db
from .models import ScEvent, ScMatch, ScPlayer, ScTeam


def _blank_row(team):
    return {
        "team": team, "played": 0, "won": 0, "drawn": 0, "lost": 0,
        "scored": 0, "conceded": 0, "points": 0,
    }


def standings_for(sport):
    """League table for one sport, from finished matches only.

    Every registered team appears, even before it has played — an empty table
    with the teams already in it reads as "nothing has happened yet", whereas
    an empty page reads as "this is broken".
    """
    rows = {
        team.id: _blank_row(team)
        for team in ScTeam.query.filter_by(sport=sport).all()
    }

    matches = ScMatch.query.filter_by(sport=sport, status=STATUS_FINAL).all()
    for match in matches:
        if not match.team_a or not match.team_b:
            continue  # a team was deleted out from under a finished match
        a = rows.setdefault(match.team_a_id, _blank_row(match.team_a))
        b = rows.setdefault(match.team_b_id, _blank_row(match.team_b))
        score_a, score_b = match.score_a or 0, match.score_b or 0

        for row, scored, conceded in ((a, score_a, score_b), (b, score_b, score_a)):
            row["played"] += 1
            row["scored"] += scored
            row["conceded"] += conceded

        if score_a > score_b:
            winner, loser = a, b
        elif score_b > score_a:
            winner, loser = b, a
        else:
            a["drawn"] += 1
            b["drawn"] += 1
            a["points"] += POINTS_DRAW
            b["points"] += POINTS_DRAW
            continue

        winner["won"] += 1
        winner["points"] += POINTS_WIN
        loser["lost"] += 1
        loser["points"] += POINTS_LOSS

    table = list(rows.values())
    for row in table:
        row["diff"] = row["scored"] - row["conceded"]

    # Points, then difference, then goals scored — the order every league uses.
    table.sort(key=lambda r: (-r["points"], -r["diff"], -r["scored"], r["team"].name.lower()))
    for position, row in enumerate(table, start=1):
        row["position"] = position
    return table


def leaders_for(sport, limit=15):
    """Players ranked by points contributed in this sport.

    Only events that actually score are counted, so cards and substitutions
    never put anyone on the leaderboard.
    """
    return (
        db.session.query(
            ScPlayer,
            func.sum(ScEvent.points).label("points"),
            func.count(ScEvent.id).label("entries"),
        )
        .join(ScEvent, ScEvent.player_id == ScPlayer.id)
        .join(ScMatch, ScMatch.id == ScEvent.match_id)
        .filter(
            ScEvent.voided.is_(False),
            ScEvent.points > 0,
            ScMatch.sport == sport,
        )
        .group_by(ScPlayer.id)
        .order_by(func.sum(ScEvent.points).desc(), func.count(ScEvent.id).desc())
        .limit(limit)
        .all()
    )


def _sports_with_activity():
    """Sports that have a team or a match, so the tabs mean something."""
    seen = {t.sport for t in ScTeam.query.all()} | {m.sport for m in ScMatch.query.all()}
    ordered = [s for s in SPORTS if s in seen]
    return ordered or list(SPORTS)


@scorecard_bp.route("/standings")
def standings():
    available = _sports_with_activity()
    wanted = request.args.get("sport")
    sport = wanted if wanted in available else available[0]

    played = ScMatch.query.filter_by(sport=sport, status=STATUS_FINAL).count()

    return render_template(
        "scorecard/standings.html",
        sports=available,
        sport=sport,
        table=standings_for(sport),
        leaders=leaders_for(sport),
        played=played,
    )
