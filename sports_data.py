"""Sports data clients for NHL, NBA, and MLB with TTL caching.

NHL Web API base: https://api-web.nhle.com/v1 — no auth required.
balldontlie API base: https://api.balldontlie.io/v1 — auth via BALLDONTLIE_API_KEY env var.
MLB Stats API base: https://statsapi.mlb.com/api/v1 — no auth required.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------

# Known team abbreviations for supported leagues.
_NBA_TEAMS = {
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}

_NHL_TEAMS = {
    "ANA", "ARI", "BOS", "BUF", "CGY", "CAR", "CHI", "COL", "CBJ", "DAL",
    "DET", "EDM", "FLA", "LAK", "MIN", "MTL", "NSH", "NJD", "NYI", "NYR",
    "OTT", "PHI", "PIT", "SJS", "SEA", "STL", "TBL", "TOR", "UTA", "VAN",
    "VGK", "WPG", "WSH",
}

_MLB_TEAMS = {
    "LAA", "AZ", "BAL", "BOS", "CHC", "CIN", "CLE", "COL", "DET", "HOU",
    "KC", "LAD", "WSH", "NYM", "ATH", "PIT", "SD", "SEA", "SF", "STL",
    "TB", "TEX", "TOR", "MIN", "PHI", "ATL", "CWS", "MIA", "NYY", "MIL",
}

# Map team.id → abbreviation for the MLB Stats API
_MLB_ID_TO_ABBREV = {
    108: "LAA", 109: "AZ", 110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KC", 119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SD", 136: "SEA", 137: "SF", 138: "STL",
    139: "TB", 140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}

# NCAA teams are not enumerated — we accept any 2-5 letter code for NCAAMB.
# We still try to extract them using the date-stripping approach.

# Map league prefix → (sport label, team set or None for NCAA)
_LEAGUE_MAP = {
    "KXNBA": ("nba", _NBA_TEAMS),
    "KXNHL": ("nhl", _NHL_TEAMS),
    "KXNCAAMB": ("ncaa", None),
    "KXMLB": ("mlb", _MLB_TEAMS),
}

# Type keywords found in the ticker (after league prefix, before the date)
_TYPE_MAP = {
    "GAME": "game",
    "SPREAD": "spread",
    "TOTAL": "total",
    "1HSPREAD": "spread",
    "1HTOTAL": "total",
    "FIRST10": "props",
    "REB": "props",
    "PTS": "props",
    "AST": "props",
    "BLK": "props",
    "STL": "props",
    "3PT": "props",
    "DREB": "props",
    "OREB": "props",
}

# Date pattern: 2 digits + 3 uppercase letters + 2 digits (e.g. 26MAR18)
_DATE_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}")


def _extract_teams(matchup_str: str, team_set: set[str] | None) -> list[str]:
    """Extract two 2-5 letter team codes from a concatenated matchup string.

    Strategy: greedily match known team abbreviations left-to-right.
    Falls back to splitting the string into two equal halves if no teams found.
    """
    if team_set is not None:
        # Try all possible split points, preferring 3-letter abbreviations
        teams: list[str] = []
        remaining = matchup_str
        while remaining and len(teams) < 2:
            matched = False
            # Try lengths 2–5 (longest first to avoid greedy short-match errors)
            for length in (4, 3, 2, 5):
                candidate = remaining[:length]
                if candidate in team_set:
                    teams.append(candidate)
                    remaining = remaining[length:]
                    matched = True
                    break
            if not matched:
                # Skip one character and try again
                remaining = remaining[1:]
        return teams
    else:
        # NCAA: try known Kalshi codes from mapping first, then fallback
        teams = []
        remaining = matchup_str
        while remaining and len(teams) < 2:
            matched = False
            # Try known Kalshi NCAA codes (longest first)
            for length in (6, 5, 4, 3, 2):
                if length > len(remaining):
                    continue
                candidate = remaining[:length]
                if candidate in _KALSHI_NCAA_MAP:
                    teams.append(candidate)
                    remaining = remaining[length:]
                    matched = True
                    break
            if not matched:
                # Fallback: take 2-5 alpha chars
                for length in (5, 4, 3, 2):
                    if length > len(remaining):
                        continue
                    candidate = remaining[:length]
                    if candidate.isalpha():
                        teams.append(candidate)
                        remaining = remaining[length:]
                        matched = True
                        break
            if not matched:
                remaining = remaining[1:]
        return teams


def parse_sports_ticker(ticker: str) -> dict | None:
    """Parse a Kalshi ticker string and return structured sports data.

    Returns None if the ticker does not match a supported sports league
    (NBA, NHL, MLB, or NCAA men's basketball).

    Return format::

        {
            "sport": "nba" | "nhl" | "mlb" | "ncaa",
            "type": "game" | "spread" | "total" | "props",
            "matchup": ["TEAM1", "TEAM2"],   # may be empty if not parseable
            "team": "TOR",                   # the bet target team (may be "")
            "raw": ticker,
        }
    """
    # Determine which league this ticker belongs to (longest prefix first)
    sport: str | None = None
    team_set: set[str] | None = None
    remainder: str = ""  # everything after the league prefix

    for prefix in sorted(_LEAGUE_MAP, key=len, reverse=True):
        if ticker.startswith(prefix):
            sport, team_set = _LEAGUE_MAP[prefix]
            remainder = ticker[len(prefix):]
            break

    if sport is None:
        return None

    # remainder looks like: "GAME-26MAR20TORDEN-TOR"
    # Split on the first "-" to get the type segment and the rest
    if "-" not in remainder:
        return None

    type_segment, rest = remainder.split("-", 1)

    # Map type segment to a canonical type
    # type_segment may have extra chars (e.g. "1HSPREAD") — match known keys
    ticker_type = "game"  # default
    for key, val in sorted(_TYPE_MAP.items(), key=lambda x: -len(x[0])):
        if type_segment.startswith(key) or type_segment == key:
            ticker_type = val
            break

    # rest looks like: "26MAR20TORDEN-TOR"
    # Strip the date prefix
    date_match = _DATE_RE.match(rest)
    if not date_match:
        return None

    after_date = rest[date_match.end():]  # e.g. "TORDEN-TOR"

    # Split on "-" to separate matchup from bet target
    if "-" in after_date:
        matchup_raw, bet_target = after_date.split("-", 1)
    else:
        matchup_raw = after_date
        bet_target = ""

    # Extract teams from matchup_raw
    matchup = _extract_teams(matchup_raw, team_set)

    # Extract the team from bet_target (strip trailing digits/special chars)
    team = re.match(r"^([A-Z]+)", bet_target)
    team_str = team.group(1) if team else ""

    return {
        "sport": sport,
        "type": ticker_type,
        "matchup": matchup,
        "team": team_str,
        "raw": ticker,
    }

_NHL_BASE = "https://api-web.nhle.com/v1"


class _Cache:
    """Simple TTL cache. Avoids hammering APIs on every 30-second bot cycle."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str, ttl_seconds: float) -> Any | None:
        """Return cached value if it exists and hasn't expired, else None."""
        if key not in self._store:
            return None
        value, ts = self._store[key]
        if time.monotonic() - ts > ttl_seconds:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        """Store value with current timestamp."""
        self._store[key] = (value, time.monotonic())

    def invalidate(self, key: str) -> None:
        """Manually remove a key."""
        self._store.pop(key, None)


class NHLClient:
    """Client for the NHL Web API.

    All methods return plain dicts/lists so callers don't need NHL types.
    Results are cached to avoid excessive requests during the bot's tight loop.
    """

    _STANDINGS_TTL = 30 * 60      # 30 minutes
    _SCHEDULE_TTL = 10 * 60       # 10 minutes
    _PLAYER_TTL = 60 * 60         # 1 hour

    def __init__(self, base_url: str = _NHL_BASE) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=10.0, follow_redirects=True)
        self._cache = _Cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_standings(self) -> dict[str, dict]:
        """Return standings keyed by team abbreviation.

        Example value::

            {
                "BOS": {
                    "wins": 42, "losses": 20, "otl": 8,
                    "points": 92, "streak": "W3",
                    "games_played": 70, "goal_diff": 45,
                    "home_wins": 22, "home_losses": 9,
                    "away_wins": 20, "away_losses": 11,
                    "l10_wins": 7, "l10_losses": 3,
                },
                ...
            }

        Cached for 30 minutes.
        """
        cached = self._cache.get("standings", self._STANDINGS_TTL)
        if cached is not None:
            return cached

        data = self._get("/standings/now")
        result: dict[str, dict] = {}
        for entry in data.get("standings", []):
            abbrev = entry.get("teamAbbrev", {}).get("default", "")
            if not abbrev:
                continue
            result[abbrev] = {
                "wins": entry.get("wins", 0),
                "losses": entry.get("losses", 0),
                "otl": entry.get("otLosses", 0),
                "points": entry.get("points", 0),
                "streak": entry.get("streakCode", ""),
                "games_played": entry.get("gamesPlayed", 0),
                "goal_diff": entry.get("goalDifferential", 0),
                "home_wins": entry.get("homeWins", 0),
                "home_losses": entry.get("homeLosses", 0),
                "away_wins": entry.get("roadWins", 0),
                "away_losses": entry.get("roadLosses", 0),
                "l10_wins": entry.get("l10Wins", 0),
                "l10_losses": entry.get("l10Losses", 0),
            }

        self._cache.set("standings", result)
        return result

    def get_schedule_today(self) -> list[dict]:
        """Return today's games.

        Example item::

            {"id": 2024020900, "home": "BOS", "away": "TOR", "state": "LIVE"}

        Cached for 10 minutes.
        """
        cached = self._cache.get("schedule_today", self._SCHEDULE_TTL)
        if cached is not None:
            return cached

        data = self._get("/schedule/now")
        games: list[dict] = []
        for day in data.get("gameWeek", []):
            for game in day.get("games", []):
                games.append({
                    "id": game.get("id"),
                    "home": game.get("homeTeam", {}).get("abbrev", ""),
                    "away": game.get("awayTeam", {}).get("abbrev", ""),
                    "state": game.get("gameState", ""),
                })

        self._cache.set("schedule_today", games)
        return games

    def get_player_stats(self, player_id: int) -> dict:
        """Return current-season stats for a player.

        Example::

            {"goals": 30, "assists": 45, "points": 75, "games_played": 68, "ppg": 1.103}

        Cached for 1 hour.
        """
        cache_key = f"player_{player_id}"
        cached = self._cache.get(cache_key, self._PLAYER_TTL)
        if cached is not None:
            return cached

        data = self._get(f"/player/{player_id}/landing")
        sub = (
            data
            .get("featuredStats", {})
            .get("regularSeason", {})
            .get("subSeason", {})
        )
        goals = sub.get("goals", 0)
        assists = sub.get("assists", 0)
        points = sub.get("points", 0)
        gp = sub.get("gamesPlayed", 0)
        ppg = round(points / gp, 3) if gp else 0.0

        result = {
            "goals": goals,
            "assists": assists,
            "points": points,
            "games_played": gp,
            "ppg": ppg,
        }
        self._cache.set(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict:
        url = f"{self._base}{path}"
        resp = self._http.get(url)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "NHLClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


_NBA_BASE = "https://api.balldontlie.io/v1"


class NBAClient:
    """Client for the balldontlie NBA API.

    All methods return plain dicts/lists so callers don't need NBA types.
    Results are cached to stay within the free tier (5 req/min).

    Auth: set env var BALLDONTLIE_API_KEY.
    """

    _STANDINGS_TTL = 30 * 60   # 30 minutes
    _GAMES_TTL = 10 * 60       # 10 minutes
    _INJURIES_TTL = 60 * 60    # 1 hour

    def __init__(self, base_url: str = _NBA_BASE) -> None:
        api_key = os.environ.get("BALLDONTLIE_API_KEY", "")
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=10.0,
            follow_redirects=True,
            headers={"Authorization": api_key} if api_key else {},
        )
        self._cache = _Cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_standings(self) -> dict[str, dict]:
        """Return NBA standings keyed by team abbreviation.

        Example value::

            {
                "BOS": {
                    "name": "Boston Celtics",
                    "wins": 50, "losses": 15,
                    "conf_rank": 1,
                    "home_record": "28-5",
                    "road_record": "22-10",
                },
                ...
            }

        Cached for 30 minutes.
        """
        cached = self._cache.get("nba_standings", self._STANDINGS_TTL)
        if cached is not None:
            return cached

        data = self._get("/standings", params={"season": "2025"})
        result: dict[str, dict] = {}
        for entry in data.get("data", []):
            team = entry.get("team", {})
            abbrev = team.get("abbreviation", "")
            if not abbrev:
                continue
            result[abbrev] = {
                "name": team.get("full_name", ""),
                "wins": entry.get("wins", 0),
                "losses": entry.get("losses", 0),
                "conf_rank": entry.get("conference_rank", 0),
                "home_record": entry.get("home_record", ""),
                "road_record": entry.get("road_record", ""),
            }

        self._cache.set("nba_standings", result)
        return result

    def get_games_today(self, date_str: str | None = None) -> list[dict]:
        """Return NBA games for a given date (defaults to today).

        ``date_str`` should be in ``YYYY-MM-DD`` format.

        Example item::

            {
                "id": 1234567,
                "home": "BOS", "away": "LAL",
                "home_score": 110, "away_score": 105,
                "status": "Final",
            }

        Cached for 10 minutes.
        """
        if date_str is None:
            import datetime
            date_str = datetime.date.today().isoformat()

        cache_key = f"nba_games_{date_str}"
        cached = self._cache.get(cache_key, self._GAMES_TTL)
        if cached is not None:
            return cached

        data = self._get("/games", params={"dates[]": date_str})
        games: list[dict] = []
        for game in data.get("data", []):
            home_team = game.get("home_team", {})
            visitor_team = game.get("visitor_team", {})
            games.append({
                "id": game.get("id"),
                "home": home_team.get("abbreviation", ""),
                "away": visitor_team.get("abbreviation", ""),
                "home_score": game.get("home_team_score", 0),
                "away_score": game.get("visitor_team_score", 0),
                "status": game.get("status", ""),
            })

        self._cache.set(cache_key, games)
        return games

    def get_player_injuries(self) -> list[dict]:
        """Return current NBA player injury report.

        Example item::

            {
                "player": "Jayson Tatum",
                "team": "BOS",
                "status": "Questionable",
                "description": "Left ankle soreness",
            }

        Cached for 1 hour.
        """
        cached = self._cache.get("nba_injuries", self._INJURIES_TTL)
        if cached is not None:
            return cached

        data = self._get("/player_injuries")
        injuries: list[dict] = []
        for entry in data.get("data", []):
            player = entry.get("player", {})
            team = entry.get("team", {})
            first = player.get("first_name", "")
            last = player.get("last_name", "")
            full_name = f"{first} {last}".strip()
            injuries.append({
                "player": full_name,
                "team": team.get("abbreviation", ""),
                "status": entry.get("status", ""),
                "description": entry.get("description", ""),
            })

        self._cache.set("nba_injuries", injuries)
        return injuries

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self._base}{path}"
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "NBAClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


