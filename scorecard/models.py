"""Scorecard tables.

Every table is prefixed ``sc_`` and none of them declare a foreign key into the
event-management schema. The only link is a pair of nullable integer columns —
``sc_team.ems_owner_id`` and ``sc_player.ems_player_id`` — which stay empty
until someone runs the linking screen. That keeps the two systems independent:
drop the ``sc_`` tables and the EMS is exactly as it was.
"""

from datetime import datetime

import re

from .config import BADMINTON_DEFAULT_FORMAT, STATUS_FINAL, STATUS_SCHEDULED, badminton_format, rules_for
from .ems import db


def badminton_round_won(fmt):
    """won(x, y): does a side on x points beat one on y under this format?"""
    target, win_by, cap = fmt["target"], fmt["win_by"], fmt["cap"]
    return lambda x, y: (x >= target and x - y >= win_by) or x >= cap


def event_order(event):
    """Oldest first. Events not yet saved (no id) come last, in the order
    they were added — the Scorer App saves a whole upload at once."""
    return (event.id is None, event.id or 0)


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
    gender = db.Column(db.String(10), default="Men")
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
    scorer_can_edit_later = db.Column(db.Boolean, default=False)
    # Chosen by the scorer or committee; when empty the top performer is used.
    potm_player_id = db.Column(db.Integer)
    # Badminton only: "bo5" (5 rounds to 15) for every new match. Empty on
    # matches scored before formats existed — read as the old best of 3.
    match_format = db.Column(db.String(12))
    note = db.Column(db.String(200))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Loaded in the same query as the match: every page that shows a match
    # shows both team names, and fetching them one by one cost two extra
    # round trips per match.
    team_a = db.relationship("ScTeam", foreign_keys=[team_a_id], lazy="joined")
    team_b = db.relationship("ScTeam", foreign_keys=[team_b_id], lazy="joined")

    events = db.relationship(
        "ScEvent", backref="match", lazy="select",
        cascade="all, delete-orphan", order_by="ScEvent.id.desc()",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.sport == "Badminton" and not self.match_format:
            self.match_format = BADMINTON_DEFAULT_FORMAT

    @property
    def format_info(self):
        """Badminton format with round names, or None for other sports."""
        if self.sport != "Badminton":
            return None
        return badminton_format(self.match_format or "bo3", self.gender)

    @property
    def periods(self):
        fmt = self.format_info
        if fmt:
            return [r["key"] for r in fmt["rounds"]]
        return rules_for(self.sport)["periods"]

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
                # Best of 5: the scoreboard always shows the round tally (the
                # points of the round in play are shown beside it). The old
                # best of 3 showed rally points until a game was won.
                if bs["format"] == "bo5" or bs["games_a"] > 0 or bs["games_b"] > 0 or self.status == "final":
                    self.score_a = bs["games_a"]
                    self.score_b = bs["games_b"]
                else:
                    self.score_a = bs["active_pts_a"]
                    self.score_b = bs["active_pts_b"]
                # The winner is declared the moment a side has enough rounds;
                # an undo that takes it away clears it again.
                if bs["match_winner"]:
                    self.winner_team_id = self.team_a_id if bs["match_winner"] == "a" else self.team_b_id
                else:
                    if self.status != STATUS_FINAL:
                        self.winner_team_id = None
                    if self.status != STATUS_SCHEDULED:
                        self.period = bs["active_game"]
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
        scored.sort(key=event_order)

        grouped = {}
        order = []
        for e in scored:
            who = e.player.name if e.player else ("Goal" if self.sport == "Football" else "Point")
            if who not in grouped:
                grouped[who] = {
                    "name": who,
                    "player_id": e.player_id,
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
                "player_id": data["player_id"],
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

        for e in sorted(self.events, key=event_order):
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
            events.sort(key=event_order)

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

    def badminton_points_by_round(self):
        """({round key: {"a": pts, "b": pts}}, [(event, round key)]) from the
        active point events, oldest first."""
        fmt = self.format_info
        keys = [r["key"] for r in fmt["rounds"]]
        won = badminton_round_won(fmt)
        pts = {k: {"a": 0, "b": 0} for k in keys}
        placed = []
        current = 0
        for e in sorted(self.events, key=event_order):
            if e.voided or (e.points or 0) <= 0:
                continue
            side = self.side_of(e.team_id)
            if not side:
                continue
            idx = None
            if e.clock:
                found = re.search(r"\b(?:round|game|r|g)\s*(\d+)", e.clock, re.IGNORECASE)
                if found and 1 <= int(found.group(1)) <= len(keys):
                    idx = int(found.group(1)) - 1
            if idx is None:
                idx = current
            pts[keys[idx]][side] += e.points or 1
            placed.append((e, keys[idx]))
            while current < len(keys) - 1 and (
                won(pts[keys[current]]["a"], pts[keys[current]]["b"])
                or won(pts[keys[current]]["b"], pts[keys[current]]["a"])
            ):
                current += 1
        return pts, placed

    @property
    def badminton_stats(self):
        """Badminton round-by-round tracker, worked out from the point events.

        New matches: best of 5 rounds, first to 15 wins a round, 3 rounds win
        the match and the rounds left are "not required". Older matches keep
        the best-of-3-games-to-21 reading they were scored with. Each point
        belongs to the round stamped on it ("Round 2", "R2", "Game 2", "G2");
        an unstamped point goes to the round in play at that moment.
        The same calculation runs in the Scorer App (badmintonStats in app.js).
        """
        fmt = self.format_info
        if fmt is None:
            return None
        rounds = fmt["rounds"]
        target, cap, need = fmt["target"], fmt["cap"], fmt["need"]
        won = badminton_round_won(fmt)
        pts, _ = self.badminton_points_by_round()

        games_list = []
        won_a = won_b = 0
        match_winner = None
        active = None
        for r in rounds:
            a, b = pts[r["key"]]["a"], pts[r["key"]]["b"]
            wa, wb = won(a, b), won(b, a)
            done = (wa or wb) and match_winner is None
            winner = ("a" if wa else "b") if done else None
            if match_winner:
                status = "not_required"
            elif done:
                status = "completed"
                if winner == "a":
                    won_a += 1
                else:
                    won_b += 1
                if won_a >= need:
                    match_winner = "a"
                elif won_b >= need:
                    match_winner = "b"
            elif active is None:
                active = r
                if self.status == STATUS_FINAL:
                    status = "not_played"
                elif self.status == STATUS_SCHEDULED and not (a or b):
                    status = "upcoming"
                else:
                    status = "ongoing"
            else:
                status = "not_played" if self.status == STATUS_FINAL else "upcoming"
            games_list.append({
                "game": r["key"],
                "label": r["label"],
                "number": r["number"],
                "a": a,
                "b": b,
                "score_text": f"{a}–{b}" if (a or b or done) else "—",
                "is_done": done,
                "winner": winner,
                "status": status,
            })

        if active is None:
            # Every round decided (or the match is over): point at the last
            # round played, for anything that shows "the current round".
            played = [g for g in games_list if g["status"] == "completed"]
            active_row = played[-1] if played else games_list[0]
        else:
            active_row = next(g for g in games_list if g["game"] == active["key"])
        active_a = active_row["a"] if active is not None else 0
        active_b = active_row["b"] if active is not None else 0

        game_point_a = bool(active is not None and won(active_a + 1, active_b))
        game_point_b = bool(active is not None and won(active_b + 1, active_a))
        played_scores = [f"{g['a']}–{g['b']}" for g in games_list
                         if g["status"] in ("completed", "ongoing") and (g["a"] or g["b"] or g["is_done"])]
        tally = f"{won_a}–{won_b}"
        if match_winner:
            winner_team = self.team_a if match_winner == "a" else self.team_b
            result_text = f"{winner_team.name if winner_team else 'Winner'} won {max(won_a, won_b)}–{min(won_a, won_b)}"
        else:
            result_text = ""

        return {
            "format": fmt["key"],
            "unit": fmt["unit"],
            "rule_text": fmt["rule_text"],
            "target": target,
            "need": need,
            "games_a": won_a,
            "games_b": won_b,
            "tally": tally,
            "games_list": games_list,
            "active_game": active_row["game"],
            "active_label": active_row["label"],
            "active_pts_a": active_a,
            "active_pts_b": active_b,
            "match_winner": match_winner,
            "result_text": result_text,
            "game_point_a": game_point_a and not match_winner,
            "game_point_b": game_point_b and not match_winner,
            "match_point_a": game_point_a and won_a == need - 1,
            "match_point_b": game_point_b and won_b == need - 1,
            "sets_summary": ", ".join(played_scores),
            "display": tally if (fmt["key"] == "bo5" or won_a or won_b or self.status == STATUS_FINAL) else f"{active_a}–{active_b}",
            # One line for scoreboards: the result, else the round in play.
            "live_text": result_text or (
                f"{active_row['label']}: {active_a}–{active_b}" if active is not None and (active_a or active_b or self.status != STATUS_SCHEDULED)
                else ("Best of 5 rounds · first to 15" if fmt["key"] == "bo5" else "")
            ),
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

        # The scorer's or committee's choice wins over the automatic pick.
        if self.potm_player_id:
            chosen = player_points.get(self.potm_player_id)
            if chosen is None:
                picked = db.session.get(ScPlayer, self.potm_player_id)
                if picked is not None:
                    chosen = {"player": picked, "team": picked.team, "points": 0}
            if chosen is not None:
                best = dict(chosen, chosen=True)
                player_points = None

        if player_points is not None:
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

    # Set when the event was recorded in the offline Scorer App: the id the
    # device gave it. Lets a later "undo" from the same device find it, and
    # makes a re-sent event impossible to store twice.
    client_id = db.Column(db.String(40), unique=True, index=True)

    # Joined for the same reason: scorer lists and feeds name the player and
    # team of every event.
    player = db.relationship("ScPlayer", lazy="joined")
    team = db.relationship("ScTeam", lazy="joined")


class ScSyncOp(db.Model):
    """One row per change the offline Scorer App has pushed.

    The app re-sends anything it has not seen acknowledged, so the same change
    can arrive twice (a dropped reply on a weak signal). The primary key on the
    device-generated id is what turns the second arrival into a no-op.
    """

    __tablename__ = "sc_sync_op"

    op_id = db.Column(db.String(40), primary_key=True)
    match_id = db.Column(db.Integer, index=True)
    kind = db.Column(db.String(12))
    result = db.Column(db.String(12))
    # Who pushed it: "scorer:<id>:<name>", "admin" or "import". Drives the
    # committee's "last upload from the ground" view.
    actor = db.Column(db.String(100), index=True)
    device_at = db.Column(db.DateTime)
    applied_at = db.Column(db.DateTime, default=datetime.utcnow)


class ScErrorLog(db.Model):
    """Server errors, kept so the committee can see what broke and when.

    Written by the site's 500 handler. Errors that happen before the app runs
    at all (the host restarting or asleep) never reach here — those show up in
    the hosting dashboard instead.
    """

    __tablename__ = "sc_error_log"

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    method = db.Column(db.String(8))
    path = db.Column(db.String(200))
    error = db.Column(db.String(300))
    detail = db.Column(db.Text)
