"""
Wallet — AED / MVR balances and the fluctuating Naira exchange
rate.

Rate mechanics:
    A background tasks.loop (same pattern as flight.py's
    scan_flights) nudges each currency's rate by a small random
    percentage every EXCHANGE_TICK_INTERVAL_SECONDS, clamped
    inside its EXCHANGE_RATE_BOUNDS band so it can't spiral in
    either direction. Nobody — including admins — can set a rate
    directly: database.set_exchange_rate() is only ever called
    from this loop. !exchange always reads whatever the loop
    last calculated.

Commands:
    !wallet             -> show ₦/AED/MVR balances + current
                           rates (with a since-last-check arrow).
    !exchange <cur> <₦> -> convert Naira into AED or MVR at the
                           current rate.

The phone's Wallet menu (replacing the old Emergency placeholder)
lives in phone.py, same convention as HotelView/FlightView living
there instead of in hotel.py/flight.py — it calls these same two
commands via ctx.invoke() so the logic is never duplicated.
"""

import random

import discord
from discord.ext import commands, tasks

import database

from config import (
    CURRENCY_SYMBOL,
    EXCHANGE_RATE_BOUNDS,
    EXCHANGE_MAX_TICK_PERCENT,
    EXCHANGE_TICK_INTERVAL_SECONDS,
)


# ================================================================
# HELPERS
# ================================================================

def _arrow(rate_row) -> str:
    if rate_row["previous_rate"] is None or rate_row["rate"] == rate_row["previous_rate"]:
        return "\u2192"
    return "\u25b2" if rate_row["rate"] > rate_row["previous_rate"] else "\u25bc"


def _balance_field(currency: str) -> str:
    return "aed_balance" if currency == "aed" else "mvr_balance"


# ================================================================
# COG
# ================================================================

class WalletCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drift_rates.start()

    def cog_unload(self):
        self.drift_rates.cancel()

    # ------------------------------------------------------------
    # !wallet
    # ------------------------------------------------------------

    @commands.command(name="wallet")
    async def wallet(self, ctx: commands.Context):
        player = database.get_or_create_player(ctx.author.id)
        aed_row = database.get_exchange_rate("aed")
        mvr_row = database.get_exchange_rate("mvr")

        embed = discord.Embed(title="\U0001f4b1 Wallet", color=discord.Color.gold())
        embed.add_field(name="Naira", value=f"\u20a6{player['balance']:,}", inline=False)
        embed.add_field(name="AED", value=f"{player['aed_balance']:,} AED", inline=True)
        embed.add_field(name="MVR", value=f"{player['mvr_balance']:,} MVR", inline=True)
        embed.add_field(
            name="Exchange Rates",
            value=(
                f"1 AED = \u20a6{aed_row['rate']:,.0f} {_arrow(aed_row)}\n"
                f"1 MVR = \u20a6{mvr_row['rate']:,.0f} {_arrow(mvr_row)}"
            ),
            inline=False,
        )

        await ctx.send(embed=embed)

    # ------------------------------------------------------------
    # !exchange <aed|mvr> <naira amount>
    # ------------------------------------------------------------

    @commands.command(name="exchange")
    async def exchange(self, ctx: commands.Context, currency: str = None, amount: int = None):
        if currency is None or amount is None:
            await ctx.send("Usage: `!exchange <aed|mvr> <naira amount>`")
            return

        currency = currency.strip().lower()

        if currency not in ("aed", "mvr"):
            await ctx.send("\u26d4 Choose a currency: `aed` or `mvr`.")
            return

        if amount <= 0:
            await ctx.send("\u26d4 Enter a positive amount.")
            return

        player = database.get_or_create_player(ctx.author.id)

        if player["balance"] < amount:
            await ctx.send(f"\u26d4 You need \u20a6{amount:,}. You have \u20a6{player['balance']:,}.")
            return

        rate_row = database.get_exchange_rate(currency)
        rate = rate_row["rate"]
        received = round(amount / rate)

        if received <= 0:
            await ctx.send(f"\u26d4 That's not enough to buy even 1 {CURRENCY_SYMBOL[currency]}.")
            return

        field = _balance_field(currency)

        database.update_player(
            ctx.author.id,
            balance=player["balance"] - amount,
            **{field: player[field] + received},
        )

        await ctx.send(
            f"\U0001f4b1 Exchanged \u20a6{amount:,} \u2192 {received:,} {CURRENCY_SYMBOL[currency]} "
            f"at 1 {CURRENCY_SYMBOL[currency]} = \u20a6{rate:,.0f}."
        )

    # ------------------------------------------------------------
    # BACKGROUND LOOP — drift each rate within its band
    # ------------------------------------------------------------

    @tasks.loop(seconds=EXCHANGE_TICK_INTERVAL_SECONDS)
    async def drift_rates(self):
        for currency, (lo, hi) in EXCHANGE_RATE_BOUNDS.items():
            row = database.get_exchange_rate(currency)

            if row is None:
                continue

            pct = random.uniform(-EXCHANGE_MAX_TICK_PERCENT, EXCHANGE_MAX_TICK_PERCENT)
            new_rate = row["rate"] * (1 + pct)
            new_rate = max(lo, min(hi, new_rate))

            database.set_exchange_rate(currency, new_rate)

    @drift_rates.before_loop
    async def before_drift_rates(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(WalletCog(bot))
