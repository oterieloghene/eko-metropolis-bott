"""
Player Stats — !stats + background passive decay.

Six bars, all 0-100 (see config.PLAYER_STAT_NAMES):

    Hunger, Thirst, Health, Hygiene, Breath, Happiness

!stats renders each as a block bar, same visual idea as a
progress bar, e.g.:

    \U0001F356 Hunger      \u2593\u2593\u2593\u2593\u2593\u2593\u2593\u2591\u2591\u2591  68%

This cog owns two things:

    1. !stats -- read-only display command.

    2. A background tasks.loop (same pattern as cogs/wallet.py's
       drift_rates) that ticks every registered player on an
       interval, passively draining hunger/thirst/hygiene/breath/
       happiness a little (config.STAT_DECAY_PER_TICK), and
       applying HEALTH FALLOUT: health is never drained directly
       by anything else in the game -- it only drops here, and
       only when hunger, thirst, or happiness is already sitting
       at 0.

Actual DISTANCE-based drain from walking lives in cogs/walk.py
(not written yet) and calls database.adjust_stats() directly --
this cog does not know about walking at all, on purpose, so the
two stay decoupled.

The COLLAPSE check itself (any of hunger/thirst/health/happiness
hitting 0 -> give the Unconscious role, freeze the player, notify
the channel) is intentionally NOT implemented in this file yet --
that lands with cogs/walk.py in the next stage, since collapsing
needs a channel to post in and a journey to freeze, neither of
which this background-only cog has. For now, this loop clamps at
STAT_MIN same as everything else and simply lets a stat sit at 0
without further action.
"""

import discord
from discord.ext import commands, tasks

import database

from config import (
    PLAYER_STAT_NAMES,
    COLLAPSE_STATS,
    STAT_MAX,
    STAT_DISPLAY,
    STAT_BAR_LENGTH,
    STAT_DECAY_TICK_SECONDS,
    STAT_DECAY_PER_TICK,
    HEALTH_LOSS_PER_ZEROED_STAT_PER_TICK,
    STAT_WARNING_THRESHOLD,
)


# ================================================================
# BAR RENDERING
# ================================================================

def _render_bar(value: float) -> str:

    value = max(0.0, min(STAT_MAX, value))

    filled = round((value / STAT_MAX) * STAT_BAR_LENGTH)
    filled = max(0, min(STAT_BAR_LENGTH, filled))

    bar = ("\u2593" * filled) + ("\u2591" * (STAT_BAR_LENGTH - filled))

    return f"{bar} {round(value)}%"


def _stat_color(stats: dict) -> discord.Color:

    """
    Pick an embed color based on the WORST collapse-relevant
    stat, so a glance at !stats' side bar tells you how urgent
    things are before you even read the numbers.
    """

    worst = min(stats[name] for name in COLLAPSE_STATS)

    if worst <= STAT_WARNING_THRESHOLD:
        return discord.Color.red()

    if worst <= 50:
        return discord.Color.orange()

    return discord.Color.green()


# ================================================================
# COG
# ================================================================

class StatsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.decay_stats.start()

    def cog_unload(self):
        self.decay_stats.cancel()

    # ------------------------------------------------------------
    # !stats
    # ------------------------------------------------------------

    @commands.command(name="stats")
    async def stats(self, ctx: commands.Context):

        stats = database.get_stats(ctx.author.id)

        embed = discord.Embed(
            title=f"\U0001F4CA {ctx.author.display_name}'s Stats",
            color=_stat_color(stats),
        )

        for stat_name in PLAYER_STAT_NAMES:

            display = STAT_DISPLAY[stat_name]

            embed.add_field(
                name=f"{display['emoji']} {display['label']}",
                value=_render_bar(stats[stat_name]),
                inline=False,
            )

        low_stats = [
            STAT_DISPLAY[name]["label"]
            for name in COLLAPSE_STATS
            if stats[name] <= STAT_WARNING_THRESHOLD
        ]

        if low_stats:
            embed.set_footer(
                text=f"\u26a0\ufe0f Getting critical: {', '.join(low_stats)}"
            )

        await ctx.send(embed=embed)

    # ------------------------------------------------------------
    # BACKGROUND LOOP -- passive decay + health fallout
    # ------------------------------------------------------------

    @tasks.loop(seconds=STAT_DECAY_TICK_SECONDS)
    async def decay_stats(self):

        for user_id in database.all_player_ids():

            stats = database.get_stats(user_id)

            deltas = dict(STAT_DECAY_PER_TICK)

            # ------------------------------------------------
            # HEALTH FALLOUT
            #
            # health is untouched by the passive table above --
            # it only reacts here, based on which of hunger/
            # thirst/happiness is already at (or below) 0 THIS
            # tick, before this tick's other decay is applied.
            # ------------------------------------------------

            zeroed = sum(
                1
                for name in ("hunger", "thirst", "happiness")
                if stats[name] <= 0
            )

            if zeroed:
                deltas["health"] = (
                    deltas.get("health", 0.0)
                    - (HEALTH_LOSS_PER_ZEROED_STAT_PER_TICK * zeroed)
                )

            database.adjust_stats(
                int(user_id),
                **deltas,
            )

    @decay_stats.before_loop
    async def before_decay_stats(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
