#!/usr/bin/env python3
"""
discord_bot.py — Minimal Discord notification shell for the Kalshi trading bot.

Provides:
  - Discord client setup / login / on_ready
  - post_daily_pnl()   — daily P&L summary embed
  - post_error()       — error alerts
  - post_info()        — generic info messages

All strategy-specific (MLB / Kirk pick) handlers have been removed.
Runs as its own systemd service so the trading loop and the notification
channel have independent lifetimes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional

import discord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("discord_bot")


# ─── Config ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_STATUS_CHANNEL_ID", "0") or "0")

if not DISCORD_TOKEN:
    log.warning("DISCORD_BOT_TOKEN not set — bot will fail to connect")
if not DISCORD_CHANNEL_ID:
    log.warning("DISCORD_STATUS_CHANNEL_ID not set — posts will have no destination")


# ─── Client ───────────────────────────────────────────────────────────────────

_intents = discord.Intents.default()
_intents.message_content = False  # no command handling — notifications only
bot = discord.Client(intents=_intents)


async def _channel() -> Optional[discord.abc.Messageable]:
    ch = bot.get_channel(DISCORD_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(DISCORD_CHANNEL_ID)
        except Exception as e:
            log.error("Cannot fetch channel %s: %s", DISCORD_CHANNEL_ID, e)
            return None
    return ch


# ─── Notification helpers ─────────────────────────────────────────────────────

async def post_info(message: str) -> None:
    """Post a plain-text informational message."""
    ch = await _channel()
    if ch is None:
        return
    try:
        await ch.send(message)
    except Exception as e:
        log.error("post_info failed: %s", e)


async def post_error(message: str) -> None:
    """Post an error alert as a red embed."""
    ch = await _channel()
    if ch is None:
        return
    try:
        embed = discord.Embed(
            title="⚠️ Bot error",
            description=str(message)[:1900],
            color=0xE74C3C,
        )
        await ch.send(embed=embed)
    except Exception as e:
        log.error("post_error failed: %s", e)


async def post_daily_pnl(
    *,
    date: str,
    trades: int,
    wins: int,
    losses: int,
    net_pnl_dollars: float,
    bankroll_dollars: float,
) -> None:
    """
    Post a daily P&L summary embed.

    Call this once per day (e.g. 8am ET) from whatever process owns the
    accounting. Takes pre-computed values — this module does no math.
    """
    ch = await _channel()
    if ch is None:
        return

    color = 0x2ECC71 if net_pnl_dollars >= 0 else 0xE74C3C
    win_rate = (wins / trades * 100) if trades > 0 else 0.0

    embed = discord.Embed(
        title=f"Daily P&L — {date}",
        color=color,
    )
    embed.add_field(name="Trades", value=str(trades), inline=True)
    embed.add_field(name="Record", value=f"{wins}W / {losses}L", inline=True)
    embed.add_field(name="Win rate", value=f"{win_rate:.0f}%", inline=True)
    embed.add_field(name="Net P&L", value=f"${net_pnl_dollars:+.2f}", inline=True)
    embed.add_field(name="Bankroll", value=f"${bankroll_dollars:.2f}", inline=True)

    try:
        await ch.send(embed=embed)
    except Exception as e:
        log.error("post_daily_pnl failed: %s", e)


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
    ch = await _channel()
    if ch is not None:
        log.info("Notification channel resolved: #%s", getattr(ch, "name", DISCORD_CHANNEL_ID))


def main() -> None:
    if not DISCORD_TOKEN:
        log.error("DISCORD_BOT_TOKEN env var is required")
        sys.exit(1)
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
