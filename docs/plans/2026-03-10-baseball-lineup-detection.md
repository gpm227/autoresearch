# Baseball Lineup Detection & Picks System

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Post baseball games to Discord when starting lineups confirm on RotoWire, parse expert responses, feed picks to trading bot, handle scratches/postponements, track outcomes.

**Architecture:** discord_bot.py gets a new background task that polls RotoWire every 5 min, parses HTML with BeautifulSoup, posts one message per game when lineups flip from expected→confirmed. Expert responses are parsed with fuzzy matching against posted games. strategy.py gets tunable params (cutoff, patience window). infra.py gets a hard daily stop-loss. Quiet hours (11pm-8am ET) suppress all Discord alerts.

**Tech Stack:** beautifulsoup4 (HTML parsing), httpx (already dep), discord.py (already dep)

---

## Task 1: Add beautifulsoup4 dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add dep**

```toml
# In [project] dependencies, add:
"beautifulsoup4>=4.12.0",
```

**Step 2: Lock**

Run: `uv lock`

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add beautifulsoup4 for RotoWire lineup parsing"
```

---

## Task 2: Strategy params for baseball picks

**Files:**
- Modify: `strategy.py` (add params after CONTRIBUTOR_WEIGHTS block, ~line 93)

**Step 1: Add tunable params**

```python
# ─── Baseball Picks Tuning ─────────────────────────────────────────────────

PICKS_CUTOFF_MINUTES_BEFORE_FIRST_PITCH = 60   # don't place new orders after this
PICKS_PATIENCE_WINDOW_MINUTES = 90              # wait this long for expert before cancelling
PICKS_SCRATCH_CANCEL_ORDERS = True              # cancel orders on pitcher scratch
PICKS_MAX_GAMES_PER_DAY = 10                    # max games to post
LINEUP_POLL_INTERVAL_SEC = 300                   # 5 minutes
```

**Step 2: Add function for scratch handling**

```python
def handle_scratch_decision(has_expert_pick: bool, minutes_to_pitch: int,
                            original_edge_cents: float) -> str:
    """
    Decide what to do when a starting pitcher is scratched.
    Returns: "cancel" | "hold" | "reduce"
    Autoresearch can rewrite this function.
    """
    if not has_expert_pick:
        return "cancel"
    if PICKS_SCRATCH_CANCEL_ORDERS:
        return "cancel"
    if minutes_to_pitch < PICKS_CUTOFF_MINUTES_BEFORE_FIRST_PITCH:
        return "cancel"
    return "hold"
```

**Step 3: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: 36 passed (no breakage)

**Step 4: Commit**

```bash
git add strategy.py
git commit -m "feat: add baseball picks tunable params to strategy.py"
```

---

## Task 3: RotoWire scraper module

**Files:**
- Create: `lineups.py`
- Test: `tests/test_lineups.py`

This is a standalone module with no Discord dependency — pure data extraction.

**Step 1: Write tests for the parser**

```python
# tests/test_lineups.py
import pytest
from lineups import parse_lineups_html, GameInfo

# Minimal HTML fixture matching RotoWire structure
CONFIRMED_GAME_HTML = '''
<div class="lineup is-mlb">
  <div class="lineup__meta flex-row">
    <div class="lineup__time">7:10 PM ET</div>
  </div>
  <div class="lineup__box">
    <div class="lineup__top">
      <div class="lineup__teams">
        <div class="lineup__team is-visit">
          <div class="lineup__abbr">LAD</div>
        </div>
        <div class="lineup__team is-home">
          <div class="lineup__abbr">SD</div>
        </div>
      </div>
    </div>
    <a class="lineup__matchup">
      <div class="lineup__mteam is-visit">Dodgers</div>
      <div class="lineup__mteam is-home">Padres</div>
    </a>
    <div class="lineup__main">
      <ul class="lineup__list is-visit">
        <li class="lineup__player-highlight mb-0">
          <div class="lineup__player-highlight-name">
            <a href="#">Shohei Ohtani</a><span class="lineup__throws">L</span>
          </div>
        </li>
        <li class="lineup__status is-confirmed">Confirmed Lineup</li>
      </ul>
      <ul class="lineup__list is-home">
        <li class="lineup__player-highlight mb-0">
          <div class="lineup__player-highlight-name">
            <a href="#">Yu Darvish</a><span class="lineup__throws">R</span>
          </div>
        </li>
        <li class="lineup__status is-confirmed">Confirmed Lineup</li>
      </ul>
    </div>
    <div class="lineup__odds">
      <div class="lineup__odds-item">
        <b>O/U</b>&nbsp;<span class="composite">8.5 Runs</span>
      </div>
    </div>
  </div>
</div>
'''

