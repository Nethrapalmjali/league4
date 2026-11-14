"""Admin screens: teams, players, fixtures, scorer passcodes, EMS linking.

These reuse the EMS admin session via its own ``admin_required`` decorator, so
there is no second committee login to remember.

Everything is gender-aware: teams, fixtures, and scorers are all managed per
(sport, gender) pair so Basketball Men and Basketball Women never share a
table, fixture list, or scorer roster.
"""

from datetime import datetime

from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from . import scorecard_bp
from .config import STAGES, STATUS_FINAL, STATUS_LIVE, STATUS_SCHEDULED, STATUSES, rules_for, GENDERS
from .ems import SPORTS, admin_required, db, team_owner_model, team_wallet_model
from .models import ScEvent, ScMatch, ScPlayer, ScScorer, ScTeam, badminton_round_won, match_numbers


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
    if request.method == "POST" and request.form.get("action") == "jerseys":
        # Every player's number in one save.
        changed, bad = 0, []
        for player in team.players:
            if f"jersey_{player.id}" not in request.form:
                continue
            raw = (request.form.get(f"jersey_{player.id}") or "").strip().lstrip("#")
            if raw and (len(raw) > 4 or not raw.isalnum()):
                bad.append(player.name)
                continue
            value = raw or None
            if player.jersey_no != value:
                player.jersey_no = value
                changed += 1
        db.session.commit()
        numbers = [p.jersey_no for p in team.players if p.active and p.jersey_no]
        clashes = sorted({n for n in numbers if numbers.count(n) > 1})
        if bad:
            flash("Not saved for " + ", ".join(bad) + ": a jersey number is up to 4 letters or digits.", "error")
        if clashes:
            flash("Two players share number " + ", ".join(f"#{n}" for n in clashes) + " — the scorer will not be able to tell them apart.", "error")
        flash(f"Saved {changed} jersey number(s) for {team.name}.", "success")
        return redirect(url_for("scorecard.admin_players", team_id=team.id))

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

    from .app_api import _player_sort_key
    players = sorted(team.players, key=lambda p: (not p.active, _player_sort_key(p)))
    return render_template("scorecard/admin_players.html", team=team, players=players)


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


def _squad_player(match, players, side, name):
    """The squad player called ``name`` on that side, added to the team's
    squad first if nobody there has that name (an extra player on the day)."""
    name = " ".join((name or "").split())[:100]
    if not name:
        return None
    same = next((p for p in players[side] if p.name.strip().lower() == name.lower()), None)
    if same:
        return same
    player = ScPlayer(team_id=match.team_a_id if side == "a" else match.team_b_id, name=name, active=True)
    db.session.add(player)
    db.session.flush()
    players[side].append(player)
    return player


def _chosen_potm(match, players, squad_ids):
    """Player of the Match from the form: an extra player typed in (someone
    who played but is not in the squad list) wins over the list. They are
    added to that team's squad once, so the award counts on every page."""
    if (request.form.get("potm_extra_name") or "").strip():
        side = "b" if request.form.get("potm_extra_side") == "b" else "a"
        player = _squad_player(match, players, side, request.form.get("potm_extra_name"))
        squad_ids.add(player.id)
        return player.id
    try:
        pid = int(request.form.get("potm_player_id") or 0) or None
    except ValueError:
        pid = None
    return pid if pid in squad_ids else None


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
    squad_ids = {p.id for p in players["a"]} | {p.id for p in players["b"]}

    def chosen_potm():
        return _chosen_potm(match, players, squad_ids)

    def potm_given():
        return bool(request.form.get("potm_player_id") or (request.form.get("potm_extra_name") or "").strip())

    if request.method == "POST" and request.form.get("action") == "potm":
        # Player of the Match only — the score is left exactly as it is.
        match.potm_player_id = chosen_potm()
        match.updated_at = datetime.utcnow()
        db.session.commit()
        picked = next((p.name for side in players.values() for p in side if p.id == match.potm_player_id), None)
        flash(f"Player of the Match: {picked}." if picked else "Player of the Match cleared — the top performer is shown instead.", "success")
        return redirect(url_for("scorecard.admin_enter_result", match_id=match.id))

    if request.method == "POST" and request.form.get("action") == "switch_bo5" and match.sport == "Badminton":
        if match.match_format == "bo5":
            flash("This match is already best of 5 rounds.", "success")
        else:
            match.switch_to_best_of_5()
            match.updated_at = datetime.utcnow()
            db.session.commit()
            bs = match.badminton_stats
            rounds = ", ".join(f"{g['label']} {g['a']}–{g['b']}" for g in bs["games_list"] if g["a"] or g["b"])
            flash("Switched to best of 5 rounds to 15. The points already scored were replayed under the new rules"
                  + (f": {rounds}" if rounds else "") + ". Correct any round below if it differs from the court.", "success")
        return redirect(url_for("scorecard.admin_enter_result", match_id=match.id))

    if request.method == "POST" and match.sport == "Badminton" and match.match_format:
        return _save_badminton_rounds(match, players, live_events, back, chosen_potm)

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
        if potm_given():
            match.potm_player_id = chosen_potm()
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


