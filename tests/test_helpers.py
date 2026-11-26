"""Tests for the pure helper functions in app.py.

Importing ``app`` builds the Flask app, so a throwaway SQLite database and
dummy credentials are configured before the import.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as league  # noqa: E402


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "₹0"),
        (999, "₹999"),
        (1000, "₹1,000"),
        (100000, "₹1,00,000"),
        (1000000, "₹10,00,000"),
        (-250000, "₹-2,50,000"),
        (1234.6, "₹1,235"),
        (None, "₹0"),
        ("abc", "₹0"),
    ],
)
def test_format_inr(value, expected):
    assert league.format_inr(value) == expected


def test_format_inr_without_prefix():
    assert league.format_inr(1500000, prefix="") == "15,00,000"
    assert league.format_inr(None, prefix="") == "0"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+91 98765-43210", "9876543210"),
        ("919876543210", "9876543210"),
        ("09876543210", "9876543210"),
        ("9876543210", "9876543210"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_phone(raw, expected):
    assert league.normalise_phone(raw) == expected


def test_clean_strips_and_truncates():
    assert league.clean("  hello  ") == "hello"
    assert league.clean(None) == ""
    assert league.clean("abcdef", limit=3) == "abc"


@pytest.mark.parametrize(
    "name, expected",
    [
        (None, "other"),
        ("", "other"),
        ("GM University", "gmu"),
        ("GMU", "gmu"),
        ("GMIT", "gmit"),
        ("GM Institute of Technology", "gmit"),
        ("Some Other College", "other"),
    ],
)
def test_classify_college(name, expected):
    assert league.classify_college(name) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Football", ("Football", "Men")),
        ("football - women", ("Football", "Women")),
        ("Football(Female)", ("Football", "Women")),
        ("Football – Men", ("Football", "Men")),
        ("Curling", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_sport_gender(value, expected):
    assert league.parse_sport_gender(value) == expected


def test_normalise_database_url_rewrites_legacy_postgres_scheme():
    url = league.normalise_database_url("postgres://u:p@host:5432/db")
    assert url.startswith("postgresql+psycopg2://")
    assert "sslmode=require" in url


def test_normalise_database_url_keeps_existing_sslmode():
    url = league.normalise_database_url("postgresql://u:p@host/db?sslmode=disable")
    assert url.count("sslmode=") == 1
    assert "sslmode=disable" in url


def test_normalise_database_url_empty():
    assert league.normalise_database_url("") == ""
    assert league.normalise_database_url(None) is None
