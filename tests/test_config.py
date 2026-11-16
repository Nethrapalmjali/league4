"""Tests for the pure scoring rules in scorecard/config.py.

config.py has no dependencies, so it is loaded straight from its file. That
avoids importing the ``scorecard`` package (and with it the whole Flask app,
database and environment) just to check the rules.
"""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scorecard" / "config.py"
_spec = importlib.util.spec_from_file_location("scorecard_config", _PATH)
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)


@pytest.mark.parametrize("sport", sorted(config.SPORT_RULES))
def test_every_sport_has_periods_and_unique_action_keys(sport):
    rules = config.rules_for(sport)
    assert rules["periods"]
    keys = [a["key"] for a in rules["actions"]]
    assert len(keys) == len(set(keys))
    assert all(a["points"] >= 0 for a in rules["actions"])


def test_unknown_sport_falls_back_to_one_point_default():
    rules = config.rules_for("Curling")
    assert rules is config.DEFAULT_RULES
    assert config.action_for("Curling", "point")["points"] == 1


def test_action_for_known_and_unknown_keys():
    assert config.action_for("Basketball", "fg3")["points"] == 3
    assert config.action_for("Football", "nope") is None


def test_cards_and_substitutions_score_nothing():
    for key in ("yellow", "red", "sub"):
        assert config.action_for("Football", key)["points"] == 0


def test_league_points_ordering():
    assert config.POINTS_WIN > config.POINTS_DRAW >= config.POINTS_WALKOVER
    assert config.POINTS_WALKOVER > config.POINTS_LOSS


def test_player_stats_fall_back_to_default_for_unknown_sport():
    assert config.player_stats_for("Curling") is config.DEFAULT_PLAYER_STATS
    assert config.player_stats_for("Football")[0]["key"] == "goals"


def test_verified_stats_sports_are_known_sports():
    assert set(config.VERIFIED_STATS_SPORTS) <= set(config.PLAYER_STATS)


def test_badminton_format_fills_in_gender_labels():
    men = config.badminton_format("bo5", "Men")
    women = config.badminton_format("bo5", "Women")
    assert men["rounds"][0]["label"] == "Men's Singles 1"
    assert women["rounds"][0]["label"] == "Women's Singles 1"
    assert [r["number"] for r in men["rounds"]] == [1, 2, 3, 4, 5]


def test_badminton_format_does_not_mutate_shared_definition():
    config.badminton_format("bo5", "Women")
    assert config.BADMINTON_FORMATS["bo5"]["rounds"][0]["label"] == "{g} Singles 1"


def test_badminton_unknown_format_uses_legacy_best_of_three():
    assert config.badminton_format(None)["key"] == "bo3"
    assert config.badminton_format("bo5")["need"] == 3
