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
            {"key": "yellow", "label": "Yellow card", "points": 0},
            {"key": "red", "label": "Red card", "points": 0},
            {"key": "sub", "label": "Substitution", "points": 0},
        ],
    },
    "Kabaddi": {
        "periods": ["H1", "H2"],
        "actions": [
            {"key": "raid", "label": "Raid", "points": 1},
            {"key": "tackle", "label": "Tackle", "points": 1},
            {"key": "bonus", "label": "Bonus", "points": 1},
            {"key": "allout", "label": "All out", "points": 2},
        ],
    },
    "Basketball": {
        "periods": ["Q1", "Q2", "Q3", "Q4"],
        "actions": [
            {"key": "ft", "label": "Free throw", "points": 1},
            {"key": "fg2", "label": "2 points", "points": 2},
            {"key": "fg3", "label": "3 points", "points": 3},
            {"key": "foul", "label": "Foul", "points": 0},
        ],
    },
    "Badminton": {
        "periods": ["Game 1", "Game 2", "Game 3"],
        "actions": [
            {"key": "point", "label": "Point", "points": 1},
        ],
    },
    "Volleyball": {
        "periods": ["Set 1", "Set 2", "Set 3", "Set 4", "Set 5"],
        "actions": [
            {"key": "point", "label": "Point", "points": 1},
        ],
    },
    "Cricket": {
        "periods": ["Innings 1", "Innings 2"],
        "actions": [
            {"key": "run1", "label": "1 Run", "points": 1},
            {"key": "run4", "label": "4 Runs", "points": 4},
            {"key": "run6", "label": "6 Runs", "points": 6},
            {"key": "wicket", "label": "Wicket", "points": 0},
            {"key": "extra", "label": "Extra", "points": 1},
        ],
    },
    "Table Tennis": {
        "periods": ["Game 1", "Game 2", "Game 3", "Game 4", "Game 5"],
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
