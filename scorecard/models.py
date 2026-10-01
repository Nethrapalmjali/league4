"""Scorecard tables.

Every table is prefixed ``sc_`` and none of them declare a foreign key into the
event-management schema. The only link is a pair of nullable integer columns —
``sc_team.ems_owner_id`` and ``sc_player.ems_player_id`` — which stay empty
until someone runs the linking screen. That keeps the two systems independent:
drop the ``sc_`` tables and the EMS is exactly as it was.
"""

from datetime import datetime

from .config import STATUS_SCHEDULED
from .ems import db


class ScTeam(db.Model):
    __tablename__ = "sc_team"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    short_name = db.Column(db.String(6))
    sport = db.Column(db.String(50), nullable=False)
    gender = db.Column(db.String(10), default="Men")
    logo = db.Column(db.String(200))

    # Soft reference to team_owner.id. Never a ForeignKey — the scorecard must
    # keep working whether or not that row exists.
    ems_owner_id = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    players = db.relationship(
        "ScPlayer", backref="team", lazy="select",
        cascade="all, delete-orphan", order_by="ScPlayer.name",
    )

    @property
    def tag(self):
        """Short label for the scoreboard — falls back to the first letters."""
        return (self.short_name or self.name[:3]).upper()


class ScPlayer(db.Model):
    __tablename__ = "sc_player"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("sc_team.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    jersey_no = db.Column(db.String(4))
    ems_player_id = db.Column(db.Integer)  # soft reference to player.id
    active = db.Column(db.Boolean, default=True)


class ScScorer(db.Model):
    __tablename__ = "sc_scorer"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    passcode_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ScMatch(db.Model):
    __tablename__ = "sc_match"

    id = db.Column(db.Integer, primary_key=True)
    sport = db.Column(db.String(50), nullable=False)
    gender = db.Column(db.String(10), default="Men")
    stage = db.Column(db.String(40), default="League")

    team_a_id = db.Column(db.Integer, db.ForeignKey("sc_team.id"), nullable=False)
    team_b_id = db.Column(db.Integer, db.ForeignKey("sc_team.id"), nullable=False)

    venue = db.Column(db.String(120))
    starts_at = db.Column(db.DateTime)

    status = db.Column(db.String(12), default=STATUS_SCHEDULED, index=True)
    period = db.Column(db.String(20))

    # Recalculated from sc_event on every write, never typed in by hand. Stored
    # rather than summed on read so the public polling endpoint stays cheap.
    score_a = db.Column(db.Integer, default=0)
    score_b = db.Column(db.Integer, default=0)

    winner_team_id = db.Column(db.Integer)
    assigned_scorer_id = db.Column(db.Integer)
    note = db.Column(db.String(200))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    team_a = db.relationship("ScTeam", foreign_keys=[team_a_id])
    team_b = db.relationship("ScTeam", foreign_keys=[team_b_id])

    events = db.relationship(
        "ScEvent", backref="match", lazy="select",
        cascade="all, delete-orphan", order_by="ScEvent.id.desc()",
    )

    def side_of(self, team_id):
        """'a' or 'b' — which side of this fixture a team is on."""
        if team_id == self.team_a_id:
            return "a"
        if team_id == self.team_b_id:
            return "b"
        return None

    def recalculate(self):
        """Re-derive both scores from the events that have not been voided."""
        totals = {"a": 0, "b": 0}
        for event in self.events:
            if event.voided:
                continue
            side = self.side_of(event.team_id)
            if side:
                totals[side] += event.points or 0
        self.score_a = totals["a"]
        self.score_b = totals["b"]
        self.updated_at = datetime.utcnow()


class ScEvent(db.Model):
    __tablename__ = "sc_event"

    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.Integer, db.ForeignKey("sc_match.id"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("sc_team.id"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("sc_player.id"))

    clock = db.Column(db.String(8))
    kind = db.Column(db.String(24), nullable=False)
    points = db.Column(db.Integer, default=0)
    note = db.Column(db.String(120))

    # Undo hides an event instead of deleting it, so a mis-tap during a match
    # never destroys the record of what actually happened.
    voided = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    player = db.relationship("ScPlayer")
    team = db.relationship("ScTeam")
