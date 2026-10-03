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
        """Re-derive scores from active events.
        For Badminton: score_a and score_b represent Games won (or current game rally points if Game 1 in progress).
        For Football, Cricket, Kabaddi: sum of event points.
        """
        if self.sport == "Badminton":
            bs = self.badminton_stats
            if bs:
                if bs["games_a"] > 0 or bs["games_b"] > 0 or self.status == "final":
                    self.score_a = bs["games_a"]
                    self.score_b = bs["games_b"]
                else:
                    self.score_a = bs["active_pts_a"]
                    self.score_b = bs["active_pts_b"]
                self.updated_at = datetime.utcnow()
                return

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
            "Basketball": "🏀",
            "Cricket": "🏏",
            "Kabaddi": "🤼",
            "Badminton": "🏸",
        }.get(self.sport, "🏆")

    def scorers_for_side(self, side):
        """List of grouped scoring contributions for side 'a' or 'b'."""
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
                    "threes": 0,
                    "twos": 0,
                    "fts": 0,
                    "kind": e.kind,
                    "icon": self.sport_icon,
                    "raids": 0,
                    "tackles": 0,
                }
                order.append(who)

            pts = e.points or 1
            grouped[who]["points"] += pts
            grouped[who]["balls"] += 1
            if pts == 4:
                grouped[who]["fours"] += 1
            elif pts == 6:
                grouped[who]["sixes"] += 1
            elif pts == 3 or "3-Pointer" in (e.kind or ""):
                grouped[who]["threes"] += 1
            elif pts == 2 or "2-Pointer" in (e.kind or ""):
                grouped[who]["twos"] += 1
            elif pts == 1 and ("Free Throw" in (e.kind or "") or "FT" in (e.kind or "")):
                grouped[who]["fts"] += 1

            if any(k in e.kind for k in ["Touch", "Bonus", "Raid"]):
                grouped[who]["raids"] += pts
            elif "Tackle" in e.kind:
                grouped[who]["tackles"] += pts

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
            elif self.sport == "Basketball":
                pts_parts = []
                if data["threes"] > 0:
                    pts_parts.append(f"{data['threes']}x 3PT")
                if data["fts"] > 0:
                    pts_parts.append(f"{data['fts']} FT")
                breakdown = f" ({', '.join(pts_parts)})" if pts_parts else ""
                clocks_text = f"{data['points']} pts{breakdown}"
            elif self.sport == "Kabaddi":
                pts_parts = []
                if data["raids"] > 0:
                    pts_parts.append(f"{data['raids']}R")
                if data["tackles"] > 0:
                    pts_parts.append(f"{data['tackles']}T")
                breakdown = f" ({', '.join(pts_parts)})" if pts_parts else ""
                clocks_text = f"{data['points']} pts{breakdown}"
            elif self.sport == "Badminton":
                clocks_text = f"{data['points']} pts"
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
                "threes": data["threes"],
                "twos": data["twos"],
                "fts": data["fts"],
                "strike_rate": sr,
                "kind": data["kind"],
                "icon": data["icon"],
            })
        return res

    @property
    def basketball_stats(self):
        """Official NBA/FIBA style Quarter-by-Quarter and Team Box Stats."""
        if self.sport != "Basketball":
            return None

        quarters = ["Q1", "Q2", "Q3", "Q4"]
        has_ot = any("OT" in (e.clock or "") or self.period == "OT" for e in self.events if not e.voided)
        if has_ot:
            quarters.append("OT")

        q_scores = {q: {"a": 0, "b": 0} for q in quarters}
        team_stats = {
            "a": {"total": 0, "fg2": 0, "fg3": 0, "ft": 0, "fouls": 0, "timeouts": 0},
            "b": {"total": 0, "fg2": 0, "fg3": 0, "ft": 0, "fouls": 0, "timeouts": 0},
        }

        players_a = {}
        players_b = {}

        for e in sorted(self.events, key=lambda x: x.id):
            if e.voided:
                continue
            side = self.side_of(e.team_id)
            if not side:
                continue

            q = None
            if e.clock:
                clk_u = e.clock.upper()
                for cand in ["OT", "Q4", "Q3", "Q2", "Q1"]:
                    if cand in clk_u:
                        q = cand
                        break
            if not q:
                if self.period in q_scores:
                    q = self.period
                else:
                    q = "Q1"

            if q not in q_scores:
                q_scores[q] = {"a": 0, "b": 0}
                if q not in quarters:
                    quarters.append(q)

            pts = e.points or 0
            q_scores[q][side] += pts
            team_stats[side]["total"] += pts

            kind_str = e.kind or ""
            if pts == 3 or "3-Pointer" in kind_str or "3pt" in kind_str.lower():
                team_stats[side]["fg3"] += 1
            elif pts == 2 or "2-Pointer" in kind_str:
                team_stats[side]["fg2"] += 1
            elif pts == 1 or "Free Throw" in kind_str:
                team_stats[side]["ft"] += 1

            if "Foul" in kind_str:
                team_stats[side]["fouls"] += 1
            elif "Timeout" in kind_str:
                team_stats[side]["timeouts"] += 1

            if e.player:
                p_dict = players_a if side == "a" else players_b
                p_name = e.player.name
                if p_name not in p_dict:
                    p_dict[p_name] = {
                        "name": p_name,
                        "jersey_no": e.player.jersey_no,
                        "points": 0,
                        "fg2": 0,
                        "fg3": 0,
                        "ft": 0,
                        "fouls": 0,
                    }
                p_dict[p_name]["points"] += pts
                if pts == 3 or "3-Pointer" in kind_str:
                    p_dict[p_name]["fg3"] += 1
                elif pts == 2 or "2-Pointer" in kind_str:
                    p_dict[p_name]["fg2"] += 1
                elif pts == 1 or "Free Throw" in kind_str:
                    p_dict[p_name]["ft"] += 1
                if "Foul" in kind_str:
                    p_dict[p_name]["fouls"] += 1

        q_rows = []
        for q in quarters:
            q_rows.append({
                "quarter": q,
                "a": q_scores[q]["a"],
                "b": q_scores[q]["b"],
            })

        diff = team_stats["a"]["total"] - team_stats["b"]["total"]
        if self.status == "final":
            if diff > 0:
                lead_text = f"{self.team_a.name} won by {diff} pts"
            elif diff < 0:
                lead_text = f"{self.team_b.name} won by {-diff} pts"
            else:
                lead_text = "Match Tied"
        else:
            if diff > 0:
                lead_text = f"{self.team_a.name} leads by {diff} pts"
            elif diff < 0:
                lead_text = f"{self.team_b.name} leads by {-diff} pts"
            else:
                lead_text = "Scores Level"

        top_scorers_a = sorted(players_a.values(), key=lambda x: x["points"], reverse=True)
        top_scorers_b = sorted(players_b.values(), key=lambda x: x["points"], reverse=True)

        return {
            "quarters": q_rows,
            "lead_text": lead_text,
            "a": team_stats["a"],
            "b": team_stats["b"],
            "players_a": top_scorers_a,
            "players_b": top_scorers_b,
        }

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

            # Count legal deliveries
            legal_balls = sum(
                1 for e in events
                if e.kind not in ("Wide", "No Ball")
            )
            overs_calc = f"{legal_balls // 6}.{legal_balls % 6} ov"
            next_ball_calc = f"{legal_balls // 6}.{(legal_balls % 6) + 1} ov"

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
                "team": team.name if team else "",
                "team_tag": team.tag if team else "",
                "runs": total_runs,
                "wickets": wickets,
                "legal_balls": legal_balls,
                "overs": latest_ov or overs_calc,
                "overs_calc": overs_calc,
                "next_ball": next_ball_calc,
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
    def badminton_stats(self):
        """Official Badminton BWF-style match and game tracker."""
        if self.sport != "Badminton":
            return None

        games_data = {
            "Game 1": {"a": 0, "b": 0},
            "Game 2": {"a": 0, "b": 0},
            "Game 3": {"a": 0, "b": 0},
        }

        current_g = "Game 1"
        for e in sorted(self.events, key=lambda x: x.id):
            if e.voided or (e.points or 0) <= 0:
                continue
            side = self.side_of(e.team_id)
            if not side:
                continue

            g_key = None
            if e.clock:
                for k in ("Game 1", "Game 2", "Game 3", "G1", "G2", "G3"):
                    if k.lower() in e.clock.lower():
                        g_key = "Game 1" if "1" in k else ("Game 2" if "2" in k else "Game 3")
                        break
            if not g_key:
                g_key = current_g

            games_data[g_key][side] += e.points or 1

            pts_a = games_data[g_key]["a"]
            pts_b = games_data[g_key]["b"]
            won_by_a = (pts_a >= 21 and pts_a - pts_b >= 2) or (pts_a == 30)
            won_by_b = (pts_b >= 21 and pts_b - pts_a >= 2) or (pts_b == 30)
            if won_by_a or won_by_b:
                if g_key == "Game 1" and current_g == "Game 1":
                    current_g = "Game 2"
                elif g_key == "Game 2" and current_g == "Game 2":
                    current_g = "Game 3"

        games_list = []
        games_won_a = 0
        games_won_b = 0
        active_game = "Game 1"
        active_pts_a = 0
        active_pts_b = 0

        for g_name in ["Game 1", "Game 2", "Game 3"]:
            a = games_data[g_name]["a"]
            b = games_data[g_name]["b"]
            is_won_a = (a >= 21 and a - b >= 2) or (a == 30)
            is_won_b = (b >= 21 and b - a >= 2) or (b == 30)
            is_done = is_won_a or is_won_b
            winner = "a" if is_won_a else ("b" if is_won_b else None)

            if is_done:
                if winner == "a":
                    games_won_a += 1
                else:
                    games_won_b += 1
            elif not games_list or all(x["is_done"] for x in games_list):
                active_game = g_name
                active_pts_a = a
                active_pts_b = b

            games_list.append({
                "game": g_name,
                "a": a,
                "b": b,
                "score_text": f"{a}–{b}" if (a > 0 or b > 0 or is_done) else "—",
                "is_done": is_done,
                "winner": winner,
            })

        match_winner = None
        if games_won_a >= 2:
            match_winner = "a"
        elif games_won_b >= 2:
            match_winner = "b"

        game_point_a = False
        game_point_b = False
        if not match_winner:
            if active_pts_a >= 20 and active_pts_a > active_pts_b:
                game_point_a = True
            elif active_pts_b >= 20 and active_pts_b > active_pts_a:
                game_point_b = True

        completed_scores = [f"{g['a']}–{g['b']}" for g in games_list if g["is_done"] or (g["a"] > 0 or g["b"] > 0)]
        sets_summary = ", ".join(completed_scores) if completed_scores else ""

        return {
            "games_a": games_won_a,
            "games_b": games_won_b,
            "games_list": games_list,
            "active_game": active_game,
            "active_pts_a": active_pts_a,
            "active_pts_b": active_pts_b,
            "match_winner": match_winner,
            "game_point_a": game_point_a,
            "game_point_b": game_point_b,
            "sets_summary": sets_summary,
            "display": f"{games_won_a}–{games_won_b}" if (games_won_a > 0 or games_won_b > 0 or self.status == 'final') else f"{active_pts_a}–{active_pts_b}",
        }

    @property
    def kabaddi_stats(self):
        """Official Pro Kabaddi style raid, tackle, bonus and all-out breakdown."""
        if self.sport != "Kabaddi":
            return None

        def stats_for(side):
            target_team_id = self.team_a_id if side == "a" else self.team_b_id
            events = [e for e in self.events if not e.voided and e.team_id == target_team_id]

            touch = sum(1 for e in events if "Touch" in e.kind and "Bonus" not in e.kind)
            bonus = sum(1 for e in events if "Bonus" in e.kind and "Touch" not in e.kind)
            touch_bonus = sum(1 for e in events if "Touch + Bonus" in e.kind)
            super_raids = sum(1 for e in events if "Super Raid" in e.kind)

            raid_pts = sum(e.points or 0 for e in events if any(k in e.kind for k in ["Touch", "Bonus", "Raid"]))
            tackle_pts = sum(e.points or 0 for e in events if "Tackle" in e.kind)
            allout_pts = sum(e.points or 0 for e in events if "All Out" in e.kind)
            super_tackles = sum(1 for e in events if "Super Tackle" in e.kind)

            return {
                "touch": touch + touch_bonus,
                "bonus": bonus + touch_bonus,
                "super_raids": super_raids,
                "raid_points": raid_pts,
                "tackle_points": tackle_pts,
                "super_tackles": super_tackles,
                "allout_points": allout_pts,
                "total_points": raid_pts + tackle_pts + allout_pts,
            }

        s_a = stats_for("a")
        s_b = stats_for("b")

        lead_text = ""
        diff = (self.score_a or 0) - (self.score_b or 0)
        if diff > 0:
            lead_text = f"{self.team_a.name} lead by {diff} point{'s' if diff > 1 else ''}"
        elif diff < 0:
            lead_text = f"{self.team_b.name} lead by {abs(diff)} point{'s' if abs(diff) > 1 else ''}"
        else:
            lead_text = "Scores level"

        return {
            "a": s_a,
            "b": s_b,
            "lead_text": lead_text,
        }

    @property
    def scorers_a(self):
        return self.scorers_for_side("a")

    @property
    def scorers_b(self):
        return self.scorers_for_side("b")

    @property
    def scorers_summary(self):
        """Compact summary of scorers."""
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
        if self.winner_team_id:
            winner_players = [p for p in ranked if p["team"] and p["team"].id == self.winner_team_id]
            best = winner_players[0] if winner_players else ranked[0]
        else:
            best = ranked[0]

        metric = "Goals" if self.sport == "Football" else (
            "Runs" if self.sport == "Cricket" else (
                "Raid Points" if self.sport == "Kabaddi" else "Points"
            )
        )
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