# ------------------------------------------------------------- edit match --
# One-click buttons on the Edit match page: the points a single action can add.
SCORE_STEPS = {"Basketball": [1, 2, 3], "Kabaddi": [1, 2, 3]}


def _points_by_key(match):
    """Active scored points per (side, player id or None) — what each player
    (and the team, for points with no player) has right now."""
    totals = {}
    for e in match.events:
        if e.voided or (e.points or 0) <= 0:
            continue
        side = match.side_of(e.team_id)
        if side:
            key = (side, e.player_id)
            totals[key] = totals.get(key, 0) + e.points
    return totals


def _apply_points_change(match, side, player_id, delta, clock):
    """Add or take away ``delta`` points for one player (or the team).
    Taking away voids the latest of their scoring events (kept, not deleted)
    and puts back any part-event remainder, so nothing is ever lost."""
    team_id = match.team_a_id if side == "a" else match.team_b_id
    if delta > 0:
        match.events.append(ScEvent(team_id=team_id, player_id=player_id, clock=clock,
                                    kind="Result entry", points=delta))
        return
    remove = -delta
    mine = [e for e in match.events if not e.voided and (e.points or 0) > 0
            and e.team_id == team_id and e.player_id == player_id]
    for e in sorted(mine, key=lambda e: (e.id is not None, e.id or 0), reverse=True):
        if remove <= 0:
            break
        e.voided = True
        if e.points > remove:
            match.events.append(ScEvent(team_id=team_id, player_id=player_id, clock=e.clock,
                                        kind="Result entry", points=e.points - remove))
            remove = 0
        else:
            remove -= e.points