EXPECTED_GAME_HTML = '''
<div class="lineup is-mlb">
  <div class="lineup__meta flex-row">
    <div class="lineup__time">4:05 PM ET</div>
  </div>
  <div class="lineup__box">
    <div class="lineup__top">
      <div class="lineup__teams">
        <div class="lineup__team is-visit">
          <div class="lineup__abbr">NYY</div>
        </div>
        <div class="lineup__team is-home">
          <div class="lineup__abbr">DET</div>
        </div>
      </div>
    </div>
    <a class="lineup__matchup">
      <div class="lineup__mteam is-visit">Yankees</div>
      <div class="lineup__mteam is-home">Tigers</div>
    </a>
    <div class="lineup__main">
      <ul class="lineup__list is-visit">
        <li class="lineup__player-highlight mb-0">
          <div class="lineup__player-highlight-name">
            <a href="#">Gerrit Cole</a><span class="lineup__throws">R</span>
          </div>
        </li>
        <li class="lineup__status is-expected">Expected Lineup</li>
      </ul>
      <ul class="lineup__list is-home">
        <li class="lineup__player-highlight mb-0">
          <div class="lineup__player-highlight-name">
            <a href="#">Tarik Skubal</a><span class="lineup__throws">L</span>
          </div>
        </li>
        <li class="lineup__status is-expected">Expected Lineup</li>
      </ul>
    </div>
    <div class="lineup__odds">
      <div class="lineup__odds-item">
        <b>O/U</b>&nbsp;<span class="composite">7.5 Runs</span>
      </div>
    </div>
  </div>
</div>
'''

STARTED_GAME_HTML = '''
<div class="lineup is-mlb has-started">
  <div class="lineup__meta flex-row">
    <div class="lineup__time">1:05 PM ET</div>
  </div>
  <div class="lineup__box">
    <div class="lineup__top">
      <div class="lineup__teams">
        <div class="lineup__team is-visit"><div class="lineup__abbr">PHI</div></div>
        <div class="lineup__team is-home"><div class="lineup__abbr">BOS</div></div>
      </div>
    </div>
    <a class="lineup__matchup">
      <div class="lineup__mteam is-visit">Phillies</div>
      <div class="lineup__mteam is-home">Red Sox</div>
    </a>
    <div class="lineup__main">
      <ul class="lineup__list is-visit">
        <li class="lineup__player-highlight mb-0">
          <div class="lineup__player-highlight-name">
            <a href="#">Zack Wheeler</a><span class="lineup__throws">R</span>
          </div>
        </li>
        <li class="lineup__status is-confirmed">Confirmed Lineup</li>
      </ul>
      <ul class="lineup__list is-home">
        <li class="lineup__player-highlight mb-0">
          <div class="lineup__player-highlight-name">
            <a href="#">Brayan Bello</a><span class="lineup__throws">R</span>
          </div>
        </li>
        <li class="lineup__status is-confirmed">Confirmed Lineup</li>
      </ul>
    </div>
  </div>
</div>
'''


def test_parse_confirmed_game():
    games = parse_lineups_html(CONFIRMED_GAME_HTML)
    assert len(games) == 1
    g = games[0]
    assert g.away_abbr == "LAD"
    assert g.home_abbr == "SD"
    assert g.away_name == "Dodgers"
    assert g.home_name == "Padres"
    assert g.away_pitcher == "Shohei Ohtani"
    assert g.home_pitcher == "Yu Darvish"
    assert g.away_confirmed is True
    assert g.home_confirmed is True
    assert g.both_confirmed is True
    assert g.game_time == "7:10 PM ET"
    assert g.total == 8.5


