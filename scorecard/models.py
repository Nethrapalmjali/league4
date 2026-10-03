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

    @property
    def sport_icon(self):
        return {
            "Football": "⚽",
            "Cricket": "🏏",
            "Badminton": "🏸",
            "Volleyball": "🏐",
            "Basketball": "🏀",
            "Kabaddi": "🤼",
            "Table Tennis": "🏓",
            "Tennis": "🎾",
        }.get(self.sport, "⚽")

    def scorers_for_side(self, side):
        """List of grouped scoring contributions for side 'a' or 'b'.
        Groups multiple goals/points by the same player:
        e.g. Chetan 1', Chetan 2' -> [{'name': 'Chetan', 'clock': "1', 2'", 'points': 2}]
        For Cricket:
        Displays official player runs (e.g. '32 runs') and provides batting statistics.
        """
        import re

        target_team_id = self.team_a_id if side == "a" else self.team_b_id
        scored = [
            e for e in self.events
            if not e.voided and (e.points or 0) > 0 and e.team_id == target_team_id
        ]
        scored.sort(key=lambda x: x.id)

        grouped = {}
        order = []
        for e in scored:
            who = e.player.name if e.player else ("Goal" if self.sport == "Football" else "Point")
            if who not in grouped:
                grouped[who] = {
                    "name": who,
                    "jersey_no": e.player.jersey_no if e.player else None,
                    "clocks": [],
                    "points": 0,
                    "balls": 0,
                    "fours": 0,
                    "sixes": 0,
                    "kind": e.kind,
                    "icon": self.sport_icon,
                }
                order.append(who)

            pts = e.points or 1
            grouped[who]["points"] += pts
            grouped[who]["balls"] += 1
            if pts == 4:
                grouped[who]["fours"] += 1
            elif pts == 6:
                grouped[who]["sixes"] += 1

            if e.clock:
                clk = e.clock.strip()
                if self.sport == "Football" and clk.isdigit():
                    clk = f"{clk}'"
                grouped[who]["clocks"].append(clk)

        res = []
        for who in order:
            data = grouped[who]
            if self.sport == "Cricket":
                clocks_text = f"{data['points']} runs"
            else:
                clocks = data["clocks"]
                if clocks:
                    clocks_text = ", ".join(clocks)
                elif data["points"] > 1:
                    clocks_text = f"×{data['points']}"
                else:
                    clocks_text = ""

            sr = round((data["points"] / data["balls"]) * 100, 1) if data["balls"] > 0 else 0.0

            res.append({
                "name": data["name"],
                "jersey_no": data["jersey_no"],
                "clock": clocks_text,
                "clocks_text": clocks_text,
                "points": data["points"],
                "balls": data["balls"],
                "fours": data["fours"],
                "sixes": data["sixes"],
                "strike_rate": sr,
                "kind": data["kind"],
                "icon": data["icon"],
            })
        return res

    @property
    def cricket_stats(self):
        """Official Google/Cricinfo style statistics breakdown for Cricket matches."""
        if self.sport != "Cricket":
            return None

        import re

        def summarize(side):
            team = self.team_a if side == "a" else self.team_b
            target_team_id = self.team_a_id if side == "a" else self.team_b_id
            events = [
                e for e in self.events
                if not e.voided and e.team_id == target_team_id
            ]
            events.sort(key=lambda x: x.id)

            wickets = sum(1 for e in events if e.kind == "Wicket")
            total_runs = sum(e.points or 0 for e in events)

            latest_ov = ""
            for e in reversed(events):
                if e.clock:
                    clk = e.clock.strip()
                    m = re.search(r'(\d+(?:\.\d+)?)', clk)
                    if m:
                        latest_ov = f"{m.group(1)} ov"
                        break
                    elif clk:
                        latest_ov = clk
                        break

            batters = self.scorers_for_side(side)
            is_batting_now = (self.status == "live" and (
                (self.period in ("Innings 1", "H1", "P1") and side == "a") or
                (self.period in ("Innings 2", "H2", "P2") and side == "b")
            ))

            for b in batters:
                b["is_not_out"] = is_batting_now

            return {
                "team": team,
                "runs": total_runs,
                "wickets": wickets,
                "overs": latest_ov or "0.0 ov",
                "batters": batters,
                "is_batting_now": is_batting_now,
                "display": f"{total_runs}/{wickets}" if wickets > 0 else f"{total_runs}",
            }

        inn1 = summarize("a")
        inn2 = summarize("b")

        equation = ""
        target = inn1["runs"] + 1

        if self.period == "Innings 2" or inn2["runs"] > 0 or self.status == "final":
            needed = target - inn2["runs"]
            if self.status == "final":
                if inn2["runs"] >= target:
                    equation = f"{self.team_b.name} won by {10 - inn2['wickets']} wickets"
                elif inn1["runs"] > inn2["runs"]:
                    equation = f"{self.team_a.name} won by {inn1['runs'] - inn2['runs']} runs"
                else:
                    equation = "Match tied"
            else:
                if needed > 0:
                    equation = f"{self.team_b.name} need {needed} runs to win"
                elif needed == 0:
                    equation = "Scores level"
                else:
                    equation = f"{self.team_b.name} won by {10 - inn2['wickets']} wickets"

        return {
            "inn1": inn1,
            "inn2": inn2,
            "target": target,
            "equation": equation,
        }

    @property
    def scorers_a(self):
        return self.scorers_for_side("a")

    @property
    def scorers_b(self):
        return self.scorers_for_side("b")

    @property
    def scorers_summary(self):
        """Compact summary of scorers (e.g. 'Chetan 1\', 2\', Ravi 32\'')."""
        all_scorers = self.scorers_for_side("a") + self.scorers_for_side("b")
        if not all_scorers:
            return ""
        parts = []
        for s in all_scorers:
            clk = f" {s['clock']}" if s.get("clock") else ""
            parts.append(f"{s['name']}{clk}")
        return ", ".join(parts)

    @property
    def player_of_the_match(self):
        """Returns the top performer of the match based on scoring points."""
        player_points = {}
        for e in self.events:
            if e.voided or (e.points or 0) <= 0 or not e.player:
                continue
            if e.player_id not in player_points:
                player_points[e.player_id] = {
                    "player": e.player,
                    "team": e.team or (self.team_a if e.team_id == self.team_a_id else self.team_b),
                    "points": 0,
                }
            player_points[e.player_id]["points"] += e.points or 0

        if not player_points:
            return None

        ranked = sorted(player_points.values(), key=lambda x: -x["points"])
        # Prefer winning team's top scorer if match is completed with a winner
        if self.winner_team_id:
            winner_players = [p for p in ranked if p["team"] and p["team"].id == self.winner_team_id]
            best = winner_players[0] if winner_players else ranked[0]
        else:
            best = ranked[0]

        metric = "Goals" if self.sport == "Football" else ("Raid Points" if self.sport == "Kabaddi" else "Points")
        if best["points"] == 1 and metric.endswith("s"):
            metric = metric[:-1]
        best["metric_label"] = metric
        return best


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
