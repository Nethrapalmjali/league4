"""Offline Scorer App: the installable app shell and the JSON API behind it.

Why this exists: the ground has little or no signal, and the web console saves
each tap with a round trip to the server — no signal, no score. The Scorer App
keeps every tap on the device first and pushes it here whenever a connection
appears, so scoring never waits on the network.

How it stays correct on a bad connection:

* every change carries an id the device made up; ``sc_sync_op`` remembers
  which ids were applied, so a change sent twice is applied once
* changes are applied in the order the device recorded them
* "undo" names the exact event it removes rather than "the last one", so two
  devices on one match cannot undo each other's work
* the device signs in with a bearer token rather than a cookie, so it can sync
  hours later after the browser session has gone

Routes (all under /scores):
    GET  /app                 the app itself (works offline once loaded)
    GET  /sw.js               service worker that keeps the app on the device
    GET  /app.webmanifest     makes the app installable
    POST /api/app/login       name + passcode  -> token
    GET  /api/app/bundle      matches this user may score, with squads
    POST /api/app/sync        queued changes   -> per-change result + fresh state
"""

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    current_app, flash, jsonify, make_response, redirect, render_template,
    request, send_from_directory, session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

from . import scorecard_bp
from .config import (
    GENDERS, STAGES, STATUS_FINAL, STATUS_LIVE, STATUS_SCHEDULED, action_for, rules_for,
)
from .ems import (
    SPORTS, admin_credentials_ok, admin_required, admin_secret_material,
    admin_session_fingerprint, db,
)
from .models import ScEvent, ScMatch, ScPlayer, ScScorer, ScSyncOp, ScTeam

TOKEN_SALT = "gml-scorer-app-v1"
# Long enough to cover a whole tournament day plus a night without signal.
TOKEN_MAX_AGE = 4 * 24 * 3600
MAX_OPS_PER_SYNC = 200
APP_VERSION = "1.2.0"

# Failed sign-ins per (IP, name), kept in memory: enough to stop someone
# guessing passcodes from the stands without adding a table for it.
_failed_logins = {}
_FAIL_WINDOW = 10 * 60
_FAIL_LIMIT = 10


# ------------------------------------------------------------------ auth ----
def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt=TOKEN_SALT)


def _admin_fingerprint():
    key = current_app.secret_key
    key = key.encode() if isinstance(key, str) else key
    return hmac.new(key, admin_secret_material().encode(), hashlib.sha256).hexdigest()[:16]


def _issue_token(role, scorer_id=None, name=""):
    payload = {"r": role, "sid": scorer_id, "n": name}
    if role == "admin":
        payload["pf"] = _admin_fingerprint()
    return _serializer().dumps(payload)


def _admin_token_current(data):
    """True when a committee token was issued under today's admin password."""
    pf = data.get("pf")
    return bool(pf) and hmac.compare_digest(str(pf), _admin_fingerprint())


def _identity_from_request():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, "Sign in to the Scorer App."
    try:
        data = _serializer().loads(header[7:].strip(), max_age=TOKEN_MAX_AGE)
    except SignatureExpired:
        return None, "Your sign-in has expired. Sign in again to sync."
    except BadSignature:
        return None, "Sign in to the Scorer App."
    if data.get("r") == "scorer":
        scorer = db.session.get(ScScorer, data.get("sid"))
        if not scorer or not scorer.active:
            return None, "This scorer login has been switched off by the committee."
        return _scorer_identity(scorer), None
    if data.get("r") == "admin":
        # Tokens from app v1.0 carry no fingerprint; they keep scoring until
        # they expire, but cannot open the admin website (see app_web_login).
        if "pf" in data and not _admin_token_current(data):
            return None, "The committee password was changed. Sign in again."
        return ADMIN_IDENTITY, None
    return None, "Sign in to the Scorer App."


ADMIN_IDENTITY = {"role": "admin", "scorer_id": None, "name": "Committee", "actor": "admin"}
IMPORT_IDENTITY = {"role": "admin", "scorer_id": None, "name": "Backup import", "actor": "import"}


def _scorer_identity(scorer):
    return {
        "role": "scorer", "scorer_id": scorer.id, "name": scorer.name,
        "actor": f"scorer:{scorer.id}:{scorer.name}"[:100],
    }


def app_auth_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        identity, error = _identity_from_request()
        if not identity:
            return jsonify({"error": error, "auth": False}), 401
        return view(identity, *args, **kwargs)
    return wrapper