@scorecard_bp.route("/admin/matches/<int:match_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_match(match_id):
    """Everything about one match on one page: teams, time, venue, stage,
    scorer, status, every player's points (one-click buttons), the winner and
    Player of the Match. Live scoring keeps working alongside it: the page
    sends what it showed when it opened, and only the changes the committee
    made are applied, so points a scorer adds meanwhile are kept."""
    match = ScMatch.query.get_or_404(match_id)
    badminton = match.sport == "Badminton"
    teams = ScTeam.query.filter_by(sport=match.sport, gender=match.gender).order_by(ScTeam.name).all()
    scorers = [s for s in ScScorer.query.order_by(ScScorer.name).all()
               if s.active or s.id == match.assigned_scorer_id]
    back = url_for("scorecard.admin_matches", sport=match.sport, gender=match.gender)

    def load_players():
        return {
            "a": ScPlayer.query.filter_by(team_id=match.team_a_id).order_by(ScPlayer.name).all(),
            "b": ScPlayer.query.filter_by(team_id=match.team_b_id).order_by(ScPlayer.name).all(),
        }

    players = load_players()

    def page(form, errors=()):
        for message in errors:
            flash(message, "error")
        current = _points_by_key(match)
        squad = {side: {p.id for p in players[side]} for side in ("a", "b")}
        # Points credited to someone no longer in the squad count as team points.
        team_pts = {side: sum(v for (s, pid), v in current.items() if s == side and pid not in squad[side])
                    for side in ("a", "b")}
        return render_template(
            "scorecard/admin_edit_match.html", match=match, teams=teams, scorers=scorers,
            players=players, current=current, team_pts=team_pts, form=form, back=back,
            stages=STAGES, statuses=STATUSES, badminton=badminton,
            steps=SCORE_STEPS.get(match.sport, [1]),
        )

    if request.method != "POST":
        return page({})

    f = request.form
    errors = []
    try:
        new_a, new_b = int(f.get("team_a_id") or 0), int(f.get("team_b_id") or 0)
    except ValueError:
        new_a = new_b = 0
    team_ids = {t.id for t in teams}
    if new_a not in team_ids or new_b not in team_ids:
        errors.append(f"Pick two {match.gender} {match.sport} teams.")
    elif new_a == new_b:
        errors.append("Team A and Team B are the same team — pick two different ones.")
    status = f.get("status")
    if status not in STATUSES:
        errors.append("Pick a status.")
    starts_at = _parse_when(f.get("starts_at"))
    if f.get("starts_at") and starts_at is None:
        errors.append("The date and time is not valid.")
    scorer_id = None
    if f.get("scorer_id"):
        try:
            scorer_id = int(f.get("scorer_id"))
        except ValueError:
            scorer_id = -1
        if scorer_id not in {s.id for s in scorers}:
            errors.append("Pick a scorer from the list.")
    winner_choice = f.get("winner") or "auto"
    if winner_choice not in ("auto", "a", "b", "draw"):
        winner_choice = "auto"
    if errors:
        return page(f, errors)

    teams_changed = {new_a, new_b} != {match.team_a_id, match.team_b_id}
    if teams_changed:
        # A team replaced by another: its points move with the side, but the
        # player credits are cleared (those players are not in the new team).
        old_ids = [t for t in (match.team_a_id, match.team_b_id) if t not in (new_a, new_b)]
        new_ids = [t for t in (new_a, new_b) if t not in (match.team_a_id, match.team_b_id)]
        moved = dict(zip(old_ids, new_ids))
        for e in match.events:
            if e.team_id in moved:
                e.team_id, e.player_id = moved[e.team_id], None
        if match.potm_player_id:
            potm = db.session.get(ScPlayer, match.potm_player_id)
            if potm is None or potm.team_id in moved:
                match.potm_player_id = None
    match.team_a_id, match.team_b_id = new_a, new_b
    match.starts_at = starts_at
    match.venue = _clean(f.get("venue")) or None
    match.stage = f.get("stage") if f.get("stage") in STAGES else (match.stage or "League")
    match.assigned_scorer_id = scorer_id
    match.note = _clean(f.get("note"), 200) or None
    match.status = status
    if not badminton:
        period = f.get("period")
        if period in match.periods:
            match.period = period
        elif status == STATUS_LIVE and not match.period:
            match.period = match.periods[0]

    if teams_changed:
        db.session.flush()
        db.session.expire(match, ["team_a", "team_b"])
        players = load_players()
    elif not badminton:
        # Scores: apply only what the committee changed on the page.
        squad = {side: {p.id for p in players[side]} for side in ("a", "b")}
        changes = {}
        for side in ("a", "b"):
            rows = [(p.id, f"p{side}_{p.id}") for p in players[side]] + [(None, f"p{side}_team")]
            for pid, name in rows:
                if name not in f:
                    continue
                new, old = _int_field(f.get(name), 0, 999), _int_field(f.get(name + "_was"), 0, 999)
                if new is None or old is None:
                    errors.append("Points must be whole numbers from 0 to 999.")
                    continue
                if new != old:
                    changes[(side, pid)] = changes.get((side, pid), 0) + new - old
            for name, pts in zip(f.getlist(f"extra_{side}_name"), f.getlist(f"extra_{side}_pts")):
                if not name.strip():
                    continue
                value = _int_field(pts or 0, 0, 999)
                if value is None:
                    errors.append(f"Points for {name.strip()} must be a whole number.")
                    continue
                player = _squad_player(match, players, side, name)
                squad[side].add(player.id)
                if value:
                    changes[(side, player.id)] = changes.get((side, player.id), 0) + value
        if errors:
            db.session.rollback()
            players = load_players()
            return page(f, errors)
        clock = "Result" if status == STATUS_FINAL else (match.period or "Result")
        for (side, pid), delta in changes.items():
            if pid is None:
                # The team row also holds points of players who left the squad.
                current = _points_by_key(match)
                for (s, other), pts in current.items():
                    if s == side and other is not None and other not in squad[side] and delta < 0:
                        take = min(pts, -delta)
                        _apply_points_change(match, side, other, -take, clock)
                        delta += take
            if delta:
                _apply_points_change(match, side, pid, delta, clock)

    db.session.flush()
    match.recalculate()
    a, b = match.score_a or 0, match.score_b or 0
    if badminton:
        if status == STATUS_FINAL and not match.winner_team_id:
            match.winner_team_id = match.team_a_id if a > b else (match.team_b_id if b > a else None)
        elif status != STATUS_FINAL:
            bs = match.badminton_stats
            if not (bs and bs["match_winner"]):
                match.winner_team_id = None
    elif status != STATUS_FINAL:
        match.winner_team_id = None
    elif a != b:
        leader = "a" if a > b else "b"
        if winner_choice in ("a", "b") and winner_choice != leader:
            db.session.rollback()
            players = load_players()
            return page(f, [f"The score is {a}–{b}, so {match.team_a.name if leader == 'a' else match.team_b.name} won. "
                            "Pick a winner by hand only when the score is level."])
        match.winner_team_id = match.team_a_id if leader == "a" else match.team_b_id
    else:
        match.winner_team_id = {"a": match.team_a_id, "b": match.team_b_id}.get(winner_choice)

    all_ids = {p.id for side in players.values() for p in side}
    if (f.get("potm_extra_name") or "").strip() or "potm_player_id" in f:
        match.potm_player_id = _chosen_potm(match, players, all_ids)
    match.updated_at = datetime.utcnow()
    db.session.commit()

    team_a, team_b = db.session.get(ScTeam, match.team_a_id), db.session.get(ScTeam, match.team_b_id)
    shown = {STATUS_SCHEDULED: "upcoming", STATUS_LIVE: "live", STATUS_FINAL: "finished"}[status]
    message = f"Saved: {team_a.name} {match.score_a or 0} – {match.score_b or 0} {team_b.name} ({shown})."
    if teams_changed:
        message += " Teams changed — the points stayed with each side; credit them to the new players below if needed."
    flash(message, "success")
    return redirect(url_for("scorecard.admin_edit_match", match_id=match.id))


def badminton_round_errors(fmt, scores):
    """Check typed round scores. ``scores`` is [(a, b) or None per round, in
    order]. Returns a list of problems (empty when it all adds up)."""
    won = badminton_round_won(fmt)
    target, win_by, cap, need = fmt["target"], fmt["win_by"], fmt["cap"], fmt["need"]
    errors = []
    wins = {"a": 0, "b": 0}
    decided_by = None   # round label after which the match was already won
    open_round = None   # first round that is not finished
    for r, score in zip(fmt["rounds"], scores):
        name = f"Round {r['number']} ({r['label']})" if fmt["unit"] == "Round" else r["label"]
        if score is None or score == (0, 0):
            if open_round is None and decided_by is None:
                open_round = name
            continue
        a, b = score
        if decided_by:
            errors.append(f"{name} is not required — the match was already won after {decided_by}. Clear its scores.")
            continue
        if open_round:
            errors.append(f"{name} has scores, but {open_round} is not finished yet. Finish the rounds in order.")
            continue
        wa, wb = won(a, b), won(b, a)
        if wa and wb:
            errors.append(f"{name}: both sides cannot reach {target}.")
            continue
        if wa or wb:
            hi, lo = (a, b) if wa else (b, a)
            # The round stops the moment it is won: no "15–3 then 16–3".
            if hi > target and not (lo == hi - win_by or (hi == cap and lo == cap - 1)):
                errors.append(f"{name}: {hi}–{lo} is not possible — the round ends when a side reaches {target}"
                              + (f" with a {win_by}-point lead." if win_by > 1 else "."))
                continue
            wins["a" if wa else "b"] += 1
            if wins["a"] >= need or wins["b"] >= need:
                decided_by = name
        else:
            open_round = name
    return errors


def _save_badminton_rounds(match, players, live_events, back, chosen_potm):
    """Save the round-by-round scores the committee typed (or corrected).

    Only the difference is written: a round that went up gets one extra
    "Result entry" for the missing points; a round that went down has its
    latest points hidden (kept, not deleted) until it matches. Rounds that
    were already right are not touched, so players credited live keep their
    points.
    """
    fmt = match.format_info
    rounds = fmt["rounds"]
    scores, errors = [], []
    for r in rounds:
        n = r["number"]
        raw_a = (request.form.get(f"r{n}a") or "").strip()
        raw_b = (request.form.get(f"r{n}b") or "").strip()
        if not raw_a and not raw_b:
            scores.append(None)
            continue
        a, b = _int_field(raw_a or 0, 0, fmt["cap"]), _int_field(raw_b or 0, 0, fmt["cap"])
        if a is None or b is None:
            errors.append(f"Round {n}: points must be whole numbers from 0 to {fmt['cap']}.")
            scores.append(None)
            continue
        scores.append((a, b))
    errors += badminton_round_errors(fmt, scores)
    if not errors and all(s is None or s == (0, 0) for s in scores) and not live_events:
        errors.append("Enter the score of at least one round.")
    if errors:
        for message in errors:
            flash(message, "error")
        return render_template(
            "scorecard/admin_result.html", match=match, rules=rules_for(match.sport),
            players=players, live_events=live_events, form=request.form, back=back,
        )

    current, placed = match.badminton_points_by_round()
    changed = 0
    for r, score in zip(rounds, scores):
        wanted = {"a": score[0] if score else 0, "b": score[1] if score else 0}
        for side in ("a", "b"):
            have = current[r["key"]][side]
            if wanted[side] == have:
                continue
            changed += 1
            if wanted[side] < have:
                mine = [e for e, key in placed if key == r["key"] and match.side_of(e.team_id) == side]
                for event in reversed(mine):
                    if have <= wanted[side]:
                        break
                    event.voided = True
                    have -= event.points or 1
            if wanted[side] > have:
                match.events.append(ScEvent(
                    team_id=match.team_a_id if side == "a" else match.team_b_id,
                    clock=r["key"], kind="Result entry", points=wanted[side] - have,
                ))

    if match.status == STATUS_SCHEDULED and any(scores):
        match.status = STATUS_LIVE
    match.recalculate()
    stats = match.badminton_stats
    if stats["match_winner"]:
        match.status = STATUS_FINAL
        match.recalculate()
    elif request.form.get("finish") == "1":
        # Walkover, retirement or a shortened match: finished without 3 rounds.
        match.status = STATUS_FINAL
        match.recalculate()
        a, b = match.score_a or 0, match.score_b or 0
        match.winner_team_id = match.team_a_id if a > b else (match.team_b_id if b > a else None)
    elif match.status == STATUS_FINAL:
        match.status = STATUS_LIVE  # a correction re-opened the result
        match.recalculate()
    if request.form.get("potm_player_id") or (request.form.get("potm_extra_name") or "").strip():
        match.potm_player_id = chosen_potm()
    if match.status == STATUS_FINAL and not match.note:
        match.note = "Round scores entered by the committee."
    db.session.commit()

    stats = match.badminton_stats
    if stats["match_winner"]:
        flash(f"Saved. {stats['result_text']} — the rest of the rounds are marked Not required. "
              "Standings and the public scoreboard are updated.", "success")
    elif match.status == STATUS_FINAL:
        flash(f"Saved and finished at {match.score_a}–{match.score_b} in rounds.", "success")
    else:
        flash(f"Saved{'' if changed else ' (nothing changed)'}. Rounds {stats['tally']} — "
              f"{stats['active_label']} is next. The match stays live.", "success")
    return redirect(url_for("scorecard.admin_enter_result", match_id=match.id))


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
        elif ScScorer.query.filter(db.func.lower(ScScorer.name) == name.lower()).first():
            # Sign-in looks a scorer up by name alone, so a name must be unique
            # across both categories or one of the two could never sign in.
            flash(f"There is already a scorer called {name}. Use a different name (e.g. {name} W).", "error")
        else:
            db.session.add(ScScorer(name=name, passcode_hash=generate_password_hash(passcode), gender=gender))
            db.session.commit()
            flash(f"{name} ({gender}) can now sign in and score.", "success")
            # Shown once on the next page so the committee can send it on;
            # the passcode cannot be read back after this.
            session["sc_new_scorer"] = {"name": name, "passcode": passcode}
        return redirect(url_for("scorecard.admin_scorers", gender=filter_gender))

    scorers = ScScorer.query.filter_by(
        gender=filter_gender
    ).order_by(ScScorer.name).all()

    from .app_api import upload_status
    try:
        uploads = upload_status()
    except Exception:  # noqa: BLE001 — a status widget must not break the page
        db.session.rollback()
        uploads = {}

    new_scorer = session.pop("sc_new_scorer", None)
    share_text = None
    if new_scorer:
        share_text = (
            f"GM League scoring login\n"
            f"Name: {new_scorer['name']}\nPasscode: {new_scorer['passcode']}\n\n"
            f"1. Open {url_for('scorecard.scorer_app', _external=True)} with internet\n"
            f"2. Tap Install (iPhone: Share → Add to Home Screen)\n"
            f"3. Sign in once. After that it works without internet."
        )

    return render_template(
        "scorecard/admin_scorers.html",
        scorers=scorers,
        genders=GENDERS,
        filter_gender=filter_gender,
        uploads=uploads,
        new_scorer=new_scorer,
        share_text=share_text,
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
def numbered_matches(matches):
    """[(sport, gender, [(match no, match), ...]), ...] — each sport and
    category numbered 1, 2, 3 ... in the order the matches were entered
    (oldest first), sports in the site's usual order. Same numbers as shown
    everywhere else (models.match_numbers)."""
    numbers = match_numbers()
    groups = {}
    for m in sorted(matches, key=lambda m: m.id):
        groups.setdefault((m.sport, m.gender), []).append(m)
    sport_rank = {s: i for i, s in enumerate(SPORTS)}
    gender_rank = {g: i for i, g in enumerate(GENDERS)}
    keys = sorted(groups, key=lambda k: (sport_rank.get(k[0], 99), k[0], gender_rank.get(k[1], 9), k[1] or ""))
    return [(sport, gender, [(numbers.get(m.id), m) for m in groups[(sport, gender)]]) for sport, gender in keys]


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

    match_headers = ["Match No.", "Sport", "Stage", "Gender", "Team A", "Score A", "Score B", "Team B", "Winner", "Status", "Period", "Scorers", "Venue", "Scheduled At"]
    ws_matches.write_row(2, 0, match_headers, header_format)

    numbered = [(no, m) for _, _, rows in numbered_matches(matches) for no, m in rows]
    for row_idx, (no, m) in enumerate(numbered, start=3):
        winner_name = m.team_a.name if m.winner_team_id == m.team_a_id else (m.team_b.name if m.winner_team_id == m.team_b_id else ("Draw" if m.status == "final" else "—"))
        row_data = [
            f"Match {no}",
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

    # Sheet 3: Player stats — every credited player, with the stats of their
    # sport and Player of the Match awards (per sport + gender block).
    from .stats import player_table
    ws_scorers = workbook.add_worksheet("Player Stats")
    ws_scorers.write(0, 0, "GM LEAGUE SEASON 4 — PLAYER STATISTICS & AWARDS", title_format)
    s_row = 2
    for sport, gender in _sport_gender_combos():
        table = player_table(sport, gender)
        if not table["players"]:
            continue
        ws_scorers.write(s_row, 0, f"{sport} — {gender}", title_format)
        s_row += 1
        headers = ["Rank", "Player", "Team", "Jersey", "Matches", "⭐ Player of the Match"] + [c["label"] for c in table["categories"]]
        ws_scorers.write_row(s_row, 0, headers, header_format)
        s_row += 1
        for idx, s in enumerate(table["players"], start=1):
            p = s["player"]
            row = [idx, p.name, p.team.name if p.team else "—", f"#{p.jersey_no}" if p.jersey_no else "—",
                   s["played"], s["potm"]] + [s[c["key"]] for c in table["categories"]]
            ws_scorers.write_row(s_row, 0, row, cell_format)
            s_row += 1
        s_row += 1
    if s_row == 2:
        ws_scorers.write(2, 0, "No player has been credited with a score yet.")
    ws_scorers.set_column(0, 0, 7)
    ws_scorers.set_column(1, 2, 24)
    ws_scorers.set_column(3, 4, 9)
    ws_scorers.set_column(5, 12, 14)

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
    from sqlalchemy.orm import selectinload
    from .stats import standings_for, player_table, _sport_gender_combos
    matches = (ScMatch.query.options(selectinload(ScMatch.events))
               .order_by(ScMatch.starts_at.asc(), ScMatch.id.asc()).all())
    sport_tables = {}
    sport_leaders = {}
    for sport, gender in _sport_gender_combos():
        sport_tables[(sport, gender)] = standings_for(sport, gender)
        table = player_table(sport, gender)
        if table["players"]:
            sport_leaders[(sport, gender)] = table
    return render_template(
        "scorecard/admin_report.html",
        matches=matches,
        match_groups=numbered_matches(matches),
        sport_tables=sport_tables,
        sport_leaders=sport_leaders,
        sports=SPORTS,
        genders=GENDERS,
        now=datetime.utcnow(),
    )
