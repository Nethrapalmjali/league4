"""Admin screens: teams, players, fixtures, scorer passcodes, EMS linking.

These reuse the EMS admin session via its own ``admin_required`` decorator, so
there is no second committee login to remember.
"""

from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from . import scorecard_bp
from .config import STAGES, STATUS_LIVE, STATUS_SCHEDULED, rules_for
from .ems import SPORTS, admin_required, db, team_owner_model
from .models import ScEvent, ScMatch, ScPlayer, ScScorer, ScTeam


def _clean(value, limit=120):
    return (value or "").strip()[:limit]


def _parse_when(value):
    """Accept the datetime-local format, tolerate a blank."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


# ------------------------------------------------------------------- teams --
@scorecard_bp.route("/admin/teams", methods=["GET", "POST"])
@admin_required
def admin_teams():
    if request.method == "POST":
        name = _clean(request.form.get("name"), 100)
        sport = _clean(request.form.get("sport"), 50)
        if not name or sport not in SPORTS:
            flash("A team needs a name and a valid sport.", "error")
        else:
            db.session.add(ScTeam(
                name=name,
                short_name=_clean(request.form.get("short_name"), 6).upper() or None,
                sport=sport,
                gender=_clean(request.form.get("gender"), 10) or "Men",
            ))
            db.session.commit()
            flash(f"Added {name}.", "success")
        return redirect(url_for("scorecard.admin_teams"))

    return render_template(
        "scorecard/admin_teams.html",
        teams=ScTeam.query.order_by(ScTeam.sport, ScTeam.name).all(),
        sports=SPORTS,
    )


@scorecard_bp.route("/admin/sync_squads", methods=["POST"])
@admin_required
def admin_sync_squads():
    from .sync import sync_all_auction_squads
    count = sync_all_auction_squads()
    flash(f"Successfully synchronised {count} auctioned player(s) into tournament team rosters.", "success")
    return redirect(request.referrer or url_for("scorecard.admin_teams"))


@scorecard_bp.route("/admin/teams/<int:team_id>/delete", methods=["POST"])
@admin_required
def admin_delete_team(team_id):
    team = ScTeam.query.get_or_404(team_id)
    booked = ScMatch.query.filter(
        db.or_(ScMatch.team_a_id == team.id, ScMatch.team_b_id == team.id)
    ).count()
    if booked:
        flash(f"{team.name} is in {booked} fixture(s). Remove those first.", "error")
    else:
        db.session.delete(team)
        db.session.commit()
        flash(f"Removed {team.name}.", "success")
    return redirect(url_for("scorecard.admin_teams"))


@scorecard_bp.route("/admin/teams/<int:team_id>/players", methods=["GET", "POST"])
@admin_required
def admin_players(team_id):
    team = ScTeam.query.get_or_404(team_id)
    if request.method == "POST":
        name = _clean(request.form.get("name"), 100)
        if not name:
            flash("A player needs a name.", "error")
        else:
            db.session.add(ScPlayer(
                team_id=team.id,
                name=name,
                jersey_no=_clean(request.form.get("jersey_no"), 4) or None,
            ))
            db.session.commit()
            flash(f"Added {name}.", "success")
        return redirect(url_for("scorecard.admin_players", team_id=team.id))

    return render_template("scorecard/admin_players.html", team=team)


@scorecard_bp.route("/admin/players/<int:player_id>/delete", methods=["POST"])
@admin_required
def admin_delete_player(player_id):
    player = ScPlayer.query.get_or_404(player_id)
    team_id = player.team_id
    if ScEvent.query.filter_by(player_id=player.id).count():
        player.active = False  # keep the name on past scorecards
        flash(f"{player.name} has match events, so they were retired instead.", "info")
    else:
        db.session.delete(player)
        flash(f"Removed {player.name}.", "success")
    db.session.commit()
    return redirect(url_for("scorecard.admin_players", team_id=team_id))


# ---------------------------------------------------------------- fixtures --
@scorecard_bp.route("/admin/matches", methods=["GET", "POST"])
@admin_required
def admin_matches():
    if request.method == "POST":
        sport = _clean(request.form.get("sport"), 50)
        team_a = request.form.get("team_a_id")
        team_b = request.form.get("team_b_id")
        instant = request.form.get("start_now") == "1"

        if sport not in SPORTS or not team_a or not team_b:
            flash("Pick a sport and both teams.", "error")
            return redirect(url_for("scorecard.admin_matches"))
        if team_a == team_b:
            flash("Team A and Team B are the same team — pick two different ones.", "error")
            return redirect(url_for("scorecard.admin_matches"))

        side_a = ScTeam.query.get(int(team_a))
        side_b = ScTeam.query.get(int(team_b))
        wrong = [t.name for t in (side_a, side_b) if t and t.sport != sport]
        if wrong:
            flash(f"{' and '.join(wrong)} do not play {sport}. Pick teams from that sport.", "error")
            return redirect(url_for("scorecard.admin_matches"))

        match = ScMatch(
            sport=sport,
            gender=_clean(request.form.get("gender"), 10) or "Men",
            stage=_clean(request.form.get("stage"), 40) or "League",
            team_a_id=int(team_a),
            team_b_id=int(team_b),
            venue=_clean(request.form.get("venue")) or None,
            starts_at=_parse_when(request.form.get("starts_at")) or (
                datetime.utcnow() if instant else None
            ),
            status=STATUS_LIVE if instant else STATUS_SCHEDULED,
            period=rules_for(sport)["periods"][0] if instant else None,
            assigned_scorer_id=int(request.form["scorer_id"])
            if request.form.get("scorer_id") else None,
        )
        db.session.add(match)
        db.session.commit()
        flash("Match started." if instant else "Fixture added.", "success")
        if instant:
            return redirect(url_for("scorecard.console", match_id=match.id))
        return redirect(url_for("scorecard.admin_matches"))

    return render_template(
        "scorecard/admin_matches.html",
        matches=ScMatch.query.order_by(ScMatch.starts_at.desc(), ScMatch.id.desc()).all(),
        teams=ScTeam.query.order_by(ScTeam.sport, ScTeam.name).all(),
        scorers=ScScorer.query.filter_by(active=True).order_by(ScScorer.name).all(),
        sports=SPORTS,
        stages=STAGES,
    )


@scorecard_bp.route("/admin/matches/<int:match_id>/delete", methods=["POST"])
@admin_required
def admin_delete_match(match_id):
    match = ScMatch.query.get_or_404(match_id)
    db.session.delete(match)
    db.session.commit()
    flash("Fixture removed.", "success")
    return redirect(url_for("scorecard.admin_matches"))


# ----------------------------------------------------------------- scorers --
@scorecard_bp.route("/admin/scorers", methods=["GET", "POST"])
@admin_required
def admin_scorers():
    if request.method == "POST":
        name = _clean(request.form.get("name"), 80)
        passcode = request.form.get("passcode") or ""
        if not name or len(passcode) < 4:
            flash("A scorer needs a name and a passcode of at least 4 characters.", "error")
        elif ScScorer.query.filter(db.func.lower(ScScorer.name) == name.lower()).first():
            # Case-insensitive, matching how sign-in looks names up — two
            # scorers differing only in case would make that lookup ambiguous.
            flash(f"There is already a scorer called {name}.", "error")
        else:
            db.session.add(ScScorer(name=name, passcode_hash=generate_password_hash(passcode)))
            db.session.commit()
            flash(f"{name} can now sign in and score.", "success")
        return redirect(url_for("scorecard.admin_scorers"))

    return render_template(
        "scorecard/admin_scorers.html",
        scorers=ScScorer.query.order_by(ScScorer.name).all(),
    )


@scorecard_bp.route("/admin/scorers/<int:scorer_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_scorer(scorer_id):
    scorer = ScScorer.query.get_or_404(scorer_id)
    scorer.active = not scorer.active
    db.session.commit()
    flash(f"{scorer.name} {'can score again' if scorer.active else 'can no longer sign in'}.", "info")
    return redirect(url_for("scorecard.admin_scorers"))


# ------------------------------------------------------------- EMS linking --
@scorecard_bp.route("/admin/link", methods=["GET", "POST"])
@admin_required
def admin_link():
    """Optional. Until this runs, ems_owner_id stays NULL and nothing depends
    on the auction data existing at all."""
    TeamOwner = team_owner_model()

    if request.method == "POST":
        linked = 0
        for team in ScTeam.query.all():
            raw = request.form.get(f"owner_{team.id}")
            wanted = int(raw) if raw else None
            if team.ems_owner_id != wanted:
                team.ems_owner_id = wanted
                linked += 1
        db.session.commit()
        flash(f"Updated {linked} link(s).", "success")
        return redirect(url_for("scorecard.admin_link"))

    owners = TeamOwner.query.order_by(TeamOwner.team_name).all()
    by_name = {(o.team_name or "").strip().lower(): o.id for o in owners}
    teams = ScTeam.query.order_by(ScTeam.sport, ScTeam.name).all()
    suggestions = {
        team.id: by_name.get(team.name.strip().lower())
        for team in teams
    }
    return render_template(
        "scorecard/admin_link.html",
        teams=teams, owners=owners, suggestions=suggestions,
    )