_NCAA_BASE = "https://ncaa-api.henrygd.me"


class NCAAClient:
    """Client for the NCAA API (henrygd/ncaa-api).

    Free, no auth. 5 req/sec rate limit.
    Provides standings, rankings, and scoreboard for March Madness signals.
    """

    _STANDINGS_TTL = 30 * 60   # 30 min
    _RANKINGS_TTL = 60 * 60    # 1 hour — rankings update infrequently
    _SCOREBOARD_TTL = 5 * 60   # 5 min — live scores

    def __init__(self, base_url: str = _NCAA_BASE) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=10.0, follow_redirects=True)
        self._cache = _Cache()

    def get_standings(self) -> dict[str, dict]:
        """Return NCAA basketball standings keyed by school name (uppercase, no spaces).

        Keys are normalized: "Duke" -> "DUKE", "Ohio St." -> "OHIOST", etc.

        Example::

            {
                "DUKE": {"name": "Duke", "wins": 32, "losses": 2, "conf_wins": 17,
                         "conf_losses": 1, "streak": "W11", "pct": 0.941},
                ...
            }
        """
        cached = self._cache.get("ncaa_standings", self._STANDINGS_TTL)
        if cached is not None:
            return cached

        try:
            raw = self._get("/standings/basketball-men/d1")
        except Exception as exc:
            logger.warning("NCAA standings fetch failed: %s", exc)
            return {}

        result: dict[str, dict] = {}
        conferences = raw.get("data", []) if isinstance(raw, dict) else raw
        for conf in conferences:
            for team in conf.get("standings", []):
                name = team.get("School", "")
                key = _ncaa_normalize(name)
                if not key:
                    continue

                wins = _safe_int(team.get("Overall W", 0))
                losses = _safe_int(team.get("Overall L", 0))
                conf_wins = _safe_int(team.get("Conference W", 0))
                conf_losses = _safe_int(team.get("Conference L", 0))
                pct = wins / (wins + losses) if (wins + losses) > 0 else 0.5

                # Parse streak: "Won 11" -> "W11", "Lost 1" -> "L1"
                streak_raw = team.get("Overall STREAK", "")
                streak = ""
                if streak_raw.startswith("Won"):
                    streak = "W" + streak_raw.split()[-1]
                elif streak_raw.startswith("Lost"):
                    streak = "L" + streak_raw.split()[-1]

                result[key] = {
                    "name": name,
                    "wins": wins,
                    "losses": losses,
                    "conf_wins": conf_wins,
                    "conf_losses": conf_losses,
                    "streak": streak,
                    "pct": pct,
                }

        self._cache.set("ncaa_standings", result)
        logger.info("NCAA standings: loaded %d teams", len(result))
        return result

    def get_rankings(self) -> dict[str, int]:
        """Return AP poll rankings: {normalized_name: rank}.

        E.g. {"DUKE": 1, "ARIZONA": 2, "MICHIGAN": 3, ...}
        """
        cached = self._cache.get("ncaa_rankings", self._RANKINGS_TTL)
        if cached is not None:
            return cached

        try:
            raw = self._get("/rankings/basketball-men/d1/associated-press")
        except Exception as exc:
            logger.warning("NCAA rankings fetch failed: %s", exc)
            return {}

        result: dict[str, int] = {}
        rankings = raw.get("data", []) if isinstance(raw, dict) else raw
        for entry in rankings:
            rank = _safe_int(entry.get("RANK", 99))
            # School field: "Duke (50)" or "Michigan" — strip vote count
            school_raw = entry.get("SCHOOL (1ST PLACE VOTES)", entry.get("SCHOOL", ""))
            school = re.sub(r"\s*\(\d+\)\s*$", "", school_raw).strip()
            key = _ncaa_normalize(school)
            if key:
                result[key] = rank

        self._cache.set("ncaa_rankings", result)
        logger.info("NCAA rankings: loaded %d teams", len(result))
        return result

    def get_scoreboard_today(self) -> list[dict]:
        """Return today's NCAA men's basketball games.

        Returns list of {home, away, home_seed, away_seed, home_score, away_score, state}.
        Team names are the char6/short code from the API (e.g. "TCU", "OHIOST").
        """
        cached = self._cache.get("ncaa_scoreboard", self._SCOREBOARD_TTL)
        if cached is not None:
            return cached

        today = datetime.date.today()

        try:
            data = self._get(
                f"/scoreboard/basketball-men/d1/{today.year}/{today.month:02d}/{today.day:02d}"
            )
        except Exception as exc:
            logger.warning("NCAA scoreboard fetch failed: %s", exc)
            return []

        games: list[dict] = []
        for entry in data.get("games", []):
            g = entry.get("game", entry)
            home = g.get("home", {})
            away = g.get("away", {})

            home_names = home.get("names", {})
            away_names = away.get("names", {})

            games.append({
                "home": (home_names.get("char6") or home_names.get("short", "")).upper(),
                "away": (away_names.get("char6") or away_names.get("short", "")).upper(),
                "home_seed": _safe_int(home.get("seed", 0)),
                "away_seed": _safe_int(away.get("seed", 0)),
                "home_score": _safe_int(home.get("score", 0)),
                "away_score": _safe_int(away.get("score", 0)),
                "state": g.get("gameState", ""),
                "start_time": g.get("startTime", ""),
            })

        self._cache.set("ncaa_scoreboard", games)
        return games

    def _get(self, path: str, params: dict = None) -> Any:
        url = f"{self._base}{path}"
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NCAAClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _safe_int(val: Any) -> int:
    """Safely convert to int, returning 0 on failure."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _ncaa_normalize(name: str) -> str:
    """Normalize NCAA school name to uppercase key.

    'Duke' -> 'DUKE', 'Ohio St.' -> 'OHIOST', 'Miami (FL)' -> 'MIAMI(FL)'
    """
    if not name:
        return ""
    # Remove periods, strip spaces but keep parenthetical for disambiguation
    return re.sub(r"[\s.]", "", name).upper()


_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_MLB_ABBREV_TO_ID = {v: k for k, v in _MLB_ID_TO_ABBREV.items()}


class MLBClient:
    """Client for the MLB Stats API.

    All methods return plain dicts/lists so callers don't need MLB types.
    Results are cached to avoid excessive requests during the bot's tight loop.
    No auth required.
    """

    _STANDINGS_TTL = 30 * 60   # 30 minutes
    _SCHEDULE_TTL = 10 * 60    # 10 minutes
    _ROSTER_TTL = 60 * 60      # 1 hour — rosters rarely change mid-day
    _PLAYER_TTL = 2 * 60 * 60  # 2 hours — stats update after games

    def __init__(self, base_url: str = _MLB_BASE) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=10.0, follow_redirects=True)
        self._cache = _Cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_standings(self) -> dict[str, dict]:
        """Return MLB standings keyed by team abbreviation.

        Example value::

            {
                "NYY": {
                    "wins": 95, "losses": 67, "pct": 0.586,
                    "division_rank": 1, "run_diff": 120,
                    "home_wins": 52, "home_losses": 29,
                    "away_wins": 43, "away_losses": 38,
                    "l10_wins": 7, "l10_losses": 3,
                },
                ...
            }

        Cached for 30 minutes.
        """
        cached = self._cache.get("mlb_standings", self._STANDINGS_TTL)
        if cached is not None:
            return cached

        data = self._get("/standings", params={"leagueId": "103,104"})
        result: dict[str, dict] = {}

        for record in data.get("records", []):
            for team_record in record.get("teamRecords", []):
                team_id = team_record.get("team", {}).get("id")
                abbrev = _MLB_ID_TO_ABBREV.get(team_id, "")
                if not abbrev:
                    continue

                wins = team_record.get("wins", 0)
                losses = team_record.get("losses", 0)
                pct_str = team_record.get("winningPercentage", "0")
                try:
                    pct = float(pct_str)
                except (ValueError, TypeError):
                    pct = wins / (wins + losses) if (wins + losses) > 0 else 0.5

                # Parse split records for home/away/last10
                home_wins = home_losses = 0
                away_wins = away_losses = 0
                l10_wins = l10_losses = 0
                for split in team_record.get("records", {}).get("splitRecords", []):
                    split_type = split.get("type", "")
                    if split_type == "home":
                        home_wins = split.get("wins", 0)
                        home_losses = split.get("losses", 0)
                    elif split_type == "away":
                        away_wins = split.get("wins", 0)
                        away_losses = split.get("losses", 0)
                    elif split_type == "lastTen":
                        l10_wins = split.get("wins", 0)
                        l10_losses = split.get("losses", 0)

                result[abbrev] = {
                    "wins": wins,
                    "losses": losses,
                    "pct": pct,
                    "division_rank": team_record.get("divisionRank", 0),
                    "run_diff": team_record.get("runDifferential", 0),
                    "home_wins": home_wins,
                    "home_losses": home_losses,
                    "away_wins": away_wins,
                    "away_losses": away_losses,
                    "l10_wins": l10_wins,
                    "l10_losses": l10_losses,
                }

        self._cache.set("mlb_standings", result)
        return result

    def get_schedule_today(self) -> list[dict]:
        """Return today's MLB games (regular season only).

        Example item::

            {
                "id": 745438,
                "home": "NYY", "away": "BOS",
                "home_score": 5, "away_score": 3,
                "state": "Final",
                "game_type": "R",
            }

        Cached for 10 minutes.
        """
        cached = self._cache.get("mlb_schedule_today", self._SCHEDULE_TTL)
        if cached is not None:
            return cached

        today = datetime.date.today().isoformat()
        data = self._get("/schedule", params={"sportId": "1", "startDate": today, "endDate": today})
        games: list[dict] = []

        for day in data.get("dates", []):
            for game in day.get("games", []):
                home_id = game.get("teams", {}).get("home", {}).get("team", {}).get("id")
                away_id = game.get("teams", {}).get("away", {}).get("team", {}).get("id")
                games.append({
                    "id": game.get("gamePk"),
                    "home": _MLB_ID_TO_ABBREV.get(home_id, ""),
                    "away": _MLB_ID_TO_ABBREV.get(away_id, ""),
                    "home_score": game.get("teams", {}).get("home", {}).get("score", 0),
                    "away_score": game.get("teams", {}).get("away", {}).get("score", 0),
                    "state": game.get("status", {}).get("abstractGameState", ""),
                    "game_type": game.get("gameType", ""),
                })

        self._cache.set("mlb_schedule_today", games)
        return games

    def get_team_roster(self, team_abbrev: str) -> dict[str, int]:
        """Return {player_name_lower: player_id} for active roster.

        Cached for 1 hour.
        """
        cache_key = f"mlb_roster_{team_abbrev}"
        cached = self._cache.get(cache_key, self._ROSTER_TTL)
        if cached is not None:
            return cached

        # Reverse lookup: abbrev → team ID
        team_id = _MLB_ABBREV_TO_ID.get(team_abbrev)
        if not team_id:
            return {}

        try:
            data = self._get(f"/teams/{team_id}/roster", params={"rosterType": "active"})
        except Exception:
            return {}

        roster: dict[str, int] = {}
        for entry in data.get("roster", []):
            person = entry.get("person", {})
            name = person.get("fullName", "")
            pid = person.get("id")
            if name and pid:
                roster[name.lower()] = pid
        self._cache.set(cache_key, roster)
        return roster

    def find_pitcher_id(self, pitcher_name: str, team_abbrev: str) -> int | None:
        """Look up a pitcher's MLB player ID by name and team.

        Tries exact match first, then substring match.
        """
        if not pitcher_name or pitcher_name in ("TBD", "Bullpen"):
            return None
        roster = self.get_team_roster(team_abbrev)
        name_lower = pitcher_name.lower()

        # Exact match
        if name_lower in roster:
            return roster[name_lower]

        # Substring / last-name match
        for rname, pid in roster.items():
            if name_lower in rname or rname.endswith(name_lower.split()[-1]):
                return pid
        return None

    def get_pitcher_season_stats(self, player_id: int) -> dict | None:
        """Return pitcher's current season stats.

        Returns:
            {"wins": 8, "losses": 3, "era": "3.21", "whip": "1.05", "ip": "120.1"}
            or None if unavailable.
        """
        cache_key = f"mlb_pitcher_season_{player_id}"
        cached = self._cache.get(cache_key, self._PLAYER_TTL)
        if cached is not None:
            return cached

        year = datetime.date.today().year
        try:
            data = self._get(
                f"/people/{player_id}/stats",
                params={"stats": "season", "season": str(year), "group": "pitching"},
            )
        except Exception:
            return None

        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return None

        stat = splits[0].get("stat", {})
        result = {
            "wins": stat.get("wins", 0),
            "losses": stat.get("losses", 0),
            "era": stat.get("era", "0.00"),
            "whip": stat.get("whip", "0.00"),
            "ip": stat.get("inningsPitched", "0.0"),
        }
        self._cache.set(cache_key, result)
        return result

    def get_pitcher_game_log(self, player_id: int, last_n: int = 5) -> list[str]:
        """Return pitcher's last N starts as W/L list, e.g. ["W", "L", "W", "W", "L"].

        Returns empty list if no data available (preseason).
        """
        cache_key = f"mlb_pitcher_log_{player_id}_{last_n}"
        cached = self._cache.get(cache_key, self._PLAYER_TTL)
        if cached is not None:
            return cached

        year = datetime.date.today().year
        try:
            data = self._get(
                f"/people/{player_id}/stats",
                params={"stats": "gameLog", "season": str(year), "group": "pitching"},
            )
        except Exception:
            return []

        splits = data.get("stats", [{}])[0].get("splits", [])
        results: list[str] = []
        for s in splits:
            stat = s.get("stat", {})
            w = stat.get("wins", 0)
            l = stat.get("losses", 0)
            if w > 0:
                results.append("W")
            elif l > 0:
                results.append("L")
            else:
                results.append("-")  # no decision

        result = results[-last_n:]
        self._cache.set(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self._base}{path}"
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "MLBClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Singleton clients — lazily initialized, shared across all signal calls
# ---------------------------------------------------------------------------

_nhl_client: NHLClient | None = None
_nba_client: NBAClient | None = None
_mlb_client: MLBClient | None = None
_ncaa_client: NCAAClient | None = None


def _get_nhl_client() -> NHLClient:
    global _nhl_client
    if _nhl_client is None:
        _nhl_client = NHLClient()
    return _nhl_client


def _get_nba_client() -> NBAClient:
    global _nba_client
    if _nba_client is None:
        _nba_client = NBAClient()
    return _nba_client


def _get_mlb_client() -> MLBClient:
    global _mlb_client
    if _mlb_client is None:
        _mlb_client = MLBClient()
    return _mlb_client


def _get_ncaa_client() -> NCAAClient:
    global _ncaa_client
    if _ncaa_client is None:
        _ncaa_client = NCAAClient()
    return _ncaa_client


# ---------------------------------------------------------------------------
# Internal signal helpers
# ---------------------------------------------------------------------------

def _nhl_signal(parsed: dict) -> dict:
    """Compute signal for NHL tickers using live standings."""
    ticker_type = parsed["type"]
    matchup = parsed["matchup"]
    team = parsed["team"]

    # For props/totals we have no team-strength model yet — return small boost
    if ticker_type in ("props", "total"):
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.10,
            "reason": f"NHL {ticker_type}: standings context available but no specific signal",
        }

    # For game/spread: compute strength difference from standings
    try:
        client = _get_nhl_client()
        standings = client.get_standings()
    except Exception as exc:
        logger.info("NHL standings fetch failed: %s", exc)
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.0,
            "reason": "NHL standings unavailable",
        }

    # Identify bet team and opponent
    if len(matchup) >= 2:
        if team and team in matchup:
            bet_team = team
            opponent = matchup[0] if matchup[1] == team else matchup[1]
        else:
            bet_team = matchup[0]
            opponent = matchup[1]
    elif team:
        bet_team = team
        opponent = ""
    else:
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.0,
            "reason": "NHL: could not identify teams from ticker",
        }

    bet_rec = standings.get(bet_team)
    opp_rec = standings.get(opponent) if opponent else None

    if bet_rec is None:
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.05,
            "reason": f"NHL: {bet_team} not found in standings",
        }

    # Compute points percentage: points / (games_played * 2)
    def pts_pct(rec: dict) -> float:
        gp = rec.get("games_played", 0)
        pts = rec.get("points", 0)
        return pts / (gp * 2) if gp > 0 else 0.5

    bet_pct = pts_pct(bet_rec)
    opp_pct = pts_pct(opp_rec) if opp_rec else 0.5

    strength_diff = bet_pct - opp_pct

    # L10 adjustment
    bet_l10_wins = bet_rec.get("l10_wins", 0)
    bet_l10_losses = bet_rec.get("l10_losses", 0)
    opp_l10_wins = opp_rec.get("l10_wins", 0) if opp_rec else 5
    opp_l10_losses = opp_rec.get("l10_losses", 0) if opp_rec else 5

    bet_l10 = bet_l10_wins / 10 if (bet_l10_wins + bet_l10_losses) > 0 else 0.5
    opp_l10 = opp_l10_wins / 10 if (opp_l10_wins + opp_l10_losses) > 0 else 0.5
    l10_diff = bet_l10 - opp_l10

    # p_adjust: strength + l10 factor, capped at ±0.20
    raw_adjust = strength_diff * 0.5 + l10_diff * 0.05
    p_adjust = max(-0.20, min(0.20, raw_adjust))

    confidence_boost = min(0.20, abs(strength_diff) * 0.4)

    reason = (
        f"NHL {ticker_type}: {bet_team} pts_pct={bet_pct:.3f} vs "
        f"{opponent or 'unknown'} pts_pct={opp_pct:.3f}, "
        f"strength_diff={strength_diff:+.3f}, l10_diff={l10_diff:+.3f}, "
        f"p_adjust={p_adjust:+.3f}"
    )
    logger.info(reason)

    return {
        "p_adjust": p_adjust,
        "confidence_boost": confidence_boost,
        "reason": reason,
    }


def _nba_signal(parsed: dict) -> dict:
    """Compute signal for NBA tickers.

    balldontlie free tier does not provide /standings (requires paid tier).
    We return a moderate confidence boost with no p_adjust to indicate
    we have sports context but cannot compute team strength.
    """
    ticker_type = parsed["type"]

    # If paid-tier standings are available, use them
    try:
        client = _get_nba_client()
        standings = client.get_standings()
        matchup = parsed["matchup"]
        team = parsed["team"]

        if standings and ticker_type in ("game", "spread") and len(matchup) >= 2:
            if team and team in matchup:
                bet_team = team
                opponent = matchup[0] if matchup[1] == team else matchup[1]
            else:
                bet_team = matchup[0]
                opponent = matchup[1]

            bet_rec = standings.get(bet_team)
            opp_rec = standings.get(opponent)

            if bet_rec and opp_rec:
                def win_pct(rec: dict) -> float:
                    w = rec.get("wins", 0)
                    l = rec.get("losses", 0)
                    return w / (w + l) if (w + l) > 0 else 0.5

                bet_wp = win_pct(bet_rec)
                opp_wp = win_pct(opp_rec)
                strength_diff = bet_wp - opp_wp
                p_adjust = max(-0.20, min(0.20, strength_diff * 0.5))
                confidence_boost = min(0.20, abs(strength_diff) * 0.4)
                reason = (
                    f"NBA {ticker_type}: {bet_team} win_pct={bet_wp:.3f} vs "
                    f"{opponent} win_pct={opp_wp:.3f}, p_adjust={p_adjust:+.3f}"
                )
                logger.info(reason)
                return {
                    "p_adjust": p_adjust,
                    "confidence_boost": confidence_boost,
                    "reason": reason,
                }
    except Exception as exc:
        logger.info("NBA standings fetch failed (may be free tier): %s", exc)

    # Free tier fallback — standings not available
    reason = f"NBA {ticker_type}: moderate confidence boost, no standings (free tier)"
    logger.info(reason)
    return {
        "p_adjust": 0.0,
        "confidence_boost": 0.10,
        "reason": reason,
    }


# Kalshi ticker code → normalized NCAA name mapping for common teams.
# Kalshi uses short codes; NCAA standings use full school names.
_KALSHI_NCAA_MAP: dict[str, str] = {
    "DUK": "DUKE", "ARI": "ARIZONA", "MICH": "MICHIGAN", "FLA": "FLORIDA",
    "HOU": "HOUSTON", "ISU": "IOWASTATE", "UCON": "UCONN", "PUR": "PURDUE",
    "AUB": "AUBURN", "TENN": "TENNESSEE", "BAYLOR": "BAYLOR", "BAY": "BAYLOR",
    "GONZ": "GONZAGA", "MARQ": "MARQUETTE", "TXAM": "TEXASA&M", "WISC": "WISCONSIN",
    "CLEM": "CLEMSON", "OREG": "OREGON", "MIZZOU": "MISSOURI", "ARK": "ARKANSAS",
    "NEB": "NEBRASKA", "VANDY": "VANDERBILT", "KU": "KANSAS", "UK": "KENTUCKY",
    "UNC": "NORTHCAROLINA", "NCST": "NCSTATE", "LOUIS": "LOUISVILLE",
    "MARYLD": "MARYLAND", "MSST": "MISSISSIPPISTATE", "OKLA": "OKLAHOMA",
    "TEXTECH": "TEXASTECH", "MIZZ": "MISSOURI", "CREIGH": "CREIGHTON",
    "SETON": "SETONHALL", "XAVIER": "XAVIER", "SDST": "SANDIEGOSTATE",
    "MEMPH": "MEMPHIS", "SMC": "SAINTMARY'S", "BYU": "BYU", "COLO": "COLORADO",
    "USC": "USC", "UCLA": "UCLA", "STAN": "STANFORD", "WASH": "WASHINGTON",
    "ILL": "ILLINOIS", "IOWA": "IOWA", "OSU": "OHIOSTATE", "MSU": "MICHIGANSTATE",
    "IND": "INDIANA", "MINN": "MINNESOTA", "NW": "NORTHWESTERN", "RUTG": "RUTGERS",
    "PENN": "PENNSTATE", "CAL": "CALIFORNIA", "ARIZ": "ARIZONA", "ORE": "OREGON",
    "COL": "COLORADO", "UTAH": "UTAH", "ASU": "ARIZONASTATE",
    "LIB": "LIBERTY", "NEV": "NEVADA", "MOH": "MOREHEADSTATE",
    "JOES": "SAINTJOSEPH'S", "PV": "PRAIRIEVIEW", "TROY": "TROY",
    "USF": "SOUTHFLORIDA", "HIGHPT": "HIGHPOINT", "SIENA": "SIENA",
    "MCNEES": "MCNEESESTATE", "HAWAII": "HAWAI'I",
    "MICHST": "MICHIGANSTATE", "VT": "VIRGINIATECH", "UVA": "VIRGINIA",
    "WAKE": "WAKEFOREST", "SYR": "SYRACUSE", "PITT": "PITTSBURGH",
    "GT": "GEORGIATECH", "FSU": "FLORIDASTATE", "ND": "NOTREDAME",
    "BC": "BOSTONCOLLEGE", "TTU": "TEXASTECH", "TCU": "TCU",
    "WVU": "WESTVIRGINIA", "OKST": "OKLAHOMASTATE", "KSU": "KANSASSTATE",
    "CINCY": "CINCINNATI", "UCF": "UCF", "CONN": "UCONN",
}


def _ncaa_resolve(code: str) -> str:
    """Resolve a Kalshi ticker code to normalized NCAA name."""
    code_up = code.upper()
    return _KALSHI_NCAA_MAP.get(code_up, code_up)


def _ncaa_fuzzy_lookup(data: dict[str, dict], code: str) -> dict | None:
    """Match a Kalshi ticker team code against NCAA standings."""
    if not code:
        return None
    # Try mapped name first
    resolved = _ncaa_resolve(code)
    if resolved in data:
        return data[resolved]
    # Direct match
    code_up = code.upper()
    if code_up in data:
        return data[code_up]
    # Prefix match
    for key, val in data.items():
        if key.startswith(resolved) or resolved.startswith(key[:4]):
            return val
    # Substring
    for key, val in data.items():
        if code_up in key or key in code_up:
            return val
    return None


def _ncaa_fuzzy_rank(rankings: dict[str, int], code: str) -> int:
    """Match a team code against AP rankings. Returns 99 if not ranked."""
    if not code:
        return 99
    resolved = _ncaa_resolve(code)
    if resolved in rankings:
        return rankings[resolved]
    code_up = code.upper()
    if code_up in rankings:
        return rankings[code_up]
    for key, rank in rankings.items():
        if key.startswith(resolved) or resolved.startswith(key[:4]):
            return rank
    return 99


def _ncaa_signal(parsed: dict) -> dict:
    """Compute signal for NCAA basketball tickers using standings + rankings.

    March Madness signal logic:
    - Ranked teams get a confidence boost (AP top 25 = strong data)
    - Win% difference between teams drives p_adjust
    - Seed/rank gap amplifies signal (1 vs 16 = strong, 8 vs 9 = weak)
    - Streaks factor in for momentum
    """
    ticker_type = parsed["type"]
    matchup = parsed["matchup"]
    team = parsed["team"]

    try:
        client = _get_ncaa_client()
        standings = client.get_standings()
        rankings = client.get_rankings()
    except Exception as exc:
        logger.info("NCAA data fetch failed: %s", exc)
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.05,
            "reason": "NCAA: data fetch failed, small boost only",
        }

    # For props we have no model
    if ticker_type == "props":
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.10,
            "reason": "NCAA props: standings context available but no specific signal",
        }

    # Identify bet team and opponent
    if len(matchup) >= 2:
        if team and team in matchup:
            bet_team = team
            opponent = matchup[0] if matchup[1] == team else matchup[1]
        else:
            bet_team = matchup[0]
            opponent = matchup[1]
    elif team:
        bet_team = team
        opponent = ""
    else:
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.05,
            "reason": "NCAA: could not identify teams from ticker",
        }

    # Fuzzy match against standings — NCAA ticker codes don't match school names
    bet_rec = _ncaa_fuzzy_lookup(standings, bet_team)
    opp_rec = _ncaa_fuzzy_lookup(standings, opponent) if opponent else None

    bet_pct = bet_rec["pct"] if bet_rec else 0.5
    opp_pct = opp_rec["pct"] if opp_rec else 0.5

    # Win % strength difference
    strength_diff = bet_pct - opp_pct

    # Ranking factor — bigger rank gap = stronger signal
    bet_rank = _ncaa_fuzzy_rank(rankings, bet_team)
    opp_rank = _ncaa_fuzzy_rank(rankings, opponent) if opponent else 99

    # Also check tournament seeds from scoreboard
    try:
        scoreboard = client.get_scoreboard_today()
        for g in scoreboard:
            home_norm = _ncaa_normalize(g["home"])
            away_norm = _ncaa_normalize(g["away"])
            if bet_team in home_norm or home_norm in bet_team:
                if g["home_seed"]:
                    bet_rank = min(bet_rank, g["home_seed"])
            elif bet_team in away_norm or away_norm in bet_team:
                if g["away_seed"]:
                    bet_rank = min(bet_rank, g["away_seed"])
            if opponent and (opponent in home_norm or home_norm in opponent):
                if g["home_seed"]:
                    opp_rank = min(opp_rank, g["home_seed"])
            elif opponent and (opponent in away_norm or away_norm in opponent):
                if g["away_seed"]:
                    opp_rank = min(opp_rank, g["away_seed"])
    except Exception:
        pass
    rank_diff = opp_rank - bet_rank  # positive = bet team ranked higher
    rank_factor = max(-0.10, min(0.10, rank_diff * 0.004))  # ~0.04 per 10-rank gap

    # Streak factor
    streak_factor = 0.0
    if bet_rec and isinstance(bet_rec.get("streak"), str):
        s = bet_rec["streak"]
        if s.startswith("W"):
            try:
                streak_factor = min(0.03, int(s[1:]) * 0.005)
            except ValueError:
                pass
        elif s.startswith("L"):
            try:
                streak_factor = max(-0.03, -int(s[1:]) * 0.005)
            except ValueError:
                pass

    # Combined p_adjust
    raw_adjust = strength_diff * 0.5 + rank_factor + streak_factor
    p_adjust = max(-0.25, min(0.25, raw_adjust))

    # Confidence: higher when we have rankings for both teams
    has_rank_data = bet_rank < 99 or opp_rank < 99
    confidence_boost = 0.20 if has_rank_data else 0.12

    # For totals, reduce signal — win% less relevant to totals
    if ticker_type == "total":
        p_adjust *= 0.3
        confidence_boost = 0.10

    bet_name = bet_rec["name"] if bet_rec else bet_team
    opp_name = opp_rec["name"] if opp_rec else opponent

    reason = (
        f"NCAA {ticker_type}: {bet_name} ({bet_pct:.3f}, #{bet_rank}) vs "
        f"{opp_name} ({opp_pct:.3f}, #{opp_rank}), "
        f"strength={strength_diff:+.3f}, rank_factor={rank_factor:+.3f}, "
        f"streak={streak_factor:+.3f}, p_adjust={p_adjust:+.3f}"
    )
    logger.info(reason)

    return {
        "p_adjust": p_adjust,
        "confidence_boost": confidence_boost,
        "reason": reason,
    }


def _mlb_signal(parsed: dict) -> dict:
    """Compute signal for MLB tickers using live standings."""
    ticker_type = parsed["type"]
    matchup = parsed["matchup"]
    team = parsed["team"]

    # For props we have no specific model yet
    if ticker_type == "props":
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.10,
            "reason": "MLB props: standings context available but no specific signal",
        }

    # For totals: use park factors + game-time weather to nudge over/under
    if ticker_type == "total":
        home_team = matchup[0] if len(matchup) >= 2 else team
        park_adjust = 0.0
        wind_adjust = 0.0
        reason_parts = []

        # Park factors
        try:
            from park_factors import get_park_factor, get_run_environment
            runs_factor = get_park_factor(home_team, "Runs")
            env = get_run_environment(home_team)
            park_adjust = (runs_factor - 100) / 1000.0
            park_adjust = max(-0.05, min(0.05, park_adjust))
            reason_parts.append(f"Runs={runs_factor}({env}) park={park_adjust:+.3f}")
        except Exception:
            reason_parts.append("park=N/A")

        # Game-time weather (wind + temp)
        try:
            from weather_data import wind_run_adjustment, get_game_weather
            wind_adjust = wind_run_adjustment(home_team, hour=19)
            weather = get_game_weather(home_team, hour=19)
            if weather:
                reason_parts.append(
                    f"wind={weather['wind_label']}{weather['wind_mph']:.0f}mph "
                    f"{weather['temp_f']:.0f}°F wx={wind_adjust:+.3f}"
                )
            else:
                reason_parts.append("wx=N/A")
        except Exception:
            reason_parts.append("wx=N/A")

        total_adjust = park_adjust + wind_adjust
        total_adjust = max(-0.06, min(0.06, total_adjust))

        return {
            "p_adjust": total_adjust,
            "confidence_boost": 0.18,
            "reason": f"MLB total @ {home_team}: {', '.join(reason_parts)}",
        }

    # For game/spread: compute strength difference from standings
    try:
        client = _get_mlb_client()
        standings = client.get_standings()
    except Exception as exc:
        logger.info("MLB standings fetch failed: %s", exc)
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.0,
            "reason": "MLB standings unavailable",
        }

    # Identify bet team and opponent
    if len(matchup) >= 2:
        if team and team in matchup:
            bet_team = team
            opponent = matchup[0] if matchup[1] == team else matchup[1]
        else:
            bet_team = matchup[0]
            opponent = matchup[1]
    elif team:
        bet_team = team
        opponent = ""
    else:
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.0,
            "reason": "MLB: could not identify teams from ticker",
        }

    bet_rec = standings.get(bet_team)
    opp_rec = standings.get(opponent) if opponent else None

    if bet_rec is None:
        return {
            "p_adjust": 0.0,
            "confidence_boost": 0.05,
            "reason": f"MLB: {bet_team} not found in standings (season may not be active)",
        }

    bet_pct = bet_rec.get("pct", 0.5)
    opp_pct = opp_rec.get("pct", 0.5) if opp_rec else 0.5

    strength_diff = bet_pct - opp_pct

    # L10 adjustment
    bet_l10_wins = bet_rec.get("l10_wins", 0)
    bet_l10_losses = bet_rec.get("l10_losses", 0)
    opp_l10_wins = opp_rec.get("l10_wins", 0) if opp_rec else 5
    opp_l10_losses = opp_rec.get("l10_losses", 0) if opp_rec else 5

    bet_l10 = bet_l10_wins / 10 if (bet_l10_wins + bet_l10_losses) > 0 else 0.5
    opp_l10 = opp_l10_wins / 10 if (opp_l10_wins + opp_l10_losses) > 0 else 0.5
    l10_diff = bet_l10 - opp_l10

    # p_adjust: strength + l10 factor, capped at ±0.20
    raw_adjust = strength_diff * 0.5 + l10_diff * 0.05
    p_adjust = max(-0.20, min(0.20, raw_adjust))

    confidence_boost = min(0.20, abs(strength_diff) * 0.4)

    reason = (
        f"MLB {ticker_type}: {bet_team} win_pct={bet_pct:.3f} vs "
        f"{opponent or 'unknown'} win_pct={opp_pct:.3f}, "
        f"strength_diff={strength_diff:+.3f}, l10_diff={l10_diff:+.3f}, "
        f"p_adjust={p_adjust:+.3f}"
    )
    logger.info(reason)

    return {
        "p_adjust": p_adjust,
        "confidence_boost": confidence_boost,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Public signal API
# ---------------------------------------------------------------------------

def get_sports_signal(ticker: str) -> dict | None:
    """Return a probability adjustment and confidence boost for a sports ticker.

    Args:
        ticker: Kalshi market ticker, e.g. ``"KXNHLGAME-26MAR20FLAEDM-FLA"``.

    Returns:
        A dict with keys ``p_adjust``, ``confidence_boost``, and ``reason``,
        or ``None`` if the ticker is not a supported sports market.

        - ``p_adjust``: float in [-0.25, +0.25] — shift p_hat toward team strength.
        - ``confidence_boost``: float in [0.0, 0.25] — boost confidence when data
          is available.
        - ``reason``: human-readable explanation of the signal.

    Never raises — all errors are caught and result in ``None`` or a
    zero-adjustment signal.
    """
    try:
        parsed = parse_sports_ticker(ticker)
        if parsed is None:
            return None

        sport = parsed["sport"]

        if sport == "nhl":
            return _nhl_signal(parsed)

        if sport == "nba":
            return _nba_signal(parsed)

        if sport == "mlb":
            return _mlb_signal(parsed)

        if sport == "ncaa":
            return _ncaa_signal(parsed)

        # Unknown supported league — should not happen given _LEAGUE_MAP
        return None

    except Exception as exc:
        logger.info("get_sports_signal(%s) failed: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Pitcher enrichment API — used by discord_bot.py
# ---------------------------------------------------------------------------

def get_pitcher_info(pitcher_name: str, team_abbrev: str) -> dict:
    """Look up a pitcher and return season stats + last 5 game log.

    Returns:
        {
            "name": "Gerrit Cole",
            "record": "8-3",
            "era": "3.21",
            "last5": ["W", "L", "W", "W", "L"],  # empty list if preseason
        }

    Never raises — returns safe defaults on any failure.
    """
    default = {
        "name": pitcher_name,
        "record": "0-0",
        "era": "0.00",
        "last5": [],
    }

    if not pitcher_name or pitcher_name in ("TBD", "Bullpen"):
        return default

    try:
        client = _get_mlb_client()
        pid = client.find_pitcher_id(pitcher_name, team_abbrev)
        if pid is None:
            return default

        stats = client.get_pitcher_season_stats(pid)
        game_log = client.get_pitcher_game_log(pid, last_n=5)

        if stats:
            default["record"] = f"{stats['wins']}-{stats['losses']}"
            default["era"] = stats["era"]
        default["last5"] = game_log
        return default

    except Exception as exc:
        logger.info("get_pitcher_info(%s, %s) failed: %s", pitcher_name, team_abbrev, exc)
        return default
