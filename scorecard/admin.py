"""Admin screens: teams, players, fixtures, scorer passcodes, EMS linking.

These reuse the EMS admin session via its own ``admin_required`` decorator, so
there is no second committee login to remember.

Everything is gender-aware: teams, fixtures, and scorers are all managed per
(sport, gender) pair so Basketball Men and Basketball Women never share a
table, fixture list, or scorer roster.
"""

from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from . import scorecard_bp
from .config import STAGES, STATUS_LIVE, STATUS_SCHEDULED, rules_for, GENDERS
from .ems import SPORTS, admin_required, db, team_owner_model, team_wallet_model
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
        gender = request.form.get("gender") or "Men"
        if not name or sport not in SPORTS or gender not in GENDERS:
            flash("A team needs a name, a valid sport, and a valid gender.", "error")
        else:
            db.session.add(ScTeam(
                name=name,
                short_name=_clean(request.form.get("short_name"), 6).upper() or None,
                sport=sport,
                gender=gender,
            ))
            db.session.commit()
            flash(f"Added {name} ({sport} – {gender}).", "success")
        return redirect(url_for("scorecard.admin_teams"))

    # Group teams by (sport, gender) so each table is separate.
    teams = ScTeam.query.order_by(ScTeam.sport, ScTeam.gender, ScTeam.name).all()
    grouped = {}
    for team in teams:
        grouped.setdefault((team.sport, team.gender), []).append(team)
    return render_template(
        "scorecard/admin_teams.html",
        grouped=grouped,
        sports=SPORTS,
        genders=GENDERS,
    )


def _short_tag(name):
    """'DVG BULLDOZERS' -> 'DB', 'Agni squad' -> 'AS', 'Venom' -> 'VENO'."""
    words = ["".join(ch for ch in w if ch.isalnum()) for w in name.split()]
    words = [w for w in words if w]
    if len(words) >= 2:
        return "".join(w[0] for w in words)[:4].upper()
    return (words[0][:4] if words else name[:4]).upper()


def import_franchise_teams():
    """Create a scorecard team for every franchise registered in the auction.

    Every TeamWallet row is one (owner, sport, gender) the franchise entered,
    so each becomes one ScTeam linked by ems_owner_id. Existing teams are
    matched (by link or by name) and only linked, never duplicated.
    Returns (teams_created, teams_linked).
    """
    TeamWallet = team_wallet_model()
    created = linked = 0
    for wallet in TeamWallet.query.all():
        owner = wallet.team_owner if hasattr(wallet, "team_owner") else None
        if owner is None:
            owner = team_owner_model().query.get(wallet.team_owner_id)
        if owner is None or wallet.sport not in SPORTS or wallet.gender not in GENDERS:
            continue
        name = (owner.team_name or "").strip()
        if not name:
            continue
        team = ScTeam.query.filter(
            ScTeam.sport == wallet.sport,
            ScTeam.gender == wallet.gender,
            db.or_(
                ScTeam.ems_owner_id == owner.id,
                db.func.lower(ScTeam.name) == name.lower(),
            ),
        ).first()
        if team is None:
            db.session.add(ScTeam(
                name=name[:100],
                short_name=_short_tag(name),
                sport=wallet.sport,
                gender=wallet.gender,
                ems_owner_id=owner.id,
                logo=owner.team_logo or owner.team_owner_photo,
            ))
            created += 1
        elif team.ems_owner_id != owner.id:
            team.ems_owner_id = owner.id
            linked += 1
    db.session.commit()
    return created, linked


@scorecard_bp.route("/admin/sync_squads", methods=["POST"])
@admin_required
def admin_sync_squads():
    """Import every auction franchise as a scorecard team, then fill squads."""
    from .sync import sync_all_auction_squads
    created, linked = import_franchise_teams()
    count = sync_all_auction_squads()
    flash(
        f"Imported {created} new franchise team(s), linked {linked} existing team(s), "
        f"and synced {count} auctioned player(s) into squads.",
        "success",
    )
    return redirect(request.referrer or url_for("scorecard.admin_teams"))