def _may_score(identity, match):
    """Same rule as the web console (auth.may_score), for a token holder."""
    if identity["role"] == "admin":
        return True
    sid = identity["scorer_id"]
    if match.assigned_scorer_id == sid and getattr(match, "scorer_can_edit_later", False):
        return True
    return match.assigned_scorer_id in (None, sid)


def _may_edit_final(identity, match):
    if identity["role"] == "admin":
        return True
    return (
        match.assigned_scorer_id == identity["scorer_id"]
        and bool(getattr(match, "scorer_can_edit_later", False))
    )


def _too_many_failures(key):
    now = time.time()
    recent = [t for t in _failed_logins.get(key, []) if now - t < _FAIL_WINDOW]
    _failed_logins[key] = recent
    return len(recent) >= _FAIL_LIMIT


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


# ------------------------------------------------------------- app shell ----
@scorecard_bp.route("/app")
def scorer_app():
    response = make_response(render_template(
        "scorecard/app.html", app_version=APP_VERSION,
    ))
    # The service worker owns caching for this page; the browser must not
    # serve it stale from its own HTTP cache.
    response.headers["Cache-Control"] = "no-cache"
    return response


@scorecard_bp.route("/sw.js")
def scorer_app_sw():
    response = send_from_directory(
        current_app.root_path + "/scorecard/static/app", "sw.js",
        mimetype="application/javascript", max_age=0,
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/scores/"
    return response


@scorecard_bp.route("/app.webmanifest")
def scorer_app_manifest():
    manifest = {
        "name": "GM League Scorer",
        "short_name": "GML Scorer",
        "description": "Record match scores at the ground, with or without internet.",
        "id": "/scores/app",
        "start_url": "/scores/app",
        "scope": "/scores/app",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#5b1f1f",
        "theme_color": "#5b1f1f",
        "icons": [
            {"src": url_for("scorecard.static", filename="app/icon-192.png"),
             "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": url_for("scorecard.static", filename="app/icon-512.png"),
             "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": url_for("scorecard.static", filename="app/icon-512.png"),
             "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    response = jsonify(manifest)
    response.headers["Content-Type"] = "application/manifest+json"
    return response


# ------------------------------------------------------------ admin app -----
# The community admin website as its own installable app. Its scope is the
# whole site, so every admin page — auction, players, fixtures, reports — opens
# inside the app window instead of bouncing out to a browser tab.
@scorecard_bp.route("/admin-app.webmanifest")
def admin_app_manifest():
    icon = lambda name: url_for("scorecard.static", filename=f"app/{name}")  # noqa: E731
    manifest = {
        "name": "GM League Admin",
        "short_name": "GML Admin",
        "description": "Community admin panel: auction, players, teams, fixtures, scoring and reports.",
        "id": "/admin/app",
        "start_url": "/admin/dashboard",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#5b1f1f",
        "theme_color": "#5b1f1f",
        "icons": [
            {"src": icon("admin-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": icon("admin-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": icon("admin-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        # Long-press the app icon (Android) or right-click it (desktop).
        "shortcuts": [
            {"name": "Fixtures & scoring", "url": "/scores/admin/matches",
             "icons": [{"src": icon("admin-192.png"), "sizes": "192x192", "type": "image/png"}]},
            {"name": "Scorer App (offline)", "url": "/scores/app",
             "icons": [{"src": icon("icon-192.png"), "sizes": "192x192", "type": "image/png"}]},
            {"name": "Live scoreboard", "url": "/scores/",
             "icons": [{"src": icon("icon-192.png"), "sizes": "192x192", "type": "image/png"}]},
        ],
    }
    response = jsonify(manifest)
    response.headers["Content-Type"] = "application/manifest+json"
    return response


@scorecard_bp.route("/admin-sw.js")
def admin_app_sw():
    response = send_from_directory(
        current_app.root_path + "/scorecard/static/app", "admin-sw.js",
        mimetype="application/javascript", max_age=0,
    )
    response.headers["Cache-Control"] = "no-cache"
    # Allowed to look after the whole site, which the admin app spans.
    response.headers["Service-Worker-Allowed"] = "/"
    return response


def _safe_next(raw, default="/admin/dashboard"):
    """Only ever redirect to a page on this site."""
    raw = (raw or "").strip()
    if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw and "\n" not in raw:
        return raw
    return default


@scorecard_bp.route("/api/app/web-login", methods=["POST"])
def app_web_login():
    """Open the admin website from the Scorer App without typing the password.

    The app posts its committee sign-in here as an ordinary form (never in a
    URL, so it cannot end up in history or logs). Only a committee token issued
    under the current admin password is accepted; a scorer login, an old app
    version's token or anything else lands on the normal admin login page.
    """
    token = (request.form.get("token") or "").strip()
    target = _safe_next(request.form.get("next"))
    data = None
    if token:
        try:
            data = _serializer().loads(token, max_age=TOKEN_MAX_AGE)
        except (BadSignature, SignatureExpired):
            data = None
    if not data or data.get("r") != "admin" or not _admin_token_current(data):
        flash("Sign in with the committee admin login to open the admin website.", "info")
        return redirect(url_for("admin_login", next=target))
    session.clear()
    session.permanent = True
    session["admin_logged_in"] = True
    session["admin_pf"] = admin_session_fingerprint()
    return redirect(target)


# ------------------------------------------------------------------- API ----
@scorecard_bp.route("/api/app/login", methods=["POST"])
def app_login():
    data = request.get_json(silent=True) or {}
    role = data.get("role") or "scorer"
    name = (data.get("name") or "").strip()[:80]
    secret = data.get("passcode") or ""
    key = (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(), name.lower())

    if _too_many_failures(key):
        return jsonify({"error": "Too many wrong attempts. Wait ten minutes and try again."}), 429

    if role == "admin":
        if name and secret and admin_credentials_ok(name, secret):
            return _no_store(jsonify({
                "token": _issue_token("admin", None, "Committee"),
                "me": {"role": "admin", "name": "Committee", "scorer_id": None},
            }))
    else:
        scorer = ScScorer.query.filter(
            db.func.lower(ScScorer.name) == name.lower(),
            ScScorer.active.is_(True),
        ).first()
        if scorer and check_password_hash(scorer.passcode_hash, secret):
            return _no_store(jsonify({
                "token": _issue_token("scorer", scorer.id, scorer.name),
                "me": {"role": "scorer", "name": scorer.name, "scorer_id": scorer.id},
            }))

    _failed_logins.setdefault(key, []).append(time.time())
    return jsonify({"error": "That name and passcode do not match."}), 401


def _visible_matches(identity):
    if identity["role"] == "admin":
        recent = datetime.utcnow() - timedelta(days=2)
        query = ScMatch.query.filter(db.or_(
            ScMatch.status.in_([STATUS_SCHEDULED, STATUS_LIVE]),
            db.and_(ScMatch.status == STATUS_FINAL, ScMatch.updated_at >= recent),
        ))
    else:
        sid = identity["scorer_id"]
        query = ScMatch.query.filter(
            db.or_(
                ScMatch.status.in_([STATUS_SCHEDULED, STATUS_LIVE]),
                db.and_(
                    ScMatch.status == STATUS_FINAL,
                    ScMatch.assigned_scorer_id == sid,
                    ScMatch.scorer_can_edit_later.is_(True),
                ),
            ),
            db.or_(ScMatch.assigned_scorer_id.is_(None), ScMatch.assigned_scorer_id == sid),
        )
    return query.order_by(ScMatch.starts_at.asc(), ScMatch.id.asc()).all()


def _team_dict(team):
    if not team:
        return {"id": None, "name": "?", "tag": "?"}
    return {"id": team.id, "name": team.name, "tag": team.tag}


def _match_state(match, identity):
    rules = rules_for(match.sport)
    players = {}
    for side, team_id in (("a", match.team_a_id), ("b", match.team_b_id)):
        players[side] = [
            {"id": p.id, "name": p.name, "jersey_no": p.jersey_no}
            for p in ScPlayer.query.filter_by(team_id=team_id, active=True)
            .order_by(ScPlayer.name).all()
        ]
    events = (
        ScEvent.query.filter_by(match_id=match.id, voided=False)
        .order_by(ScEvent.id.asc()).all()
    )
    return {
        "id": match.id,
        "sport": match.sport,
        "gender": match.gender,
        "stage": match.stage,
        "venue": match.venue,
        "starts_at": match.starts_at.isoformat() if match.starts_at else None,
        "status": match.status,
        "period": match.period,
        "periods": rules["periods"],
        "actions": rules["actions"],
        "team_a": _team_dict(match.team_a),
        "team_b": _team_dict(match.team_b),
        "score_a": match.score_a or 0,
        "score_b": match.score_b or 0,
        "can_edit_final": _may_edit_final(identity, match),
        "players": players,
        "events": [
            {
                "uid": e.client_id or f"srv-{e.id}",
                "side": match.side_of(e.team_id),
                "kind": e.kind,
                "points": e.points or 0,
                "clock": e.clock,
                "player_id": e.player_id,
                "player": e.player.name if e.player else None,
            }
            for e in events
        ],
        "server_updated_at": match.updated_at.isoformat() if match.updated_at else None,
    }


def _setup_for_new_matches():
    """Everything an admin needs to start a match with no signal: every team,
    every squad and the scoring rules of every sport."""
    teams = ScTeam.query.order_by(ScTeam.sport, ScTeam.gender, ScTeam.name).all()
    squads = {}
    for p in ScPlayer.query.filter_by(active=True).order_by(ScPlayer.name).all():
        squads.setdefault(str(p.team_id), []).append({"id": p.id, "name": p.name, "jersey_no": p.jersey_no})
    return {
        "teams": [{"id": t.id, "name": t.name, "tag": t.tag, "sport": t.sport, "gender": t.gender} for t in teams],
        "squads": squads,
        "rules": {sport: rules_for(sport) for sport in SPORTS},
        "stages": STAGES,
        "genders": GENDERS,
    }


@scorecard_bp.route("/api/app/bundle")
@app_auth_required
def app_bundle(identity):
    matches = _visible_matches(identity)
    payload = {
        "me": {"role": identity["role"], "name": identity["name"], "scorer_id": identity["scorer_id"]},
        "server_time": datetime.utcnow().isoformat(),
        "app_version": APP_VERSION,
        "matches": [_match_state(m, identity) for m in matches],
    }
    if identity["role"] == "admin":
        payload["setup"] = _setup_for_new_matches()
    return _no_store(jsonify(payload))


# --------------------------------------------------------------- applying ---
class _Reject(Exception):
    pass


def _parse_device_time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    # A device with a wrong clock must not stamp an event years away.
    now = datetime.utcnow()
    if abs((now - parsed).total_seconds()) > 7 * 24 * 3600:
        return None
    return parsed


def _check_writable(identity, match):
    if not _may_score(identity, match):
        raise _Reject("This match is assigned to another scorer.")
    if match.status == STATUS_FINAL and not _may_edit_final(identity, match):
        raise _Reject("This match is already finished.")


def _apply_event(identity, match, op_id, payload, at):
    _check_writable(identity, match)
    side = payload.get("side")
    if side not in ("a", "b"):
        raise _Reject("Pick a team.")
    action = action_for(match.sport, payload.get("action") or "")
    if not action:
        raise _Reject("That action is not valid for this sport.")
    if match.sport == "Badminton" and (match.badminton_stats or {}).get("match_winner"):
        # Every game is decided; another point has no game to belong to.
        raise _Reject("This badminton match is already won. End it, or undo the last point first.")
    team_id = match.team_a_id if side == "a" else match.team_b_id

    player_id = payload.get("player_id") or None
    if player_id:
        try:
            player = ScPlayer.query.filter_by(id=int(player_id), team_id=team_id).first()
        except (TypeError, ValueError):
            player = None
        player_id = player.id if player else None

    clock = (str(payload.get("clock") or "")).strip()[:8] or None
    if not clock:
        if match.sport in ("Basketball", "Badminton"):
            clock = match.period or rules_for(match.sport)["periods"][0]
        elif match.sport == "Kabaddi":
            clock = match.period or "H1"

    event = ScEvent(
        match_id=match.id, team_id=team_id, player_id=player_id,
        clock=clock, kind=action["label"], points=action["points"],
        client_id=op_id,
    )
    if at:
        event.created_at = at
    db.session.add(event)

    if match.status == STATUS_SCHEDULED:
        match.status = STATUS_LIVE
        match.period = match.period or rules_for(match.sport)["periods"][0]

    db.session.flush()
    db.session.refresh(match)  # pick up the new event in match.events
    match.recalculate()
    if match.sport == "Badminton":
        stats = match.badminton_stats
        if stats:
            if stats.get("match_winner"):
                match.winner_team_id = match.team_a_id if stats["match_winner"] == "a" else match.team_b_id
            elif stats.get("active_game") and stats["active_game"] != match.period:
                match.period = stats["active_game"]


def _apply_undo(identity, match, payload):
    _check_writable(identity, match)
    target = str(payload.get("target") or "")
    event = None
    if target.startswith("srv-"):
        try:
            event = db.session.get(ScEvent, int(target[4:]))
        except ValueError:
            event = None
    elif target:
        event = ScEvent.query.filter_by(client_id=target).first()
    if not event or event.match_id != match.id or event.voided:
        return "noop"
    event.voided = True
    db.session.flush()
    db.session.refresh(match)
    match.recalculate()
    if match.sport == "Badminton":
        stats = match.badminton_stats
        if stats and not stats.get("match_winner"):
            match.winner_team_id = None
            if stats.get("active_game"):
                match.period = stats["active_game"]
    return "applied"


def _apply_period(identity, match, payload):
    _check_writable(identity, match)
    wanted = payload.get("period")
    if wanted not in rules_for(match.sport)["periods"]:
        raise _Reject("Unknown period for this sport.")
    match.period = wanted
    match.updated_at = datetime.utcnow()


def _apply_status(identity, match, payload):
    if not _may_score(identity, match):
        raise _Reject("This match is assigned to another scorer.")
    wanted = payload.get("status")
    if wanted not in (STATUS_LIVE, STATUS_FINAL):
        raise _Reject("Unknown status.")
    if match.status == STATUS_FINAL and wanted == STATUS_LIVE and not _may_edit_final(identity, match):
        raise _Reject("This match is already finished.")
    match.status = wanted
    if wanted == STATUS_LIVE and not match.period:
        match.period = rules_for(match.sport)["periods"][0]
    if wanted == STATUS_FINAL:
        a, b = match.score_a or 0, match.score_b or 0
        match.winner_team_id = match.team_a_id if a > b else (match.team_b_id if b > a else None)
    match.updated_at = datetime.utcnow()


NEW_MATCH_PREFIX = "n:"
OP_KINDS = ("create", "event", "undo", "period", "status")


def _resolve_match_id(raw):
    """A match made offline is called "n:<id of its create change>" on the
    device until the server has created it; afterwards that name still works."""
    if isinstance(raw, str) and raw.startswith(NEW_MATCH_PREFIX):
        row = db.session.get(ScSyncOp, raw[len(NEW_MATCH_PREFIX):][:40])
        if row and row.kind == "create" and row.result == "applied":
            return row.match_id
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _apply_create(identity, payload, at):
    if identity["role"] != "admin":
        raise _Reject("Only the committee can create matches.")
    sport, gender = payload.get("sport"), payload.get("gender")
    if sport not in SPORTS or gender not in GENDERS:
        raise _Reject("Pick a sport and category.")
    try:
        team_a = db.session.get(ScTeam, int(payload.get("team_a_id")))
        team_b = db.session.get(ScTeam, int(payload.get("team_b_id")))
    except (TypeError, ValueError):
        team_a = team_b = None
    if not team_a or not team_b or team_a.id == team_b.id:
        raise _Reject("Pick two different teams.")
    for team in (team_a, team_b):
        if team.sport != sport or team.gender != gender:
            raise _Reject(f"{team.name} does not play {sport} ({gender}).")
    stage = payload.get("stage") if payload.get("stage") in STAGES else "League"
    match = ScMatch(
        sport=sport, gender=gender, stage=stage,
        team_a_id=team_a.id, team_b_id=team_b.id,
        venue=(str(payload.get("venue") or "").strip()[:120] or None),
        starts_at=at or datetime.utcnow(),
        status=STATUS_SCHEDULED,
    )
    db.session.add(match)
    db.session.flush()
    return match


def _apply_one(identity, op):
    """Apply one queued change in its own transaction.

    Returns (status, error, match_id) — match_id is the real server id, so the
    device can rename a match it created offline.
    """
    op_id = str(op.get("id") or "").strip()[:40]
    kind = op.get("type")
    if not op_id or kind not in OP_KINDS:
        return "rejected", "Malformed change.", None
    done = db.session.get(ScSyncOp, op_id)
    if done:
        return "duplicate", None, done.match_id

    at = _parse_device_time(op.get("at"))
    payload = op.get("payload") or {}
    actor = identity.get("actor")
    match = None
    match_id = None
    if kind != "create":
        match_id = _resolve_match_id(op.get("match_id"))
        match = db.session.get(ScMatch, match_id) if match_id else None
        if not match:
            return "rejected", "This match no longer exists.", match_id

    status = "applied"
    try:
        # Claim the id first: a second request carrying the same change blocks
        # here on the primary key and then fails, instead of double-scoring.
        row = ScSyncOp(op_id=op_id, match_id=match_id, kind=kind, device_at=at, actor=actor)
        db.session.add(row)
        db.session.flush()
        if kind == "create":
            match = _apply_create(identity, payload, at)
            row.match_id = match_id = match.id
        elif kind == "event":
            _apply_event(identity, match, op_id, payload, at)
        elif kind == "undo":
            status = _apply_undo(identity, match, payload)
        elif kind == "period":
            _apply_period(identity, match, payload)
        elif kind == "status":
            _apply_status(identity, match, payload)
        row.result = status
        db.session.commit()
        return status, None, match_id
    except IntegrityError:
        db.session.rollback()
        done = db.session.get(ScSyncOp, op_id)
        return "duplicate", None, done.match_id if done else match_id
    except _Reject as exc:
        db.session.rollback()
        # Record the rejection so a re-send is not re-evaluated differently later.
        try:
            db.session.add(ScSyncOp(op_id=op_id, match_id=match_id, kind=kind,
                                    device_at=at, result="rejected", actor=actor))
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
        return "rejected", str(exc), match_id


def apply_ops(identity, ops):
    """Apply changes in order. Returns (results, ids of matches touched)."""
    results = []
    touched = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        try:
            status, error, match_id = _apply_one(identity, op)
        except Exception as exc:  # noqa: BLE001 — one bad change must not block the queue
            db.session.rollback()
            current_app.logger.exception("Scorer App sync failed for op %s: %s", op.get("id"), exc)
            try:
                from .errorlog import record
                record(exc)
            except Exception:  # noqa: BLE001
                pass
            # Stop here: later changes depend on this one, and the device will
            # retry the whole tail on its next attempt.
            results.append({"id": op.get("id"), "status": "retry", "error": "Server error, will retry."})
            break
        results.append({"id": op.get("id"), "status": status, "error": error, "match_id": match_id})
        if match_id and match_id not in touched:
            touched.append(match_id)
    return results, touched


@scorecard_bp.route("/api/app/sync", methods=["POST"])
@app_auth_required
def app_sync(identity):
    data = request.get_json(silent=True) or {}
    ops = data.get("ops") or []
    if not isinstance(ops, list):
        return jsonify({"error": "Bad request."}), 400
    results, touched = apply_ops(identity, ops[:MAX_OPS_PER_SYNC])

    states = []
    for match_id in touched:
        match = db.session.get(ScMatch, match_id)
        if match:
            states.append(_match_state(match, identity))

    return _no_store(jsonify({
        "results": results,
        "matches": states,
        "server_time": datetime.utcnow().isoformat(),
    }))


# ------------------------------------------------------- committee pages ---
def upload_status():
    """Per uploader: when they last uploaded and how many changes today."""
    today = datetime.utcnow() - timedelta(hours=18)
    rows = (
        db.session.query(
            ScSyncOp.actor,
            db.func.max(ScSyncOp.applied_at),
            db.func.count(ScSyncOp.op_id),
        )
        .filter(ScSyncOp.actor.isnot(None), ScSyncOp.applied_at >= today)
        .group_by(ScSyncOp.actor)
        .all()
    )
    out = {}
    now = datetime.utcnow()
    for actor, last, count in rows:
        out[actor] = {"last": last, "count": count,
                      "mins": int((now - last).total_seconds() // 60) if last else None}
    return out


def actor_label(actor):
    if not actor:
        return "?"
    if actor.startswith("scorer:"):
        return actor.split(":", 2)[2]
    return {"admin": "Committee (app)", "import": "Backup import"}.get(actor, actor)


@scorecard_bp.route("/admin/import_backup", methods=["GET", "POST"])
@admin_required
def admin_import_backup():
    """Upload a Scorer App backup file from a phone that never got signal."""
    summary = None
    if request.method == "POST":
        upload = request.files.get("backup")
        import json
        try:
            data = json.loads(upload.read().decode("utf-8")) if upload else None
            ops = data.get("outbox") if isinstance(data, dict) else None
            if not isinstance(ops, list):
                raise ValueError
        except (ValueError, UnicodeDecodeError, AttributeError):
            flash("That is not a Scorer App backup file. In the app use ☰ → Download backup file.", "error")
            return redirect(url_for("scorecard.admin_import_backup"))
        results, touched = apply_ops(IMPORT_IDENTITY, ops)
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        summary = {
            "who": ((data.get("me") or {}).get("name")) or "unknown device",
            "exported_at": data.get("exported_at"),
            "counts": counts,
            "errors": [r["error"] for r in results if r["status"] == "rejected" and r.get("error")][:10],
            "matches": [db.session.get(ScMatch, mid) for mid in touched],
        }
    return render_template("scorecard/admin_import.html", summary=summary)