def test_parse_expected_game():
    games = parse_lineups_html(EXPECTED_GAME_HTML)
    assert len(games) == 1
    g = games[0]
    assert g.away_pitcher == "Gerrit Cole"
    assert g.home_pitcher == "Tarik Skubal"
    assert g.away_confirmed is False
    assert g.home_confirmed is False
    assert g.both_confirmed is False
    assert g.total == 7.5


def test_skip_started_games():
    games = parse_lineups_html(STARTED_GAME_HTML)
    assert len(games) == 0  # has-started games are excluded


def test_combined_html():
    html = CONFIRMED_GAME_HTML + EXPECTED_GAME_HTML + STARTED_GAME_HTML
    games = parse_lineups_html(html)
    assert len(games) == 2  # confirmed + expected, not started
    confirmed = [g for g in games if g.both_confirmed]
    assert len(confirmed) == 1


def test_missing_total():
    html = CONFIRMED_GAME_HTML.replace(
        '<div class="lineup__odds">', '<!-- no odds -->'
    ).replace('</div>\n  </div>\n</div>', '</div>\n</div>')
    games = parse_lineups_html(html)
    assert len(games) == 1
    assert games[0].total is None


def test_empty_html():
    assert parse_lineups_html("") == []
    assert parse_lineups_html("<html></html>") == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_lineups.py -v`
Expected: FAIL (lineups module doesn't exist)

**Step 3: Implement lineups.py**

```python
# lineups.py — RotoWire lineup scraper. No Discord dependency.
"""
Parses RotoWire daily lineups HTML to detect confirmed starting lineups.
Polled every 5 minutes by the Discord bot background task.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

ROTOWIRE_URL = "https://www.rotowire.com/baseball/daily-lineups.php"
_USER_AGENT = "Mozilla/5.0 (compatible; KalshiBot/1.0)"


@dataclass
class GameInfo:
    """One baseball game parsed from RotoWire."""
    away_abbr: str
    home_abbr: str
    away_name: str
    home_name: str
    away_pitcher: str
    home_pitcher: str
    away_throws: str = ""       # R or L
    home_throws: str = ""
    away_confirmed: bool = False
    home_confirmed: bool = False
    game_time: str = ""         # e.g. "7:10 PM ET"
    total: Optional[float] = None  # over/under line
    moneyline: str = ""          # e.g. "LAD -150"

    @property
    def both_confirmed(self) -> bool:
        return self.away_confirmed and self.home_confirmed

    @property
    def game_key(self) -> str:
        """Unique key for this game (for dedup)."""
        return f"{self.away_abbr}@{self.home_abbr}"


def parse_lineups_html(html: str) -> list[GameInfo]:
    """
    Parse RotoWire daily lineups HTML.
    Returns GameInfo for each game that has NOT started yet.
    Games with class 'has-started' are excluded.
    """
    soup = BeautifulSoup(html, "html.parser")
    games: list[GameInfo] = []

    for card in soup.find_all("div", class_="lineup"):
        classes = card.get("class", [])
        # Skip non-MLB or already-started games
        if "is-mlb" not in classes:
            continue
        if "has-started" in classes:
            continue

        try:
            game = _parse_card(card)
            if game:
                games.append(game)
        except Exception as e:
            log.warning("Failed to parse lineup card: %s", e)

    return games


def _parse_card(card: Tag) -> Optional[GameInfo]:
    """Parse one lineup card div into a GameInfo."""
    # Game time
    time_div = card.find("div", class_="lineup__time")
    game_time = time_div.get_text(strip=True) if time_div else ""

    # Team abbreviations
    visit_team = card.find("div", class_=lambda c: c and "lineup__team" in c and "is-visit" in c)
    home_team = card.find("div", class_=lambda c: c and "lineup__team" in c and "is-home" in c)
    away_abbr = ""
    home_abbr = ""
    if visit_team:
        abbr_div = visit_team.find("div", class_="lineup__abbr")
        if abbr_div:
            away_abbr = abbr_div.get_text(strip=True)
    if home_team:
        abbr_div = home_team.find("div", class_="lineup__abbr")
        if abbr_div:
            home_abbr = abbr_div.get_text(strip=True)

    if not away_abbr or not home_abbr:
        return None

    # Full team names
    away_name = home_name = ""
    visit_mteam = card.find("div", class_=lambda c: c and "lineup__mteam" in c and "is-visit" in c)
    home_mteam = card.find("div", class_=lambda c: c and "lineup__mteam" in c and "is-home" in c)
    if visit_mteam:
        # Strip record like "(96-66)"
        away_name = re.sub(r"\s*\(\d+-\d+\)\s*", "", visit_mteam.get_text(strip=True))
    if home_mteam:
        home_name = re.sub(r"\s*\(\d+-\d+\)\s*", "", home_mteam.get_text(strip=True))

    # Lineup lists (visit and home)
    lists = card.find_all("ul", class_="lineup__list")
    away_pitcher = home_pitcher = ""
    away_throws = home_throws = ""
    away_confirmed = home_confirmed = False

    for ul in lists:
        is_visit = "is-visit" in (ul.get("class") or [])
        is_home = "is-home" in (ul.get("class") or [])

        # Starting pitcher
        highlight = ul.find("li", class_=lambda c: c and "lineup__player-highlight" in c)
        pitcher_name = ""
        throws = ""
        if highlight:
            name_div = highlight.find("div", class_="lineup__player-highlight-name")
            if name_div:
                a_tag = name_div.find("a")
                if a_tag:
                    pitcher_name = a_tag.get_text(strip=True)
                throws_span = name_div.find("span", class_="lineup__throws")
                if throws_span:
                    throws = throws_span.get_text(strip=True)

        # Lineup status
        status_li = ul.find("li", class_="lineup__status")
        confirmed = False
        if status_li:
            confirmed = "is-confirmed" in (status_li.get("class") or [])

        if is_visit:
            away_pitcher = pitcher_name
            away_throws = throws
            away_confirmed = confirmed
        elif is_home:
            home_pitcher = pitcher_name
            home_throws = throws
            home_confirmed = confirmed

    # Over/under total
    total: Optional[float] = None
    odds_div = card.find("div", class_="lineup__odds")
    if odds_div:
        for item in odds_div.find_all("div", class_="lineup__odds-item"):
            b_tag = item.find("b")
            if b_tag and "O/U" in b_tag.get_text():
                # Get composite span first, fall back to any span with runs
                composite = item.find("span", class_="composite")
                if composite:
                    text = composite.get_text(strip=True)
                else:
                    # Try any visible span
                    for span in item.find_all("span"):
                        if "hide" not in (span.get("class") or []):
                            text = span.get_text(strip=True)
                            break
                    else:
                        text = ""
                match = re.search(r"([\d.]+)", text)
                if match:
                    total = float(match.group(1))

    # Moneyline
    moneyline = ""
    if odds_div:
        for item in odds_div.find_all("div", class_="lineup__odds-item"):
            b_tag = item.find("b")
            if b_tag and "LINE" in b_tag.get_text():
                composite = item.find("span", class_="composite")
                if composite:
                    moneyline = composite.get_text(strip=True)
                break

    return GameInfo(
        away_abbr=away_abbr,
        home_abbr=home_abbr,
        away_name=away_name,
        home_name=home_name,
        away_pitcher=away_pitcher,
        home_pitcher=home_pitcher,
        away_throws=away_throws,
        home_throws=home_throws,
        away_confirmed=away_confirmed,
        home_confirmed=home_confirmed,
        game_time=game_time,
        total=total,
        moneyline=moneyline,
    )


def fetch_lineups() -> list[GameInfo]:
    """Fetch and parse today's lineups from RotoWire. Returns [] on error."""
    try:
        resp = httpx.get(ROTOWIRE_URL, headers={"User-Agent": _USER_AGENT}, timeout=15.0)
        resp.raise_for_status()
        return parse_lineups_html(resp.text)
    except Exception as e:
        log.warning("RotoWire fetch failed: %s", e)
        return []