@scorecard_bp.route("/admin/teams/<int:team_id>/edit", methods=["POST"])
@admin_required
def admin_edit_team(team_id):
    team = ScTeam.query.get_or_404(team_id)
    name = _clean(request.form.get("name"), 80)
    short_name = _clean(request.form.get("short_name"), 12)
    sport = request.form.get("sport")
    gender = request.form.get("gender")
    if name:
        team.name = name
    if short_name is not None:
        team.short_name = short_name.upper() if short_name else None
    if sport:
        team.sport = sport
    if gender:
        team.gender = gender
    db.session.commit()
    flash(f"Team {team.name} updated.", "success")
    return redirect(url_for("scorecard.admin_teams"))


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
    # Optional filters so the admin can view one (sport, gender) at a time.
    filter_sport = request.args.get("sport") or request.form.get("sport") or ""
    filter_gender = request.args.get("gender") or request.form.get("gender") or "Men"
    if filter_gender not in GENDERS:
        filter_gender = "Men"

    if request.method == "POST":
        sport = _clean(request.form.get("sport"), 50)
        team_a = request.form.get("team_a_id")
        team_b = request.form.get("team_b_id")
        gender = request.form.get("gender") or "Men"
        instant = request.form.get("start_now") == "1"

        if sport not in SPORTS or not team_a or not team_b or gender not in GENDERS:
            flash("Pick a sport, both teams, and a gender.", "error")
            return redirect(url_for(
                "scorecard.admin_matches", sport=filter_sport, gender=filter_gender
            ))
        if team_a == team_b:
            flash("Team A and Team B are the same team — pick two different ones.", "error")
            return redirect(url_for(
                "scorecard.admin_matches", sport=filter_sport, gender=filter_gender
            ))

        side_a = ScTeam.query.get(int(team_a))
        side_b = ScTeam.query.get(int(team_b))
        wrong = []
        for t in (side_a, side_b):
            if not t:
                wrong.append("unknown team")
            elif t.sport != sport:
                wrong.append(f"{t.name} does not play {sport}")
            elif t.gender != gender:
                wrong.append(f"{t.name} is a {t.gender} team, not {gender}")
        if wrong:
            flash("; ".join(wrong) + ". Pick teams from that sport and gender.", "error")
            return redirect(url_for(
                "scorecard.admin_matches", sport=filter_sport, gender=filter_gender
            ))

        match = ScMatch(
            sport=sport,
            gender=gender,
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
        return redirect(url_for(
            "scorecard.admin_matches", sport=sport, gender=gender
        ))

    # Build the match list, optionally filtered.
    query = ScMatch.query
    if filter_sport:
        query = query.filter_by(sport=filter_sport)
    if filter_gender:
        query = query.filter_by(gender=filter_gender)
    matches = query.order_by(ScMatch.starts_at.desc(), ScMatch.id.desc()).all()

    # Teams available for selection, grouped by (sport, gender).
    teams = ScTeam.query.order_by(ScTeam.sport, ScTeam.gender, ScTeam.name).all()
    scorer_rows = ScScorer.query.order_by(ScScorer.name).all()
    scorer_map = {s.id: s.name for s in scorer_rows}
    scorers = [
        {"id": s.id, "name": s.name, "gender": s.gender}
        for s in scorer_rows if s.active
    ]

    # The template serialises these with |tojson, so hand it plain dicts.
    teams = [
        {"id": t.id, "name": t.name, "sport": t.sport, "gender": t.gender}
        for t in teams
    ]

    return render_template(
        "scorecard/admin_matches.html",
        matches=matches,
        teams=teams,
        scorers=scorers,
        scorer_map=scorer_map,
        sports=SPORTS,
        genders=GENDERS,
        stages=STAGES,
        filter_sport=filter_sport,
        filter_gender=filter_gender,
    )


@scorecard_bp.route("/admin/matches/<int:match_id>/delete", methods=["POST"])
@admin_required
def admin_delete_match(match_id):
    match = ScMatch.query.get_or_404(match_id)
    sport = match.sport
    gender = match.gender
    db.session.delete(match)
    db.session.commit()
    flash("Fixture removed.", "success")
    return redirect(url_for("scorecard.admin_matches", sport=sport, gender=gender))


@scorecard_bp.route("/admin/matches/<int:match_id>/grant_edit", methods=["POST"])
@admin_required
def admin_grant_scorer_edit(match_id):
    """Community admin (EMS admin) grants the assigned scorer permission to edit this fixture later."""
    match = ScMatch.query.get_or_404(match_id)
    sport = match.sport
    gender = match.gender
    if not match.assigned_scorer_id:
        flash("Assign a scorer to this fixture before granting edit-later access.", "error")
        return redirect(url_for("scorecard.admin_matches", sport=sport, gender=gender))
    if match.status != "final":
        flash("Fix/finalize the fixture first, then grant edit-later access.", "error")
        return redirect(url_for("scorecard.admin_matches", sport=sport, gender=gender))
    match.scorer_can_edit_later = not bool(getattr(match, "scorer_can_edit_later", False))
    db.session.commit()
    scorer = ScScorer.query.get(match.assigned_scorer_id)
    scorer_name = scorer.name if scorer else "the assigned scorer"
    if match.scorer_can_edit_later:
        flash(f"{scorer_name} can now edit this fixture later using their scorer login.", "success")
    else:
        flash(f"Edit-later access removed for {scorer_name} on this fixture.", "info")
    return redirect(url_for("scorecard.admin_matches", sport=sport, gender=gender))


def _int_field(value, low=0, high=999):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


@scorecard_bp.route("/admin/matches/<int:match_id>/result", methods=["GET", "POST"])
@admin_required
def admin_enter_result(match_id):
    """Type in the final result of a match that was not scored live.

    For when the console could not be used (no signal at the ground, the site
    down). The result is stored as ordinary events — one per player who is
    credited, one for any remainder — so standings, leaderboards, the public
    match page and later corrections all work exactly as for a live-scored
    match. Anything already recorded is voided first (kept, not deleted) when
    the committee asks for a clean replace.
    """
    match = ScMatch.query.get_or_404(match_id)
    rules = rules_for(match.sport)
    players = {
        "a": ScPlayer.query.filter_by(team_id=match.team_a_id).order_by(ScPlayer.name).all(),
        "b": ScPlayer.query.filter_by(team_id=match.team_b_id).order_by(ScPlayer.name).all(),
    }
    live_events = [e for e in match.events if not e.voided]
    back = url_for("scorecard.admin_matches", sport=match.sport, gender=match.gender)

    if request.method == "POST":
        new_events = []
        errors = []

        if match.sport == "Badminton":
            last_game = None
            for n in (1, 2, 3):
                raw_a = request.form.get(f"g{n}a", "").strip()
                raw_b = request.form.get(f"g{n}b", "").strip()
                if not raw_a and not raw_b:
                    continue
                pa, pb = _int_field(raw_a or 0, 0, 30), _int_field(raw_b or 0, 0, 30)
                if pa is None or pb is None:
                    errors.append(f"Game {n}: points must be between 0 and 30.")
                    continue
                for side, pts in (("a", pa), ("b", pb)):
                    if pts:
                        new_events.append({"side": side, "points": pts, "player_id": None, "clock": f"Game {n}"})
                last_game = f"Game {n}"
            if not new_events and not errors:
                errors.append("Enter at least one game score.")
            final_period = last_game or rules["periods"][0]
        else:
            scores = {}
            for side in ("a", "b"):
                total = _int_field(request.form.get(f"score_{side}"), 0, 999)
                if total is None:
                    errors.append(f"Enter a valid final score for {match.team_a.name if side == 'a' else match.team_b.name}.")
                    continue
                scores[side] = total
                valid_ids = {p.id for p in players[side]}
                credited = 0
                for pid, pts in zip(request.form.getlist(f"player_{side}"), request.form.getlist(f"pts_{side}")):
                    if not pid and not pts:
                        continue
                    player_id = _int_field(pid, 1, 10**9)
                    points = _int_field(pts, 1, 999)
                    if player_id not in valid_ids or points is None:
                        errors.append("Each credited player needs a player from that team and points above 0.")
                        continue
                    credited += points
                    new_events.append({"side": side, "points": points, "player_id": player_id, "clock": "Result"})
                if credited > total:
                    errors.append(
                        f"Players credited with {credited} for "
                        f"{match.team_a.name if side == 'a' else match.team_b.name}, but the final score is {total}."
                    )
                elif total - credited > 0:
                    new_events.append({"side": side, "points": total - credited, "player_id": None, "clock": "Result"})
            final_period = rules["periods"][-1] if match.sport != "Basketball" else "Q4"

        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "scorecard/admin_result.html", match=match, rules=rules,
                players=players, live_events=live_events, form=request.form, back=back,
            )

        if request.form.get("replace") == "1":
            for event in live_events:
                event.voided = True

        for item in new_events:
            db.session.add(ScEvent(
                match_id=match.id,
                team_id=match.team_a_id if item["side"] == "a" else match.team_b_id,
                player_id=item["player_id"],
                clock=item["clock"],
                kind="Result entry",
                points=item["points"],
            ))
        db.session.flush()
        db.session.refresh(match)

        match.status = "final"
        match.period = final_period
        match.recalculate()
        a, b = match.score_a or 0, match.score_b or 0
        match.winner_team_id = match.team_a_id if a > b else (match.team_b_id if b > a else None)
        if not match.note:
            match.note = "Result entered by the committee after the match."
        db.session.commit()
        flash(
            f"Result saved: {match.team_a.name} {a} – {b} {match.team_b.name}. "
            "Standings and the public scoreboard are updated.",
            "success",
        )
        return redirect(back)

    return render_template(
        "scorecard/admin_result.html", match=match, rules=rules,
        players=players, live_events=live_events, form={}, back=back,
    )


