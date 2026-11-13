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
    "Basketball": {
        "periods": ["Q1", "Q2", "Q3", "Q4", "OT"],
        "actions": [
            {"key": "ft", "label": "Free Throw", "points": 1},
            {"key": "fg2", "label": "2-Pointer", "points": 2},
            {"key": "fg3", "label": "3-Pointer", "points": 3},
            {"key": "foul", "label": "Personal Foul", "points": 0},
            {"key": "tech_foul", "label": "Technical Foul", "points": 0},
            # "team": no single player does this, so the app does not ask who.
            {"key": "timeout", "label": "Timeout", "points": 0, "team": True},
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
            {"key": "allout", "label": "All Out", "points": 2, "team": True},
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

# Player statistics, per sport. Each category adds up events by their label:
# "points" sums what they scored, "count" counts how often they happened.
# "Result entry" is what the committee's Enter-result form records when it
# credits a player, so it counts towards the main scoring category.
# The first category is the sport's headline stat (and the tie-breaker for
# Player of the Tournament).
PLAYER_STATS = {
    "Football": [
        {"key": "goals", "label": "Goals", "kinds": ["Goal", "Penalty Goal", "Result entry"], "measure": "points"},
        {"key": "penalties", "label": "Penalty goals", "kinds": ["Penalty Goal"], "measure": "count"},
        {"key": "yellow", "label": "Yellow cards", "kinds": ["Yellow card"], "measure": "count"},
        {"key": "red", "label": "Red cards", "kinds": ["Red card"], "measure": "count"},
    ],
    "Basketball": [
        {"key": "points", "label": "Points", "kinds": ["Free Throw", "2-Pointer", "3-Pointer", "Result entry"], "measure": "points"},
        {"key": "threes", "label": "3-pointers", "kinds": ["3-Pointer"], "measure": "count"},
        {"key": "twos", "label": "2-pointers", "kinds": ["2-Pointer"], "measure": "count"},
        {"key": "ft", "label": "Free throws", "kinds": ["Free Throw"], "measure": "count"},
        {"key": "fouls", "label": "Fouls", "kinds": ["Personal Foul", "Technical Foul"], "measure": "count"},
    ],
    "Kabaddi": [
        {"key": "total", "label": "Total points",
         "kinds": ["Touch Point", "Bonus Point", "Touch + Bonus", "Super Raid", "Tackle Point", "Super Tackle", "Result entry"],
         "measure": "points"},
        {"key": "raid", "label": "Raid points", "kinds": ["Touch Point", "Bonus Point", "Touch + Bonus", "Super Raid"], "measure": "points"},
        {"key": "tackle", "label": "Tackle points", "kinds": ["Tackle Point", "Super Tackle"], "measure": "points"},
        {"key": "super_raids", "label": "Super raids", "kinds": ["Super Raid"], "measure": "count"},
        {"key": "super_tackles", "label": "Super tackles", "kinds": ["Super Tackle"], "measure": "count"},
    ],
    "Badminton": [
        {"key": "points", "label": "Points won", "kinds": ["Point", "Result entry"], "measure": "points"},
    ],
}
DEFAULT_PLAYER_STATS = [
    {"key": "points", "label": "Points", "kinds": ["Point", "Result entry"], "measure": "points"},
]


def player_stats_for(sport):
    return PLAYER_STATS.get(sport, DEFAULT_PLAYER_STATS)


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
GENDERS = ["Men", "Women"]

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