```

**Step 4: Run tests**

Run: `uv run python -m pytest tests/test_lineups.py -v`
Expected: 6 passed

**Step 5: Commit**

```bash
git add lineups.py tests/test_lineups.py
git commit -m "feat: RotoWire lineup parser with confirmed/expected detection"
```

---

## Task 4: Game response parser

**Files:**
- Add to: `discord_bot.py` (new section after pick parser)
- Test: `tests/test_game_response.py`

The expert responds with loose natural language to game posts. The parser must map "Dodgers, over" back to the correct game.

**Step 1: Write tests**

```python
# tests/test_game_response.py
import pytest
from discord_bot import parse_game_response, PostedGame

GAMES = [
    PostedGame(
        game_key="LAD@SD",
        away_abbr="LAD", home_abbr="SD",
        away_name="Dodgers", home_name="Padres",
        away_pitcher="Ohtani", home_pitcher="Darvish",
        total=8.5, message_id=1001,
    ),
    PostedGame(
        game_key="DET@NYY",
        away_abbr="DET", home_abbr="NYY",
        away_name="Tigers", home_name="Yankees",
        away_pitcher="Skubal", home_pitcher="Cole",
        total=7.5, message_id=1002,
    ),
]


def test_team_name_pick():
    r = parse_game_response("Dodgers, over", GAMES)
    assert r is not None
    assert r.game_key == "LAD@SD"
    assert r.winner_pick == "LAD"
    assert r.total_pick == "over"


