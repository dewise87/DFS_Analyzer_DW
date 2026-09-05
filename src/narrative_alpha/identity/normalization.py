"""Deterministic player-name and team-code normalization shared by all identity sources."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})
_WHITESPACE = re.compile(r"\s+")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9 ]+")

CANONICAL_TEAM_CODES: Final[frozenset[str]] = frozenset(
    {
        "ARI",
        "ATL",
        "BAL",
        "BUF",
        "CAR",
        "CHI",
        "CIN",
        "CLE",
        "DAL",
        "DEN",
        "DET",
        "GB",
        "HOU",
        "IND",
        "JAX",
        "KC",
        "LAC",
        "LAR",
        "LV",
        "MIA",
        "MIN",
        "NE",
        "NO",
        "NYG",
        "NYJ",
        "PHI",
        "PIT",
        "SEA",
        "SF",
        "TB",
        "TEN",
        "WAS",
    }
)
"""One canonical abbreviation per current NFL franchise (32 codes)."""

TEAM_CODE_ALIASES: Final[dict[str, str]] = {
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "GNB": "GB",
    "HST": "HOU",
    "JAC": "JAX",
    "KAN": "KC",
    "LA": "LAR",
    "LVR": "LV",
    "NOR": "NO",
    "NWE": "NE",
    "OAK": "LV",
    "SD": "LAC",
    "SDG": "LAC",
    "SFO": "SF",
    "STL": "LAR",
    "TAM": "TB",
    "WSH": "WAS",
}
"""Known vendor and historical variants mapped onto the canonical codes."""


def normalize_name(value: str) -> str:
    """Normalize punctuation without guessing nicknames or rearranging tokens."""

    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", errors="ignore")
        .decode("ascii")
        .casefold()
    )
    without_apostrophes = ascii_value.replace("'", "")
    without_initial_periods = without_apostrophes.replace(".", "")
    with_spaces = without_initial_periods.replace("-", " ")
    cleaned = _NON_ALPHANUMERIC.sub(" ", with_spaces)
    return _WHITESPACE.sub(" ", cleaned).strip()


def name_without_suffix(value: str) -> str:
    """Return a normalized name with one conventional terminal suffix removed."""

    tokens = normalize_name(value).split()
    if tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def has_name_suffix(value: str) -> bool:
    """Report whether a name carries a conventional terminal suffix."""

    tokens = normalize_name(value).split()
    return bool(tokens) and tokens[-1] in _SUFFIXES


def normalize_team_code(value: str) -> str:
    """Map any known team-code variant onto its canonical franchise code."""

    code = value.strip().upper()
    return TEAM_CODE_ALIASES.get(code, code)


def team_code_variants(value: str) -> tuple[str, ...]:
    """Return every known spelling of a team code, canonical form first."""

    canonical = normalize_team_code(value)
    variants = sorted(alias for alias, target in TEAM_CODE_ALIASES.items() if target == canonical)
    return (canonical, *variants)


TEAM_CODES_BY_NAME = {
    "arizona cardinals": "ARI",
    "atlanta falcons": "ATL",
    "baltimore ravens": "BAL",
    "buffalo bills": "BUF",
    "carolina panthers": "CAR",
    "chicago bears": "CHI",
    "cincinnati bengals": "CIN",
    "cleveland browns": "CLE",
    "dallas cowboys": "DAL",
    "denver broncos": "DEN",
    "detroit lions": "DET",
    "green bay packers": "GB",
    "houston texans": "HOU",
    "indianapolis colts": "IND",
    "jacksonville jaguars": "JAX",
    "kansas city chiefs": "KC",
    "las vegas raiders": "LV",
    "los angeles chargers": "LAC",
    "los angeles rams": "LAR",
    "miami dolphins": "MIA",
    "minnesota vikings": "MIN",
    "new england patriots": "NE",
    "new orleans saints": "NO",
    "new york giants": "NYG",
    "new york jets": "NYJ",
    "philadelphia eagles": "PHI",
    "pittsburgh steelers": "PIT",
    "san francisco 49ers": "SF",
    "seattle seahawks": "SEA",
    "tampa bay buccaneers": "TB",
    "tennessee titans": "TEN",
    "washington commanders": "WAS",
}


def team_code_from_name(value: str) -> str | None:
    """Resolve only a maintained NFL full name; never infer a franchise."""

    return TEAM_CODES_BY_NAME.get(" ".join(value.casefold().split()))
