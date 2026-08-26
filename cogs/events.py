"""
Events — Dubai Desert, Dubai Marina, Blue Lagoon, Ocean Excursion.

Each event area has exactly two activities (see config.AREA_EVENTS):

    !compete <event>  -> pay the entry fee (local currency) to
                          join a pool. A registration window
                          opens on the first entrant; when it
                          closes, every entrant is rolled against
                          the event's metric and the best result
                          takes the whole pool. Fewer than 2
                          entrants when the window closes -> the
                          round is cancelled and everyone is
                          refunded.

    !try <event>      -> free, instant flavor outcome, no payout.

Both commands only work inside the matching area's thread
(checks.require_area("event")), same rule shops.py follows for
!mall/!fastfood/!spa. The trailing <event> name is optional (each
area currently has one of each) but if given must match the
area's own event, so this doesn't silently accept a typo'd name
from a different area.

Pool resolution runs on a background tasks.loop, same pattern as
flight.py's scan_flights / hotel.py's scan_room_service.
"""

import random
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import checks
import database

from config import (
    AREAS,
    AREA_EVENTS,
    COUNTRY_CURRENCY,
    CURRENCY_SYMBOL,
    EVENT_REGISTRATION_WINDOW_SECONDS,
    EVENT_SCAN_INTERVAL_SECONDS,
)


# ================================================================
# HELPERS
# ================================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _currency_for_area(area_code: str) -> str:
    return COUNTRY_CURRENCY[AREAS[area_code]["country"]]


def _balance_field(currency: str) -> str:
    return "aed_balance" if currency == "aed" else "mvr_balance"


def _fmt_money(amount, currency: str) -> str:
    return f"{amount:,} {CURRENCY_SYMBOL[currency]}"


def _compete_event_for_area(area_code: str) -> dict | None:
    return AREA_EVENTS.get(area_code, {}).get("compete")


def _try_event_for_area(area_code: str) -> dict | None:
    return AREA_EVENTS.get(area_code, {}).get("try")


def _event_name_matches(event_cfg: dict, given: str) -> bool:
    given = given.strip().lower()
    return given in (event_cfg["name"].lower(), event_cfg["code"].lower())


async def _charge(user_id: int, currency: str, amount: int) -> tuple[bool, int]:
    player = database.get_or_create_player(user_id)
    field = _balance_field(currency)
    balance = player[field]

    if balance < amount:
        return False, balance

    database.update_player(user_id, **{field: balance - amount})
    return True, balance - amount


def _refund(user_id: int, currency: str, amount: int) -> None:
    player = database.get_or_create_player(user_id)
    field = _balance_field(currency)
    database.update_player(user_id, **{field: player[field] + amount})


# ================================================================
# COG
# ================================================================

class EventsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scan_pools.start()

    def cog_unload(self):
        self.scan_pools.cancel()

    # ------------------------------------------------------------
    # !compete <event>
    # ------------------------------------------------------------

    @commands.command(name="compete")
    @checks.require_area("event")
    async def compete(self, ctx: commands.Context, *, event: str = None):
        area_code = ctx.area["area_code"]
        event_cfg = _compete_event_for_area(area_code)

        if event_cfg is None:
            await ctx.send("\u26d4 There's no competitive event here.")
            return

        if event is not None and not _event_name_matches(event_cfg, event):
            await ctx.send(
                f"\u26d4 The event here is **{event_cfg['name']}** \u2014 "
                f"try `!compete {event_cfg['name']}` or just `!compete`."
            )
            return

        currency = _currency_for_area(area_code)
        entry_fee = event_cfg["entry_fee"]

        pool = database.get_open_pool(area_code, event_cfg["code"])

        if pool is not None and database.get_event_entry(pool["pool_id"], ctx.author.id) is not None:
            await ctx.send("\u26d4 You've already entered this round.")
            return

        ok, balance = await _charge(ctx.author.id, currency, entry_fee)

        if not ok:
            await ctx.send(
                f"\u26d4 You need {_fmt_money(entry_fee, currency)} to enter "
                f"**{event_cfg['name']}**. You have {_fmt_money(balance, currency)}."
            )
            return

        if pool is None:
            closes_at = _now() + timedelta(seconds=EVENT_REGISTRATION_WINDOW_SECONDS)
            pool_id = database.create_event_pool(
                area_code, event_cfg["code"], currency, entry_fee, _iso(closes_at)
            )
            database.add_event_entry(pool_id, ctx.author.id)

            await ctx.send(
                f"\U0001f3c1 **{event_cfg['name']}** pool opened! Entry: "
                f"{_fmt_money(entry_fee, currency)}.\n"
                f"{ctx.author.mention} has joined. Registration closes in "
                f"{EVENT_REGISTRATION_WINDOW_SECONDS}s \u2014 others can `!compete` to join."
            )
            return

        database.add_event_entry(pool["pool_id"], ctx.author.id)
        entries = database.get_pool_entries(pool["pool_id"])
        remaining = max(0, int((_parse(pool["closes_at"]) - _now()).total_seconds()))

        await ctx.send(
            f"\U0001f3c1 {ctx.author.mention} joined **{event_cfg['name']}**! "
            f"Entrants: {len(entries)}. Closes in ~{remaining}s."
        )

    # ------------------------------------------------------------
    # !try <event>
    # ------------------------------------------------------------

    @commands.command(name="try")
    @checks.require_area("event")
    async def try_activity(self, ctx: commands.Context, *, event: str = None):
        area_code = ctx.area["area_code"]
        event_cfg = _try_event_for_area(area_code)

        if event_cfg is None:
            await ctx.send("\u26d4 There's nothing free to try here.")
            return

        if event is not None and not _event_name_matches(event_cfg, event):
            await ctx.send(
                f"\u26d4 The free activity here is **{event_cfg['name']}** \u2014 "
                f"try `!try {event_cfg['name']}` or just `!try`."
            )
            return

        await ctx.send(random.choice(event_cfg["flavors"]))

    # ------------------------------------------------------------
    # BACKGROUND SCAN — resolve pools whose window has closed
    # ------------------------------------------------------------

    @tasks.loop(seconds=EVENT_SCAN_INTERVAL_SECONDS)
    async def scan_pools(self):
        now_iso = _iso(_now())

        for pool in database.pools_due(now_iso):
            # Claim it immediately so an overlapping scan pass
            # (or a slow resolve) can't double-process it.
            database.set_pool_status(pool["pool_id"], "resolving")
            await self._resolve_pool(pool)

    @scan_pools.before_loop
    async def before_scan_pools(self):
        await self.bot.wait_until_ready()

    async def _get_area_thread(self, area_code: str):
        row = database.get_area(area_code)

        if row is None or not row["thread_id"]:
            return None

        thread_id = int(row["thread_id"])

        for guild in self.bot.guilds:
            thread = guild.get_thread(thread_id)
            if thread is not None:
                return thread

        for guild in self.bot.guilds:
            try:
                return await guild.fetch_channel(thread_id)
            except discord.HTTPException:
                continue

        return None

    def _find_event_cfg(self, area_code: str, event_code: str) -> dict | None:
        for cfg in AREA_EVENTS.get(area_code, {}).values():
            if cfg.get("code") == event_code:
                return cfg
        return None

    async def _resolve_pool(self, pool):
        entries = database.get_pool_entries(pool["pool_id"])
        area_code = pool["area_code"]
        currency = pool["currency"]
        event_cfg = self._find_event_cfg(area_code, pool["event_code"])
        event_name = event_cfg["name"] if event_cfg else pool["event_code"]
        thread = await self._get_area_thread(area_code)

        # ---- fewer than 2 entrants: cancel + refund ----
        if len(entries) < 2:
            for entry in entries:
                _refund(int(entry["user_id"]), currency, pool["entry_fee"])

            database.delete_pool(pool["pool_id"])

            if thread is not None:
                try:
                    await thread.send(
                        f"\u26a0\ufe0f Not enough entrants for **{event_name}** \u2014 "
                        f"round cancelled, entry fees refunded."
                    )
                except discord.HTTPException:
                    pass

            return

        # ---- roll results ----
        if event_cfg and event_cfg.get("fishing"):
            rolled = []
            for entry in entries:
                species = random.choice(event_cfg["species"])
                weight = round(random.uniform(species["min_weight"], species["max_weight"]), 1)
                rolled.append((int(entry["user_id"]), weight, species["name"]))

            rolled.sort(key=lambda r: r[1], reverse=True)
            winner_id, winner_weight, winner_species = rolled[0]

            result_desc = "\n".join(
                f"\u2022 <@{uid}> reeled in a {weight:.1f}kg {species}"
                for uid, weight, species in rolled
            )
            winner_line = (
                f"\U0001f3c6 <@{winner_id}> wins with a {winner_weight:.1f}kg {winner_species}!"
            )

        else:
            higher_wins = event_cfg["higher_wins"] if event_cfg else True
            lo = event_cfg["min"] if event_cfg else 0
            hi = event_cfg["max"] if event_cfg else 100
            metric_name = event_cfg.get("metric_name", "result") if event_cfg else "result"
            unit = event_cfg.get("unit", "") if event_cfg else ""

            rolled = [(int(entry["user_id"]), round(random.uniform(lo, hi), 1)) for entry in entries]
            rolled.sort(key=lambda r: r[1], reverse=higher_wins)
            winner_id, winner_value = rolled[0]

            result_desc = "\n".join(
                f"\u2022 <@{uid}> \u2014 {metric_name}: {value}{unit}"
                for uid, value in rolled
            )
            winner_line = f"\U0001f3c6 <@{winner_id}> wins with {metric_name} {winner_value}{unit}!"

        pot = pool["entry_fee"] * len(entries)
        _refund(winner_id, currency, pot)  # winner takes the whole pool

        database.delete_pool(pool["pool_id"])

        if thread is not None:
            try:
                await thread.send(
                    f"\U0001f3c1 **{event_name}** results:\n{result_desc}\n\n"
                    f"{winner_line} Takes the pool: {_fmt_money(pot, currency)}!"
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))
