"""Standings and scoring leaders.

Both are derived on read from matches and events that are already stored —
nothing here is cached or written back. That means correcting a result, or
voiding an event days later, fixes the table and the leaderboard with it, and
there is no second copy of the truth to drift out of step.

Every table is now gender-aware: standings and leaders are computed per
(sport, gender) pair so Basketball Men and Basketball Women never bleed into
each other's tables.
"""

from collections import defaultdict

from flask import abort, render_template, request
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from . import scorecard_bp
from .config import (
    POINTS_DRAW, POINTS_LOSS, POINTS_WIN, STATUS_FINAL, player_stats_for,
)
from .ems import SPORTS, db, ems_player_card
from .models import ScEvent, ScMatch, ScPlayer, ScTeam

GENDERS = ["Men", "Women"]


def _auto_potm(points_by_player, winner_team_id, team_of):
    """Top scorer of the match, preferring the winning side — the same rule as
    ScMatch.player_of_the_match."""
    if not points_by_player:
        return None
    ranked = sorted(points_by_player.items(), key=lambda kv: -kv[1])
    on_winner = [pid for pid, _ in ranked if team_of.get(pid) == winner_team_id]
    return on_winner[0] if on_winner else ranked[0][0]


def player_table(sport, gender):
    """Every credited player in one (sport, gender): stats per category,
    matches played and Player of the Match awards, in three queries.

    Returns {"categories", "leaderboards", "players", "tournament"}.
    """
    categories = player_stats_for(sport)
    rows = (
        db.session.query(
            ScEvent.player_id, ScEvent.match_id, ScEvent.kind,
            func.coalesce(func.sum(ScEvent.points), 0), func.count(ScEvent.id),
        )
        .join(ScMatch, ScMatch.id == ScEvent.match_id)
        .filter(
            ScEvent.voided.is_(False), ScEvent.player_id.isnot(None),
            ScMatch.sport == sport, ScMatch.gender == gender,
        )
        .group_by(ScEvent.player_id, ScEvent.match_id, ScEvent.kind)
        .all()
    )
    finals = (
        db.session.query(ScMatch.id, ScMatch.winner_team_id, ScMatch.potm_player_id)
        .filter(ScMatch.sport == sport, ScMatch.gender == gender, ScMatch.status == STATUS_FINAL)
        .all()
    )
    ids = {r[0] for r in rows} | {f[2] for f in finals if f[2]}
    players = {
        p.id: p for p in ScPlayer.query.options(joinedload(ScPlayer.team)).filter(ScPlayer.id.in_(ids)).all()
    } if ids else {}

    stats = {}

    def entry(pid):
        if pid not in stats:
            stats[pid] = {"player": players[pid], "matches": set(), "potm": 0,
                          **{c["key"]: 0 for c in categories}}
        return stats[pid]

    match_points = defaultdict(lambda: defaultdict(int))
    for pid, mid, kind, points, count in rows:
        if pid not in players:
            continue
        s = entry(pid)
        s["matches"].add(mid)
        for c in categories:
            if kind in c["kinds"]:
                s[c["key"]] += int(points) if c["measure"] == "points" else int(count)
        if points and points > 0:
            match_points[mid][pid] += int(points)

    team_of = {pid: p.team_id for pid, p in players.items()}
    for mid, winner, chosen in finals:
        pick = chosen if chosen in players else _auto_potm(match_points.get(mid), winner, team_of)
        if pick in players:
            entry(pick)["potm"] += 1

    for s in stats.values():
        s["played"] = len(s.pop("matches"))
    headline = categories[0]["key"]
    ordered = sorted(stats.values(), key=lambda s: (-s[headline], -s["played"], s["player"].name.lower()))
    leaderboards = [
        {
            "category": c,
            "rows": sorted(
                (s for s in stats.values() if s[c["key"]] > 0),
                key=lambda s, k=c["key"]: (-s[k], s["played"], s["player"].name.lower()),
            )[:10],
        }
        for c in categories
    ]
    tournament = sorted(
        (s for s in stats.values() if s["potm"] or s[headline]),
        key=lambda s: (-s["potm"], -s[headline], s["player"].name.lower()),
    )[:5]
    return {"categories": categories, "leaderboards": leaderboards, "players": ordered, "tournament": tournament}


def _blank_row(team):
    return {
        "team": team, "played": 0, "won": 0, "drawn": 0, "lost": 0,
        "scored": 0, "conceded": 0, "points": 0,
    }


def standings_for(sport, gender="Men"):
    """League table for one (sport, gender) pair, from finished matches only.

    Every registered team appears, even before it has played — an empty table
    with the teams already in it reads as "nothing has happened yet", whereas
    an empty page reads as "this is broken".
    """
    rows = {
        team.id: _blank_row(team)
        for team in ScTeam.query.filter_by(sport=sport, gender=gender).all()
    }

    matches = ScMatch.query.filter_by(
        sport=sport, gender=gender, status=STATUS_FINAL
    ).all()
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
        elif match.winner_team_id in (match.team_a_id, match.team_b_id):
            # Level on the scoreboard, but the committee recorded a winner
            # (penalties, toss, tie-break) on the Edit match page.
            winner, loser = (a, b) if match.winner_team_id == match.team_a_id else (b, a)
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