# ----------------------------------------------------------------- scorers --
@scorecard_bp.route("/admin/scorers", methods=["GET", "POST"])
@admin_required
def admin_scorers():
    filter_gender = request.args.get("gender") or request.form.get("gender") or "Men"
    if filter_gender not in GENDERS:
        filter_gender = "Men"

    if request.method == "POST":
        name = _clean(request.form.get("name"), 80)
        passcode = request.form.get("passcode") or ""
        gender = request.form.get("gender") or "Men"
        if not name or len(passcode) < 4 or gender not in GENDERS:
            flash("A scorer needs a name, a passcode of at least 4 characters, and a valid gender.", "error")
        elif ScScorer.query.filter(
            db.func.lower(ScScorer.name) == name.lower(),
            ScScorer.gender == gender,
        ).first():
            flash(f"There is already a {gender} scorer called {name}.", "error")
        else:
            db.session.add(ScScorer(name=name, passcode_hash=generate_password_hash(passcode), gender=gender))
            db.session.commit()
            flash(f"{name} ({gender}) can now sign in and score.", "success")
        return redirect(url_for("scorecard.admin_scorers", gender=filter_gender))

    scorers = ScScorer.query.filter_by(
        gender=filter_gender
    ).order_by(ScScorer.name).all()

    return render_template(
        "scorecard/admin_scorers.html",
        scorers=scorers,
        genders=GENDERS,
        filter_gender=filter_gender,
    )


