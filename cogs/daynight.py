"""
Day/Night — real-world West Africa Time (WAT, UTC+1, no DST)
day/night cycle, same shape as cogs/weather.py.

This does NOT run on an arbitrary in-game clock -- it reads actual
real-world time every check and derives "day" or "night" directly
from it. Night is 6 PM - 6 AM WAT; everything outside that window
is day.

A background tasks.loop polls every DAYNIGHT_CHECK_INTERVAL_SECONDS
and, ONLY when the period actually flips (day -> night or vice
versa), announces it in #server-announcements, same channel/role
pattern as cogs/weather.py.

Three other files read the current period through the accessor
functions below, and COMPOUND it with cogs/weather.py's own
multiplier rather than replacing it (a rainy night is worse than
either alone):

    cogs/travel.py  (_travel_route)    -> get_movement_multiplier()
    cogs/bus.py     (_run_bus)         -> get_movement_multiplier()
    cogs/walk.py    (_apply_hop_decay) -> get_stat_drain_multiplier()
    cogs/stats.py   (decay_stats)      -> get_passive_stat_multiplier()

This cog has no idea travel, bus, walk, or stats exist, on
purpose -- same decoupling principle as cogs/weather.py.
"""

from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks


# ================================================================
# CONFIG
# ================================================================

DAYNIGHT_CHECK_INTERVAL_SECONDS = 60

# West Africa Time -- fixed UTC+1 year-round, Nigeria does not
# observe DST, so a plain fixed offset is correct (no zoneinfo/
# tzdata dependency needed).
WAT = timezone(timedelta(hours=1))

NIGHT_START_HOUR = 18  # 6 PM WAT
NIGHT_END_HOUR = 6     # 6 AM WAT

ANNOUNCEMENTS_CHANNEL = "server-announcements"
DAYNIGHT_PING_ROLE = "Lagosians"

NIGHTFALL_MESSAGE = "🌙 Night has fallen over Eko — streets are darker and slower going."
SUNRISE_MESSAGE = "🌅 The sun rises over Eko."

# Extra caution driving in the dark, on top of whatever weather
# is already doing.
NIGHT_MOVEMENT_MULTIPLIER = 1.15

# Extra fatigue from being out walking after dark, on top of
# whatever weather is already doing.
NIGHT_STAT_DRAIN_MULTIPLIER = 1.15

# Passive (background, non-walking) stat effect at night -- kept
# gentle, same philosophy as weather's passive multipliers.
NIGHT_PASSIVE_STAT_MULTIPLIERS = {
    "happiness": 1.1,
    "hygiene": 1.05,
}


# ================================================================
# CURRENT PERIOD
# ================================================================

def _now_wat() -> datetime:
    return datetime.now(timezone.utc).astimezone(WAT)


def get_current_period() -> str:
    hour = _now_wat().hour

    if hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR:
        return "night"

    return "day"


# ================================================================
# ACCESSORS — read by travel.py / bus.py / walk.py / stats.py
# ================================================================

def get_movement_multiplier() -> float:
    if get_current_period() == "night":
        return NIGHT_MOVEMENT_MULTIPLIER

    return 1.0


def get_stat_drain_multiplier() -> float:
    if get_current_period() == "night":
        return NIGHT_STAT_DRAIN_MULTIPLIER

    return 1.0


def get_passive_stat_multiplier(stat_name: str) -> float:
    if get_current_period() == "night":
        return NIGHT_PASSIVE_STAT_MULTIPLIERS.get(stat_name, 1.0)

    return 1.0


# ================================================================
# COG
# ================================================================

class DayNightCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_period = get_current_period()
        self.check_period.start()

    def cog_unload(self):
        self.check_period.cancel()

    @tasks.loop(seconds=DAYNIGHT_CHECK_INTERVAL_SECONDS)
    async def check_period(self):
        current = get_current_period()

        if current == self._last_period:
            return

        self._last_period = current

        message = NIGHTFALL_MESSAGE if current == "night" else SUNRISE_MESSAGE

        for guild in self.bot.guilds:

            channel = discord.utils.get(
                guild.text_channels,
                name=ANNOUNCEMENTS_CHANNEL
            )

            if channel is None:
                continue

            role = discord.utils.get(
                guild.roles,
                name=DAYNIGHT_PING_ROLE
            )

            ping = role.mention if role else f"@{DAYNIGHT_PING_ROLE}"

            await channel.send(
                f"{ping} {message}"
            )

    @check_period.before_loop
    async def before_check_period(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DayNightCog(bot))