def test_abbreviation_pick():
    r = parse_game_response("LAD over", GAMES)
    assert r is not None
    assert r.game_key == "LAD@SD"
    assert r.winner_pick == "LAD"
    assert r.total_pick == "over"


def test_pitcher_name_pick():
    r = parse_game_response("Ohtani, under", GAMES)
    assert r is not None
    assert r.game_key == "LAD@SD"
    assert r.winner_pick == "LAD"  # pitcher's team
    assert r.total_pick == "under"


def test_just_team_no_total():
    r = parse_game_response("Yankees", GAMES)
    assert r is not None
    assert r.game_key == "DET@NYY"
    assert r.winner_pick == "NYY"
    assert r.total_pick is None


def test_just_over():
    # Ambiguous — can't determine which game. Should return None.
    r = parse_game_response("over", GAMES)
    assert r is None


def test_numbered_format():
    r = parse_game_response("1. Dodgers 2. Over", GAMES)
    assert r is not None
    assert r.game_key == "LAD@SD"
    assert r.winner_pick == "LAD"
    assert r.total_pick == "over"


def test_and_format():
    r = parse_game_response("padres and under", GAMES)
    assert r is not None
    assert r.game_key == "LAD@SD"
    assert r.winner_pick == "SD"
    assert r.total_pick == "under"


def test_case_insensitive():
    r = parse_game_response("TIGERS OVER", GAMES)
    assert r is not None
    assert r.game_key == "DET@NYY"
    assert r.winner_pick == "DET"
    assert r.total_pick == "over"


def test_no_match():
    r = parse_game_response("hello world", GAMES)
    assert r is None


def test_reply_context_game():
    """When replying to a specific game message, use that game's context."""
    r = parse_game_response("over", GAMES, reply_to_message_id=1001)
    assert r is not None
    assert r.game_key == "LAD@SD"
    assert r.total_pick == "over"
```

**Step 2: Implement in discord_bot.py**

Add PostedGame dataclass and parse_game_response function:

```python
@dataclass
class PostedGame:
    """A game that was posted to Discord for expert picks."""
    game_key: str        # "LAD@SD"
    away_abbr: str
    home_abbr: str
    away_name: str
    home_name: str
    away_pitcher: str
    home_pitcher: str
    total: Optional[float]
    message_id: int      # Discord message ID for reply matching


@dataclass
class GamePick:
    """Parsed expert response."""
    game_key: str
    winner_pick: Optional[str]   # team abbr or None
    total_pick: Optional[str]    # "over" | "under" | None