def leaders_for(sport, gender="Men", limit=15):
    """Players ranked by points contributed in this (sport, gender) pair.

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
        .join(ScTeam, ScTeam.id == ScPlayer.team_id)
        .filter(
            ScEvent.voided.is_(False),
            ScEvent.points > 0,
            ScMatch.sport == sport,
            ScMatch.gender == gender,
            ScTeam.gender == gender,
        )
        .group_by(ScPlayer.id)
        .order_by(func.sum(ScEvent.points).desc(), func.count(ScEvent.id).desc())
        .limit(limit)
        .all()
    )


def _sport_gender_combos():
    """Return (sport, gender) pairs that have teams or matches, plus all Men/Women
    for each sport that has at least one team. Ensures Basketball Women (and any
    other women's sport) gets its own tab even with zero matches so far."""
    team_combos = set(db.session.query(ScTeam.sport, ScTeam.gender).distinct().all())
    combos = set(team_combos) | set(db.session.query(ScMatch.sport, ScMatch.gender).distinct().all())

    # Make sure every sport that has teams gets both Men and Women tabs so the
    # UI can show gender-separate tables even before any women's team exists.
    sports_with_teams = {s for s, _g in team_combos}
    for sport in sports_with_teams:
        combos.add((sport, "Men"))
        combos.add((sport, "Women"))

    # Order: follow SPORTS order, Men before Women for each sport.
    ordered = []
    for sport in SPORTS:
        for g in GENDERS:
            if (sport, g) in combos:
                ordered.append((sport, g))
    return ordered or [(s, "Men") for s in SPORTS]


@scorecard_bp.route("/standings")
def standings():
    available = _sport_gender_combos()
    wanted_sport = request.args.get("sport")
    wanted_gender = request.args.get("gender", "Men")
    if wanted_gender not in ("Men", "Women"):
        wanted_gender = "Men"

    # Find the matching combo, defaulting to the first available.
    sport = None
    gender = wanted_gender
    for s, g in available:
        if s == wanted_sport:
            sport = s
            gender = g
            break
    if sport is None:
        sport, gender = available[0]

    from .public import _shared

    def build():
        table = standings_for(sport, gender)
        return {"table": table, "played": sum(r["played"] for r in table) // 2,
                "players": player_table(sport, gender)}

    data = _shared(("standings", sport, gender), build)
    return render_template(
        "scorecard/standings.html",
        sports=available,
        sport=sport,
        gender=gender,
        genders=GENDERS,
        table=data["table"],
        played=data["played"],
        stats=data["players"],
    )


# ------------------------------------------------------------ player pages --
@scorecard_bp.route("/player/<int:player_id>")
def player_page(player_id):
    """One player's record: photo, team, totals and match-by-match stats."""
    player = ScPlayer.query.options(joinedload(ScPlayer.team)).filter_by(id=player_id).first()
    if not player or not player.team:
        abort(404)
    team = player.team
    categories = player_stats_for(team.sport)
    rows = (
        db.session.query(ScEvent.match_id, ScEvent.kind,
                         func.coalesce(func.sum(ScEvent.points), 0), func.count(ScEvent.id))
        .filter(ScEvent.player_id == player.id, ScEvent.voided.is_(False))
        .group_by(ScEvent.match_id, ScEvent.kind)
        .all()
    )
    per_match = defaultdict(lambda: {c["key"]: 0 for c in categories})
    for mid, kind, points, count in rows:
        for c in categories:
            if kind in c["kinds"]:
                per_match[mid][c["key"]] += int(points) if c["measure"] == "points" else int(count)
    matches = (
        ScMatch.query.filter(ScMatch.id.in_(list(per_match))).order_by(ScMatch.starts_at.desc(), ScMatch.id.desc()).all()
        if per_match else []
    )
    record = player_table(team.sport, team.gender)
    mine = next((s for s in record["players"] if s["player"].id == player.id), None)
    games = []
    for m in matches:
        own_side = "a" if m.team_a_id == team.id else "b"
        opponent = m.team_b if own_side == "a" else m.team_a
        us, them = (m.score_a or 0, m.score_b or 0) if own_side == "a" else (m.score_b or 0, m.score_a or 0)
        outcome = None
        if m.status == STATUS_FINAL:
            outcome = "Won" if us > them else ("Lost" if us < them else "Drew")
        games.append({"match": m, "opponent": opponent, "us": us, "them": them,
                      "outcome": outcome, "stats": per_match[m.id]})
    totals = {c["key"]: sum(g["stats"][c["key"]] for g in games) for c in categories}
    return render_template(
        "scorecard/player.html",
        player=player, team=team, categories=categories, games=games, totals=totals,
        potm=mine["potm"] if mine else 0, card=ems_player_card(player.ems_player_id),
    )
