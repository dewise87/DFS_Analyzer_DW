"""Versioned, manually maintained NFL stadium metadata.

Coordinates identify the playing venue, not the associated team's offices.  Update the
version whenever a venue, roof classification, surface, or coordinate changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

STADIUM_TABLE_VERSION = "2026-09-01.1"


class RoofType(StrEnum):
    """Weather exposure classification used by the forecast collector."""

    OUTDOOR = "outdoor"
    RETRACTABLE = "retractable"
    INDOOR = "indoor"


class SurfaceType(StrEnum):
    """Stable surface categories for manual stadium metadata."""

    NATURAL_GRASS = "natural_grass"
    ARTIFICIAL_TURF = "artificial_turf"
    HYBRID_GRASS = "hybrid_grass"


@dataclass(frozen=True, slots=True)
class Stadium:
    """One active NFL venue; shared venues contain both home team codes."""

    name: str
    latitude: float
    longitude: float
    roof: RoofType
    surface: SurfaceType
    home_teams: tuple[str, ...]
    aliases: tuple[str, ...] = ()


# Exactly 30 active venues for 32 teams (LAR/LAC and NYG/NYJ share venues).
STADIUMS: tuple[Stadium, ...] = (
    Stadium(
        "State Farm Stadium",
        33.5276,
        -112.2626,
        RoofType.RETRACTABLE,
        SurfaceType.NATURAL_GRASS,
        ("ARI",),
        ("University of Phoenix Stadium",),
    ),
    Stadium(
        "Mercedes-Benz Stadium",
        33.7554,
        -84.4008,
        RoofType.RETRACTABLE,
        SurfaceType.ARTIFICIAL_TURF,
        ("ATL",),
    ),
    Stadium(
        "M&T Bank Stadium",
        39.2780,
        -76.6227,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("BAL",),
    ),
    Stadium(
        "Highmark Stadium",
        42.7738,
        -78.7868,
        RoofType.OUTDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("BUF",),
        ("New Highmark Stadium", "New Era Field", "Bills Stadium"),
    ),
    Stadium(
        "Bank of America Stadium",
        35.2258,
        -80.8528,
        RoofType.OUTDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("CAR",),
    ),
    Stadium(
        "Soldier Field",
        41.8623,
        -87.6167,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("CHI",),
    ),
    Stadium(
        "Paycor Stadium",
        39.0954,
        -84.5160,
        RoofType.OUTDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("CIN",),
        ("Paul Brown Stadium",),
    ),
    Stadium(
        "Huntington Bank Field",
        41.5061,
        -81.6995,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("CLE",),
        ("Cleveland Browns Stadium", "FirstEnergy Stadium"),
    ),
    Stadium(
        "AT&T Stadium",
        32.7473,
        -97.0945,
        RoofType.RETRACTABLE,
        SurfaceType.ARTIFICIAL_TURF,
        ("DAL",),
        ("Cowboys Stadium",),
    ),
    Stadium(
        "Empower Field at Mile High",
        39.7439,
        -105.0201,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("DEN",),
        ("Mile High Stadium",),
    ),
    Stadium(
        "Ford Field",
        42.3400,
        -83.0456,
        RoofType.INDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("DET",),
    ),
    Stadium(
        "Lambeau Field",
        44.5013,
        -88.0622,
        RoofType.OUTDOOR,
        SurfaceType.HYBRID_GRASS,
        ("GB",),
    ),
    Stadium(
        "NRG Stadium",
        29.6847,
        -95.4107,
        RoofType.RETRACTABLE,
        SurfaceType.ARTIFICIAL_TURF,
        ("HOU",),
        ("Reliant Stadium",),
    ),
    Stadium(
        "Lucas Oil Stadium",
        39.7601,
        -86.1639,
        RoofType.RETRACTABLE,
        SurfaceType.ARTIFICIAL_TURF,
        ("IND",),
    ),
    Stadium(
        "EverBank Stadium",
        30.3239,
        -81.6373,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("JAX",),
        ("TIAA Bank Field",),
    ),
    Stadium(
        "GEHA Field at Arrowhead Stadium",
        39.0489,
        -94.4839,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("KC",),
        ("Arrowhead Stadium",),
    ),
    Stadium(
        "Allegiant Stadium",
        36.0909,
        -115.1833,
        RoofType.INDOOR,
        SurfaceType.NATURAL_GRASS,
        ("LV",),
    ),
    Stadium(
        "SoFi Stadium",
        33.9535,
        -118.3392,
        RoofType.INDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("LAC", "LAR"),
    ),
    Stadium(
        "Hard Rock Stadium",
        25.9580,
        -80.2389,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("MIA",),
        ("Sun Life Stadium",),
    ),
    Stadium(
        "U.S. Bank Stadium",
        44.9736,
        -93.2575,
        RoofType.INDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("MIN",),
    ),
    Stadium(
        "Gillette Stadium",
        42.0909,
        -71.2643,
        RoofType.OUTDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("NE",),
    ),
    Stadium(
        "Caesars Superdome",
        29.9511,
        -90.0812,
        RoofType.INDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("NO",),
        ("Mercedes-Benz Superdome",),
    ),
    Stadium(
        "MetLife Stadium",
        40.8135,
        -74.0745,
        RoofType.OUTDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("NYG", "NYJ"),
    ),
    Stadium(
        "Lincoln Financial Field",
        39.9008,
        -75.1675,
        RoofType.OUTDOOR,
        SurfaceType.HYBRID_GRASS,
        ("PHI",),
    ),
    Stadium(
        "Acrisure Stadium",
        40.4468,
        -80.0158,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("PIT",),
        ("Heinz Field",),
    ),
    Stadium(
        "Levi's Stadium",
        37.4030,
        -121.9700,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("SF",),
    ),
    Stadium(
        "Lumen Field",
        47.5952,
        -122.3316,
        RoofType.OUTDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("SEA",),
        ("CenturyLink Field",),
    ),
    Stadium(
        "Raymond James Stadium",
        27.9759,
        -82.5033,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("TB",),
    ),
    Stadium(
        "Nissan Stadium",
        36.1665,
        -86.7713,
        RoofType.OUTDOOR,
        SurfaceType.ARTIFICIAL_TURF,
        ("TEN",),
    ),
    Stadium(
        "Northwest Stadium",
        38.9076,
        -76.8645,
        RoofType.OUTDOOR,
        SurfaceType.NATURAL_GRASS,
        ("WAS",),
        ("FedExField", "Commanders Field"),
    ),
)


def _normalize_lookup(value: str) -> str:
    return " ".join(value.casefold().replace("&", "and").split())


STADIUMS_BY_NAME: dict[str, Stadium] = {
    _normalize_lookup(name): stadium
    for stadium in STADIUMS
    for name in (stadium.name, *stadium.aliases)
}

STADIUMS_BY_TEAM: dict[str, Stadium] = {
    team: stadium for stadium in STADIUMS for team in stadium.home_teams
}

_TEAM_CODES_BY_NAME = {
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


def find_stadium(name: str) -> Stadium | None:
    """Resolve a canonical stadium name or maintained historical alias."""

    return STADIUMS_BY_NAME.get(_normalize_lookup(name))


def find_stadium_for_team(team: str) -> Stadium | None:
    """Resolve a home venue from an NFL abbreviation or full team name."""

    normalized = team.strip().upper()
    team_code = (
        normalized
        if normalized in STADIUMS_BY_TEAM
        else _TEAM_CODES_BY_NAME.get(_normalize_lookup(team))
    )
    return None if team_code is None else STADIUMS_BY_TEAM[team_code]