def parse_game_response(text: str, posted_games: list[PostedGame],
                        reply_to_message_id: Optional[int] = None) -> Optional[GamePick]:
    """
    Parse expert's free-form response and map to a posted game.
    Returns None if can't determine which game.
    """
    msg = text.lower().strip()
    # Remove numbering like "1." "2."
    msg = re.sub(r"\b\d+\.\s*", "", msg)

    # Detect over/under
    total_pick: Optional[str] = None
    if re.search(r"\bover\b", msg):
        total_pick = "over"
    elif re.search(r"\bunder\b", msg):
        total_pick = "under"

    # If replying to a specific game message, use that game
    target_game: Optional[PostedGame] = None
    if reply_to_message_id:
        for g in posted_games:
            if g.message_id == reply_to_message_id:
                target_game = g
                break

    # Try to match a team or pitcher
    matched_game: Optional[PostedGame] = None
    winner_abbr: Optional[str] = None

    for g in posted_games:
        candidates = [
            (g.away_name.lower(), g.away_abbr),
            (g.home_name.lower(), g.home_abbr),
            (g.away_abbr.lower(), g.away_abbr),
            (g.home_abbr.lower(), g.home_abbr),
        ]
        # Pitcher last names
        away_last = g.away_pitcher.split()[-1].lower() if g.away_pitcher else ""
        home_last = g.home_pitcher.split()[-1].lower() if g.home_pitcher else ""
        if away_last:
            candidates.append((away_last, g.away_abbr))
        if home_last:
            candidates.append((home_last, g.home_abbr))

        for name, abbr in candidates:
            if name and re.search(r"\b" + re.escape(name) + r"\b", msg):
                matched_game = g
                winner_abbr = abbr
                break
        if matched_game:
            break

    # Use reply context if no team match but we have a reply target
    if not matched_game and target_game:
        matched_game = target_game

    if not matched_game:
        return None

    # Must have at least one pick (winner or total)
    if winner_abbr is None and total_pick is None:
        return None

    return GamePick(
        game_key=matched_game.game_key,
        winner_pick=winner_abbr,
        total_pick=total_pick,
    )
```

**Step 3: Run tests**

Run: `uv run python -m pytest tests/test_game_response.py -v`
Expected: 10 passed

**Step 4: Commit**

```bash
git add discord_bot.py tests/test_game_response.py
git commit -m "feat: game response parser for expert baseball picks"
```

---

## Task 5: Quiet hours for Discord alerts

**Files:**
- Modify: `infra.py` (Discord._send method, ~line 953)

**Step 1: Add quiet hours check**

```python
# In Discord._send(), before the httpx.post:
from datetime import datetime, timezone, timedelta

_ET = timezone(timedelta(hours=-5))  # ET offset (close enough; DST doesn't matter for 11pm-8am)

def _in_quiet_hours(self) -> bool:
    """No alerts between 11pm and 8am ET."""
    hour = datetime.now(self._ET).hour
    return hour >= 23 or hour < 8
```

Modify `_send()`:
```python
def _send(self, message: str) -> None:
    if not self._enabled:
        log.info("Discord (disabled): %s", message)
        return
    if self._in_quiet_hours():
        log.info("Discord (quiet hours): %s", message[:200])
        return
    try:
        httpx.post(self._url, json={"content": message}, timeout=5.0)
    except Exception as e:
        log.warning("Discord send failed: %s", e)
```

**Step 2: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: all pass

**Step 3: Commit**

```bash
git add infra.py
git commit -m "feat: quiet hours — suppress Discord alerts 11pm-8am ET"
```

---

## Task 6: Daily stop-loss in infra.py

**Files:**
- Modify: `infra.py` (RiskEngine class, ~line 586)

This is a HARD safety rail — not in strategy.py, the loop cannot override it.

**Step 1: Add daily stop-loss to RiskEngine**

```python
DAILY_STOP_LOSS_PCT = 0.05  # 5% of bankroll — HARD CODED, not tunable

# In __init__:
self._daily_loss_cents = 0
self._daily_stop_active = False
self._daily_stop_reset_date: Optional[date] = None

