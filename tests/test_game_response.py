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
    assert r.winner_pick == "LAD"
    assert r.total_pick == "under"


def test_just_team_no_total():
    r = parse_game_response("Yankees", GAMES)
    assert r is not None
    assert r.game_key == "DET@NYY"
    assert r.winner_pick == "NYY"
    assert r.total_pick is None


def test_just_over_ambiguous():
    # Can't determine which game — should return None
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
    """When replying to a specific game message, use that context."""
    r = parse_game_response("over", GAMES, reply_to_message_id=1001)
    assert r is not None
    assert r.game_key == "LAD@SD"
    assert r.total_pick == "over"


def test_pitcher_last_name():
    r = parse_game_response("Cole over", GAMES)
    assert r is not None
    assert r.game_key == "DET@NYY"
    assert r.winner_pick == "NYY"
    assert r.total_pick == "over"
