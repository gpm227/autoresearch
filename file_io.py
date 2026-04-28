"""
file_io.py — Shared flat-file I/O with fcntl locking.
Used by both bot.py (writer) and discord_bot.py (reader).
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent

# ─── File paths ───────────────────────────────────────────────────────────────

BANKROLL_FILE        = DATA_DIR / "bankroll.json"
ACTIVE_MARKETS_FILE  = DATA_DIR / "active_markets.json"
RESULTS_TSV          = DATA_DIR / "results.tsv"
SKIPS_TSV            = DATA_DIR / "skips.tsv"
PICKS_TSV            = DATA_DIR / "picks.tsv"
CONTRIBUTOR_WEIGHTS_FILE = DATA_DIR / "contributor_weights.json"
CANCEL_ORDERS_FILE       = DATA_DIR / "cancel_orders.json"

# ─── TSV column schemas ───────────────────────────────────────────────────────

RESULTS_COLS = [
    "timestamp", "ticker", "side", "price_cents", "contracts",
    "expected_ev", "status", "fill_count", "pnl_cents", "order_id", "reason",
]

SKIPS_COLS = [
    "timestamp", "ticker", "rejection_stage", "details",
]

PICKS_COLS = [
    "timestamp", "discord_user_id", "discord_username",
    "raw_message", "parsed_market_ticker",
    "market_type",              # winner/total
    "his_pick",                 # "LAD" or "over"
    "starting_pitchers",        # "Ohtani vs Darvish"
    "time_before_first_pitch",  # minutes
    "lineup_confirmed_at",      # unix timestamp
    "pick_received_at",         # unix timestamp
    "scratch_after_pick",       # yes/no
    "parsed_sentiment", "confidence_assigned",
    "trade_triggered", "outcome", "pnl",
]

# ─── JSON helpers ─────────────────────────────────────────────────────────────

def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON with shared lock. Returns default on missing/corrupt file."""
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically with exclusive lock."""
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(data, f, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    tmp.replace(path)  # atomic rename

# ─── TSV helpers ──────────────────────────────────────────────────────────────

def read_tsv(path: Path) -> list[list[str]]:
    """Read all rows from TSV. Returns list of string lists."""
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                lines = f.readlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return [line.strip().split("\t") for line in lines if line.strip()]
    except FileNotFoundError:
        return []


def append_tsv(path: Path, row: list) -> None:
    """Append one row to TSV with exclusive lock."""
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write("\t".join(str(x) for x in row) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def ensure_tsv_header(path: Path, columns: list[str]) -> None:
    """Create TSV file with header row if it doesn't exist."""
    if not path.exists():
        with open(path, "w") as f:
            f.write("\t".join(columns) + "\n")


def read_results() -> list[dict]:
    rows = read_tsv(RESULTS_TSV)
    return [dict(zip(RESULTS_COLS, row)) for row in rows if len(row) >= len(RESULTS_COLS)]


def read_picks() -> list[dict]:
    rows = read_tsv(PICKS_TSV)
    return [dict(zip(PICKS_COLS, row)) for row in rows if len(row) >= len(PICKS_COLS)]