def record_loss(self, cents: int) -> None:
    self._rolling_loss += cents
    self._daily_loss_cents += cents
    self._bankroll -= cents
    # Daily stop-loss check
    if self._bankroll > 0:
        daily_pct = self._daily_loss_cents / self._peak_bankroll
        if daily_pct >= self.DAILY_STOP_LOSS_PCT:
            self._daily_stop_active = True
            log.critical("DAILY STOP-LOSS: lost %d cents today (%.1f%%)",
                        self._daily_loss_cents, daily_pct * 100)
    # Existing kill switch check
    if self._peak_bankroll > 0:
        drawdown_pct = self._rolling_loss / self._peak_bankroll
        if drawdown_pct >= self.KILL_DRAWDOWN_PCT:
            self._kill_switch = True
            log.critical("KILL SWITCH ACTIVATED: %.1f%% drawdown", drawdown_pct * 100)

def daily_stop_active(self) -> bool:
    """Check if daily stop-loss is hit. Auto-resets at midnight ET."""
    if not self._daily_stop_active:
        return False
    today = datetime.now(timezone(timedelta(hours=-5))).date()
    if self._daily_stop_reset_date != today:
        self._daily_stop_active = False
        self._daily_loss_cents = 0
        self._daily_stop_reset_date = today
        return False
    return True
```

**Step 2: Wire into bot.py run loop** (check alongside kill_switch):

```python
if self._risk.daily_stop_active():
    log.info("Daily stop-loss active — no new trades until midnight ET")
    # Don't break the loop — just skip trading. Still check fills.
```

**Step 3: Run tests, commit**

```bash
git add infra.py bot.py
git commit -m "safety: add 5% daily stop-loss to RiskEngine (hard-coded, not tunable)"
```

---

## Task 7: Lineup detection background task

**Files:**
- Modify: `discord_bot.py` (replace `_post_tonights_games` with lineup-driven posting)

This is the main integration. The background task:
1. Polls RotoWire every 5 min
2. Tracks which games have been posted (by game_key)
3. When a game flips to confirmed, posts it as a separate Discord message
4. Continues polling for scratches after posting
5. Falls back to fixed-time posting if RotoWire is down 30+ min

**Key state (in-memory, survives within process):**
```python
_posted_games: dict[str, PostedGame] = {}     # game_key → PostedGame
_last_pitchers: dict[str, tuple[str,str]] = {}  # game_key → (away_pitcher, home_pitcher)
_rotowire_down_since: Optional[float] = None
_fallback_posted_today: bool = False
```

**Step 1: Implement the background task**

Replace `_post_tonights_games` and `_POSTED_DATES` with the new lineup detection loop. See full implementation in Task 7 code block below.

**Step 2: Wire expert response handling**

Modify `_handle_pick()` to check if the message is a reply to a posted game, and use `parse_game_response()` to parse it. If matched, react with checkmark and log with enriched fields.

**Step 3: Format each game as its own message**

```python
def _format_game_message(game: GameInfo) -> str:
    """Format one game as a phone-friendly Discord message."""
    away_p = game.away_pitcher or "TBD"
    home_p = game.home_pitcher or "TBD"
    away_hand = f" ({game.away_throws})" if game.away_throws else ""
    home_hand = f" ({game.home_throws})" if game.home_throws else ""

    header = f"⚾ {away_p}{away_hand} ({game.away_abbr}) vs {home_p}{home_hand} ({game.home_abbr}) — {game.game_time}"

    lines = [header]
    lines.append(f"1. {game.away_name} or {game.home_name}?")
    if game.total:
        lines.append(f"2. Over or under {game.total}?")

    return "\n".join(lines)
