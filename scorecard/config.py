"""Per-sport scoring rules.

One dictionary drives every sport. The database schema and the scoring console
are identical throughout; only the period labels, the buttons and the points
each button carries change. Adding Cricket or Volleyball later is an entry
here, not new code.

``points`` is what the action adds to that team's score. Cards and
substitutions are recorded as events but score nothing, so they carry 0.
"""

SPORT_RULES = {
    "Football": {
        "periods": ["H1", "H2"],
        "actions": [
            {"key": "goal", "label": "Goal", "points": 1},
            {"key": "penalty", "label": "Penalty Goal", "points": 1},
            {"key": "yellow", "label": "Yellow card", "points": 0},
            {"key": "red", "label": "Red card", "points": 0},
            {"key": "sub", "label": "Substitution", "points": 0},
        ],
    },
    "Cricket": {
        "periods": ["Innings 1", "Innings 2"],
        "actions": [
            {"key": "dot", "label": "Dot Ball", "points": 0},
            {"key": "run1", "label": "1 Run", "points": 1},
            {"key": "run2", "label": "2 Runs", "points": 2},
            {"key": "run3", "label": "3 Runs", "points": 3},
            {"key": "run4", "label": "4 Runs", "points": 4},
            {"key": "run6", "label": "6 Runs", "points": 6},
            {"key": "wicket", "label": "Wicket", "points": 0},
            {"key": "wide", "label": "Wide", "points": 1},
            {"key": "noball", "label": "No Ball", "points": 1},
            {"key": "bye", "label": "Bye / Leg-bye", "points": 1},
        ],
    },
    "Kabaddi": {
        "periods": ["H1", "H2"],
        "actions": [
            {"key": "touch", "label": "Touch Point", "points": 1},
            {"key": "bonus", "label": "Bonus Point", "points": 1},
            {"key": "touch_bonus", "label": "Touch + Bonus", "points": 2},
            {"key": "super_raid", "label": "Super Raid", "points": 3},
            {"key": "tackle", "label": "Tackle Point", "points": 1},
            {"key": "super_tackle", "label": "Super Tackle", "points": 2},
            {"key": "allout", "label": "All Out", "points": 2},
            {"key": "empty", "label": "Empty Raid", "points": 0},
        ],
    },
    "Badminton": {
        "periods": ["Game 1", "Game 2", "Game 3"],
        "actions": [
            {"key": "point", "label": "Point", "points": 1},
        ],
    },
}

# Anything not listed above still works — it just gets a plain one-point button
# and a single period, which is enough to keep a score.
DEFAULT_RULES = {
    "periods": ["P1", "P2"],
    "actions": [{"key": "point", "label": "Point", "points": 1}],
}

# League points. Three-one-nil is what everyone reads without being told; the
# sports here that cannot draw simply never award the middle one.
POINTS_WIN = 3
POINTS_DRAW = 1
POINTS_LOSS = 0

STAGES = [
    "League",
    "Quarter Final",
    "Semi Final",
    "Final",
    "Friendly",
]

STATUS_SCHEDULED = "scheduled"
STATUS_LIVE = "live"
STATUS_FINAL = "final"
STATUSES = [STATUS_SCHEDULED, STATUS_LIVE, STATUS_FINAL]


def rules_for(sport):
    """Scoring rules for a sport, falling back to a generic one-point setup."""
    return SPORT_RULES.get(sport, DEFAULT_RULES)


def action_for(sport, key):
    """Look up a single action, or None if the key is not valid for the sport."""
    for action in rules_for(sport)["actions"]:
        if action["key"] == key:
            return action
    return None
