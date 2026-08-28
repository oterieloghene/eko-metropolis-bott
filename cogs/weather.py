"""
Weather — server-wide condition that shifts on a timer and affects
travel speed and stat drain.

This is a background-only cog, same shape as cogs/wallet.py's rate
drift and cogs/stats.py's passive decay loop:

    A tasks.loop picks a new weather condition every
    WEATHER_CHANGE_INTERVAL_SECONDS, stores it in memory (module-
    level CURRENT_WEATHER, same "just live in memory" approach as
    the rest of this bot's transient state), and announces the
    change in the #server-announcements channel, tagging the
    @Lagosians role.

Two other files read CURRENT_WEATHER through the accessor
functions below:

    cogs/travel.py  (_travel_route)  -> get_movement_multiplier()
    cogs/bus.py     (_run_bus)       -> get_movement_multiplier()
    cogs/walk.py    (_apply_hop_decay) -> get_stat_drain_multiplier()

Each just multiplies its own timing/decay number by whatever this
cog currently reports -- this cog has no idea travel, bus, or walk
exist, on purpose, same decoupling principle as cogs/stats.py.
"""

import random

import discord
from discord.ext import commands, tasks


# ================================================================
# CONFIG
# ================================================================

WEATHER_CHANGE_INTERVAL_SECONDS = 20 * 60

ANNOUNCEMENTS_CHANNEL = "server-announcements"
WEATHER_PING_ROLE = "Lagosians"

# name -> (announcement text, movement_multiplier, stat_drain_multiplier)
#
# movement_multiplier scales segment travel time (>1 = slower).
# stat_drain_multiplier scales walking's per-km stat decay
# (>1 = drains faster). Neither multiplier touches anything when
# it's exactly 1.0 -- that's "clear" weather, the common case.
WEATHER_TYPES = {
    "clear": {
        "message": "☀️ Skies over Eko are clear.",
        "movement_multiplier": 1.0,
        "stat_drain_multiplier": 1.0,
    },
    "cloudy": {
        "message": "☁️ It's overcast across the city.",
        "movement_multiplier": 1.0,
        "stat_drain_multiplier": 1.0,
    },
    "rain": {
        "message": "🌧️ Rain is falling over Eko — roads are slower going.",
        "movement_multiplier": 1.25,
        "stat_drain_multiplier": 1.1,
    },
    "storm": {
        "message": "⛈️ A storm has rolled in — expect serious delays.",
        "movement_multiplier": 1.6,
        "stat_drain_multiplier": 1.2,
    },
    "heatwave": {
        "message": "🌡️ A heatwave has hit Eko — stay hydrated out there.",
        "movement_multiplier": 1.1,
        "stat_drain_multiplier": 1.35,
    },
    "harmattan": {
        "message": "🌫️ Harmattan haze is rolling through, cutting visibility.",
        "movement_multiplier": 1.15,
        "stat_drain_multiplier": 1.1,
    },
}

# Module-level, in-memory current condition (mirrors the "no DB
# needed for transient global state" approach used elsewhere,
# e.g. cogs/wallet.py's rates). Starts clear on every fresh boot.
CURRENT_WEATHER = "clear"


# ================================================================
# ACCESSORS — read by travel.py / bus.py / walk.py
# ================================================================

def get_movement_multiplier() -> float:
    return WEATHER_TYPES[CURRENT_WEATHER]["movement_multiplier"]


def get_stat_drain_multiplier() -> float:
    return WEATHER_TYPES[CURRENT_WEATHER]["stat_drain_multiplier"]


def get_current_weather() -> str:
    return CURRENT_WEATHER


# ================================================================
# COG
# ================================================================

class WeatherCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.change_weather.start()

    def cog_unload(self):
        self.change_weather.cancel()

    @tasks.loop(seconds=WEATHER_CHANGE_INTERVAL_SECONDS)
    async def change_weather(self):
        global CURRENT_WEATHER

        CURRENT_WEATHER = random.choice(list(WEATHER_TYPES.keys()))
        weather_info = WEATHER_TYPES[CURRENT_WEATHER]

        for guild in self.bot.guilds:

            channel = discord.utils.get(
                guild.text_channels,
                name=ANNOUNCEMENTS_CHANNEL
            )

            if channel is None:
                continue

            role = discord.utils.get(
                guild.roles,
                name=WEATHER_PING_ROLE
            )

            ping = role.mention if role else f"@{WEATHER_PING_ROLE}"

            await channel.send(
                f"{ping} {weather_info['message']}"
            )

    @change_weather.before_loop
    async def before_change_weather(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(WeatherCog(bot))