```

**Step 4: Run all tests, commit**

```bash
git add discord_bot.py
git commit -m "feat: lineup-driven game posting — polls RotoWire, posts on confirm"
```

---

## Task 8: Enriched picks.tsv logging

**Files:**
- Modify: `file_io.py` (update PICKS_COLS)
- Modify: `discord_bot.py` (_log_pick function)

**New PICKS_COLS:**
```python
PICKS_COLS = [
    "timestamp", "discord_user_id", "discord_username",
    "raw_message", "parsed_market_ticker",
    "market_type",          # winner/total
    "his_pick",             # "LAD" or "over"
    "starting_pitchers",    # "Ohtani vs Darvish"
    "time_before_first_pitch",  # minutes
    "lineup_confirmed_at",  # unix timestamp
    "pick_received_at",     # unix timestamp
    "scratch_after_pick",   # yes/no
    "parsed_sentiment", "confidence_assigned",
    "trade_triggered", "outcome", "pnl",
]
```

Note: This is a backwards-incompatible schema change. Old picks.tsv data (1 row) will not match. Archive it or accept the mismatch (read_picks uses zip, shorter rows just get fewer fields).

**Step 1: Update file_io.py, Step 2: Update _log_pick, Step 3: Run tests, Step 4: Commit**

```bash
git add file_io.py discord_bot.py
git commit -m "feat: enriched picks.tsv with pitcher/timing/scratch fields"
```

---

## Task 9: Scratch detection and postponement handling

**Files:**
- Modify: `discord_bot.py` (add to lineup poll loop)

In the lineup poll loop, after posting a game:
- Compare current pitcher names to `_last_pitchers[game_key]`
- If pitcher changed, post scratch alert and ask for updated pick
- If game card disappears or gains a special status, treat as postponement

**Scratch alert format:**
```
⚠️ SCRATCH: Ohtani OUT for LAD. Now starting Buehler (LAD) vs Darvish (SD)
Same pick or change?
```

**Postponement:**
```
⚠️ POSTPONED: LAD vs SD — orders cancelled
```

The actual order cancellation requires communication with the trading bot via a flat file (e.g., `cancel_orders.json`). The trading bot reads this on each tick and cancels matching orders.

**Step 1: Implement scratch detection in poll loop**
**Step 2: Add cancel_orders.json flat file contract**
**Step 3: Wire bot.py to read cancel_orders.json on each tick**
**Step 4: Test, commit**

```bash
git add discord_bot.py file_io.py bot.py
git commit -m "feat: scratch detection + postponement handling with order cancellation"
```

---

## Task 10: Settlement tracking

**Files:**
- Modify: `discord_bot.py` or `bot.py` (settlement already happens in bot.py via Kalshi API)

The trading bot already tracks fills and settlements. This task wires the outcome back to picks.tsv:
- When a market settles, find matching picks in picks.tsv and update outcome/pnl columns
- Post settlement to Discord alerts (already handled by `position_closed()` in infra.py Discord class)

This can be a periodic task in bot.py that scans settled markets and updates picks.tsv.

**Step 1: Add settlement update to bot.py flush cycle**
**Step 2: Test, commit**

```bash
git add bot.py
git commit -m "feat: settlement tracking — update picks.tsv outcomes on market settle"
```

---

## Task 11: Deploy and verify

**Steps:**
1. `uv sync` locally to verify deps
2. Run full test suite: `uv run python -m pytest tests/ -v`
3. rsync all changed files to Lightsail
4. `uv sync` on server
5. Restart all 3 services
6. Check journald logs for clean startup
7. Verify RotoWire polling is working (check logs for "fetched N games")
8. Wait for a lineup to confirm and verify Discord post appears

---

## Execution Order

Tasks 1-4 are independent foundations (can be parallelized).
Task 5-6 are independent safety features (can be parallelized).
Task 7 depends on Tasks 1, 3, 4.
Task 8 depends on Task 4.
Task 9 depends on Tasks 7, 8.
Task 10 depends on Task 8.
Task 11 depends on all above.

```
[1: deps] ──────────────────────┐
[2: strategy params] ───────────┤
[3: RotoWire parser] ───────────┼──→ [7: lineup bg task] ──→ [9: scratches] ──┐
[4: response parser] ───────────┤         ↓                                    │
[5: quiet hours] ───────────────┤    [8: picks.tsv] ──→ [10: settlement] ─────┼──→ [11: deploy]
[6: daily stop-loss] ───────────┘                                              │
                                                                               │
```