@scorecard_bp.route("/admin/scorers/<int:scorer_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_scorer(scorer_id):
    scorer = ScScorer.query.get_or_404(scorer_id)
    gender = scorer.gender
    scorer.active = not scorer.active
    db.session.commit()
    flash(f"{scorer.name} {'can score again' if scorer.active else 'can no longer sign in'}.", "info")
    return redirect(url_for("scorecard.admin_scorers", gender=gender))


@scorecard_bp.route("/admin/scorers/<int:scorer_id>/delete", methods=["POST"])
@admin_required
def admin_delete_scorer(scorer_id):
    scorer = ScScorer.query.get_or_404(scorer_id)
    name = scorer.name
    gender = scorer.gender
    db.session.delete(scorer)
    db.session.commit()
    flash(f"Scorer {name} was permanently deleted.", "success")
    return redirect(url_for("scorecard.admin_scorers", gender=gender))


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
    teams = ScTeam.query.order_by(ScTeam.sport, ScTeam.gender, ScTeam.name).all()
    suggestions = {
        team.id: by_name.get(team.name.strip().lower())
        for team in teams
    }
    return render_template(
        "scorecard/admin_link.html",
        teams=teams, owners=owners, suggestions=suggestions,
    )


# -------------------------------------------------- tournament export & report --
@scorecard_bp.route("/admin/export_tournament_excel")
@admin_required
def export_tournament_excel():
    """Generates a structured multi-sheet Excel file of the entire tournament."""
    from io import BytesIO
    from flask import send_file
    import xlsxwriter
    from .stats import standings_for, leaders_for, _sport_gender_combos

    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    title_format = workbook.add_format({"bold": True, "font_size": 13, "font_color": "#5b1f1f"})
    header_format = workbook.add_format({"bold": True, "bg_color": "#ecc35c", "font_color": "#1b1c19", "border": 1})
    cell_format = workbook.add_format({"border": 1})

    # Sheet 1: Matches & Results
    matches = ScMatch.query.order_by(ScMatch.starts_at.asc(), ScMatch.id.asc()).all()
    ws_matches = workbook.add_worksheet("Matches & Results")
    ws_matches.write(0, 0, "GM LEAGUE SEASON 4 — TOURNAMENT FIXTURES & RESULTS", title_format)

    match_headers = ["Match ID", "Sport", "Stage", "Gender", "Team A", "Score A", "Score B", "Team B", "Winner", "Status", "Period", "Scorers", "Venue", "Scheduled At"]
    ws_matches.write_row(2, 0, match_headers, header_format)

    for row_idx, m in enumerate(matches, start=3):
        winner_name = m.team_a.name if m.winner_team_id == m.team_a_id else (m.team_b.name if m.winner_team_id == m.team_b_id else ("Draw" if m.status == "final" else "—"))
        row_data = [
            m.id,
            m.sport,
            m.stage,
            m.gender,
            m.team_a.name,
            m.score_a or 0,
            m.score_b or 0,
            m.team_b.name,
            winner_name,
            m.status.capitalize(),
            m.period or "—",
            m.scorers_summary or "—",
            m.venue or "—",
            m.starts_at.strftime("%Y-%m-%d %H:%M") if m.starts_at else "—",
        ]
        ws_matches.write_row(row_idx, 0, row_data, cell_format)

    ws_matches.set_column(0, 0, 10)
    ws_matches.set_column(1, 3, 14)
    ws_matches.set_column(4, 4, 22)
    ws_matches.set_column(5, 6, 10)
    ws_matches.set_column(7, 8, 22)
    ws_matches.set_column(9, 10, 12)
    ws_matches.set_column(11, 11, 32)
    ws_matches.set_column(12, 13, 20)

    # Sheet 2: League Standings (per sport + gender)
    ws_standings = workbook.add_worksheet("League Standings")
    ws_standings.write(0, 0, "GM LEAGUE SEASON 4 — OFFICIAL STANDINGS", title_format)
    stand_headers = ["Sport", "Gender", "Rank", "Team Name", "Tag", "Played", "Won", "Drawn", "Lost", "For", "Against", "+/-", "Points"]
    ws_standings.write_row(2, 0, stand_headers, header_format)

    curr_row = 3
    for sport, gender in _sport_gender_combos():
        table = standings_for(sport, gender)
        for r in table:
            stand_row = [
                sport,
                gender,
                r.get("position", 1),
                r["team"].name,
                r["team"].tag,
                r["played"],
                r["won"],
                r["drawn"],
                r["lost"],
                r["scored"],
                r["conceded"],
                r["diff"],
                r["points"],
            ]
            ws_standings.write_row(curr_row, 0, stand_row, cell_format)
            curr_row += 1

    ws_standings.set_column(0, 1, 12)
    ws_standings.set_column(2, 2, 24)
    ws_standings.set_column(3, 11, 10)

    # Sheet 3: Top Scorers (per sport + gender)
    ws_scorers = workbook.add_worksheet("Top Scorers")
    ws_scorers.write(0, 0, "GM LEAGUE SEASON 4 — TOP SCORERS & LEADERS", title_format)
    score_headers = ["Sport", "Gender", "Rank", "Player Name", "Team", "Jersey", "Total Points/Goals"]
    ws_scorers.write_row(2, 0, score_headers, header_format)

    s_row = 3
    for sport, gender in _sport_gender_combos():
        leaders = leaders_for(sport, gender, limit=20)
        for idx, (player, pts, entries) in enumerate(leaders, start=1):
            s_data = [
                sport,
                gender,
                idx,
                player.name,
                player.team.name if player.team else "—",
                f"#{player.jersey_no}" if player.jersey_no else "—",
                pts,
            ]
            ws_scorers.write_row(s_row, 0, s_data, cell_format)
            s_row += 1

    ws_scorers.set_column(0, 1, 12)
    ws_scorers.set_column(2, 3, 24)
    ws_scorers.set_column(4, 5, 16)

    workbook.close()
    output.seek(0)
    return send_file(
        output,
        download_name="GML_Season4_Tournament_Report.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@scorecard_bp.route("/admin/tournament_report")
@admin_required
def tournament_report():
    """Print-ready official tournament summary with college branding."""
    from .stats import standings_for, leaders_for, _sport_gender_combos, _sport_gender_combos
    matches = ScMatch.query.order_by(ScMatch.starts_at.asc(), ScMatch.id.asc()).all()
    sport_tables = {}
    sport_leaders = {}
    for sport, gender in _sport_gender_combos():
        sport_tables[(sport, gender)] = standings_for(sport, gender)
        sport_leaders[(sport, gender)] = leaders_for(sport, gender, limit=10)
    return render_template(
        "scorecard/admin_report.html",
        matches=matches,
        sport_tables=sport_tables,
        sport_leaders=sport_leaders,
        sports=SPORTS,
        genders=GENDERS,
        now=datetime.utcnow(),
    )
