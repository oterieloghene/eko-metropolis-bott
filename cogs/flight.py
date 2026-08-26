"""
Flights — Dubai and Maldives.

Flow:
    1. !bookflight <destination> <stay_minutes>
       Pays the round-trip price up front. No role/channel
       change happens yet — this only reserves a seat and
       sets a departure deadline.

    2. !checkin
       Must be typed in #travel-agency, and the player's
       database location must actually be "agency" (see
       checks.require_location). Must happen before the
       departure deadline (or the one-time reschedule
       deadline). Grants the "On vacation" role and removes
       write access everywhere — the player is "in the air".

    3. After FLIGHT_DURATION_SECONDS, the player automatically
       arrives: write access opens in the destination channel
       only.

    4. After stay_seconds, the player automatically returns:
       "On vacation" role is removed, write access moves back
       to #travel-agency, database location is set to "agency".

Missed check-in:
    - 1st miss  -> departure deadline is pushed back by
                   FLIGHT_RESCHEDULE_WINDOW_SECONDS. One time
                   only.
    - 2nd miss  -> ticket is forfeited. No refund, no further
                   reschedule.

booking_helper() below is a standalone function (not tied to
ctx) so a future phone UI can call the exact same logic that
!bookflight uses.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import checks
import database
import permissions

from config import (
    LOCATIONS,
    FLIGHT_DESTINATIONS,
    FLIGHT_VACATION_ROLE,
    FLIGHT_AGENCY_LOCATION,
    FLIGHT_CHECKIN_WINDOW_SECONDS,
    FLIGHT_RESCHEDULE_WINDOW_SECONDS,
    FLIGHT_DURATION_SECONDS,
    FLIGHT_MIN_STAY_SECONDS,
    FLIGHT_MAX_STAY_SECONDS,
    FLIGHT_SCAN_INTERVAL_SECONDS,
    FLIGHT_RETURN_REMINDER_SECONDS,
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


def _dest_name(code: str) -> str:
    loc = LOCATIONS.get(code)
    return loc["name"] if loc else code.title()


def _fmt_seconds(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)

    if minutes and secs:
        return f"{minutes}m {secs}s"

    if minutes:
        return f"{minutes}m"

    return f"{secs}s"


# ================================================================
# BOOKING (shared logic — usable by !bookflight and, later, phone)
# ================================================================

def book_flight_for(
    user_id: int,
    destination: str,
    stay_minutes: int
) -> tuple[bool, str]:
    """
    Attempt to book a flight.

    Returns (success, message).
    """

    destination = destination.strip().lower()

    if destination not in FLIGHT_DESTINATIONS:

        available = ", ".join(
            _dest_name(code) for code in FLIGHT_DESTINATIONS
        )

        return False, (
            f"⛔ `{destination}` is not a flight destination. "
            f"Available: {available}."
        )

    existing = database.get_flight(user_id)

    if existing is not None:

        return False, (
            "⛔ You already have an active flight booking. "
            "Use `!flightstatus` to check it."
        )

    stay_seconds = stay_minutes * 60

    if not (
        FLIGHT_MIN_STAY_SECONDS
        <= stay_seconds
        <= FLIGHT_MAX_STAY_SECONDS
    ):

        return False, (
            f"⛔ Stay length must be between "
            f"{FLIGHT_MIN_STAY_SECONDS // 60} and "
            f"{FLIGHT_MAX_STAY_SECONDS // 60} minutes "
            f"(test-run scale)."
        )

    player = database.get_or_create_player(user_id)

    price_one_way = FLIGHT_DESTINATIONS[destination]["price_one_way"]
    round_trip_price = price_one_way * 2

    if player["balance"] < round_trip_price:

        return False, (
            f"⛔ You need ₦{round_trip_price:,} for a round-trip "
            f"ticket to {_dest_name(destination)}. "
            f"You have ₦{player['balance']:,}."
        )

    departure_at = _now() + timedelta(
        seconds=FLIGHT_CHECKIN_WINDOW_SECONDS
    )

    database.update_player(
        user_id,
        balance=player["balance"] - round_trip_price
    )

    database.book_flight(
        user_id,
        destination=destination,
        price_paid=round_trip_price,
        stay_seconds=stay_seconds,
        departure_at=_iso(departure_at)
    )

    return True, (
        f"✈️ Flight booked to **{_dest_name(destination)}**!\n"
        f"💵 Paid: ₦{round_trip_price:,} (round trip)\n"
        f"🕒 Check in at {LOCATIONS[FLIGHT_AGENCY_LOCATION]['name']} "
        f"(`!checkin`) before "
        f"**{departure_at.strftime('%H:%M:%S UTC')}**\n"
        f"🏖️ Vacation length: {_fmt_seconds(stay_seconds)}\n\n"
        f"⚠️ Miss check-in and you get one reschedule. "
        f"Miss it a second time and the ticket is forfeited — "
        f"no refund."
    )


# ================================================================
# COG
# ================================================================

class FlightCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scan_flights.start()

    def cog_unload(self):
        self.scan_flights.cancel()

    # ------------------------------------------------------------
    # !bookflight
    # ------------------------------------------------------------

    @commands.command(name="bookflight")
    async def bookflight(
        self,
        ctx: commands.Context,
        destination: str = None,
        stay_minutes: int = None
    ):

        if destination is None or stay_minutes is None:

            available = ", ".join(FLIGHT_DESTINATIONS.keys())

            await ctx.send(
                f"Usage: `!bookflight <destination> <stay_minutes>`\n"
                f"Destinations: {available}\n"
                f"Stay length: {FLIGHT_MIN_STAY_SECONDS // 60}"
                f"-{FLIGHT_MAX_STAY_SECONDS // 60} minutes "
                f"(test-run scale)."
            )

            return

        ok, message = book_flight_for(
            ctx.author.id,
            destination,
            stay_minutes
        )

        await ctx.send(message)

    # ------------------------------------------------------------
    # !checkin  (must be at the travel agency)
    # ------------------------------------------------------------

    @commands.command(name="checkin")
    @checks.require_location(FLIGHT_AGENCY_LOCATION)
    async def checkin(self, ctx: commands.Context):

        flight = database.get_flight(ctx.author.id)

        if flight is None:

            await ctx.send(
                "⛔ You don't have a flight booked. "
                "Use `!bookflight` first."
            )

            return

        if flight["status"] != "booked":

            await ctx.send(
                "⛔ You've already checked in for your flight."
            )

            return

        deadline = _parse(flight["departure_at"])

        if _now() > deadline:

            # The scan loop handles missed check-ins, but guard
            # here too in case the player types !checkin right
            # as the deadline is being processed.
            await ctx.send(
                "⛔ You missed your check-in window for this "
                "flight. Hang tight — it may be rescheduled, "
                "or forfeited if this was your second miss."
            )

            return

        now = _now()
        arrival_at = now + timedelta(seconds=FLIGHT_DURATION_SECONDS)
        return_at = arrival_at + timedelta(seconds=flight["stay_seconds"])

        database.update_flight(
            ctx.author.id,
            status="in_transit",
            arrival_at=_iso(arrival_at),
            return_at=_iso(return_at)
        )

        # Grant the vacation role.
        role = discord.utils.get(
            ctx.guild.roles,
            name=FLIGHT_VACATION_ROLE
        )

        if role is not None:

            try:
                await ctx.author.add_roles(
                    role,
                    reason="Checked in for a flight"
                )

            except discord.Forbidden:
                pass

        # Player is now "in the air" — no write access anywhere
        # until they land.
        await permissions.set_write_access(
            ctx.guild,
            ctx.author,
            FLIGHT_AGENCY_LOCATION,
            allowed=False
        )

        await ctx.send(
            f"🛫 You've checked in and departed for "
            f"**{_dest_name(flight['destination'])}**. "
            f"You'll land in about "
            f"{_fmt_seconds(FLIGHT_DURATION_SECONDS)}."
        )

    # ------------------------------------------------------------
    # !flightstatus
    # ------------------------------------------------------------

    @commands.command(name="flightstatus")
    async def flightstatus(self, ctx: commands.Context):

        flight = database.get_flight(ctx.author.id)

        if flight is None:

            await ctx.send("You have no active flight.")
            return

        name = _dest_name(flight["destination"])

        if flight["status"] == "booked":

            deadline = _parse(flight["departure_at"])
            remaining = (deadline - _now()).total_seconds()

            await ctx.send(
                f"✈️ Booked to **{name}**. "
                f"Check in at "
                f"{LOCATIONS[FLIGHT_AGENCY_LOCATION]['name']} "
                f"within {_fmt_seconds(max(0, remaining))} "
                f"(missed so far: {flight['missed_count']})."
            )

        elif flight["status"] == "in_transit":

            arrival = _parse(flight["arrival_at"])
            remaining = (arrival - _now()).total_seconds()

            if remaining > 0:

                await ctx.send(
                    f"🛫 In the air to **{name}** — "
                    f"landing in {_fmt_seconds(max(0, remaining))}."
                )

            else:

                await ctx.send(
                    f"🏖️ On vacation in **{name}**."
                )

        elif flight["status"] == "on_vacation":

            return_at = _parse(flight["return_at"])
            remaining = (return_at - _now()).total_seconds()

            await ctx.send(
                f"🏖️ On vacation in **{name}** — "
                f"returning in {_fmt_seconds(max(0, remaining))}."
            )

    # ------------------------------------------------------------
    # BACKGROUND SCAN — missed check-ins, arrivals, returns
    # ------------------------------------------------------------

    @tasks.loop(seconds=FLIGHT_SCAN_INTERVAL_SECONDS)
    async def scan_flights(self):

        now = _now()

        for guild in self.bot.guilds:
            await self._scan_missed_checkins(guild, now)
            await self._scan_arrivals(guild, now)
            await self._scan_return_reminders(guild, now)
            await self._scan_returns(guild, now)

    @scan_flights.before_loop
    async def before_scan_flights(self):
        await self.bot.wait_until_ready()

    async def _scan_missed_checkins(self, guild, now):

        for flight in database.flights_by_status("booked"):

            deadline = _parse(flight["departure_at"])

            if now <= deadline:
                continue

            user_id = int(flight["user_id"])
            member = guild.get_member(user_id)

            if flight["missed_count"] < 1:

                new_deadline = now + timedelta(
                    seconds=FLIGHT_RESCHEDULE_WINDOW_SECONDS
                )

                database.update_flight(
                    user_id,
                    missed_count=flight["missed_count"] + 1,
                    departure_at=_iso(new_deadline)
                )

                if member:

                    await self._notify(
                        member,
                        f"⚠️ You missed your check-in for your "
                        f"flight to "
                        f"{_dest_name(flight['destination'])}. "
                        f"Rescheduled — check in before "
                        f"{new_deadline.strftime('%H:%M:%S UTC')} "
                        f"or the ticket is forfeited."
                    )

            else:

                database.delete_flight(user_id)

                if member:

                    await self._notify(
                        member,
                        f"❌ You missed your rescheduled flight "
                        f"to {_dest_name(flight['destination'])} "
                        f"a second time. The ticket is forfeited "
                        f"— no refund."
                    )

    async def _scan_arrivals(self, guild, now):

        for flight in database.flights_by_status("in_transit"):

            arrival_at = _parse(flight["arrival_at"])

            if now < arrival_at:
                continue

            user_id = int(flight["user_id"])
            member = guild.get_member(user_id)

            database.update_flight(
                user_id,
                status="on_vacation"
            )

            database.update_player(
                user_id,
                location=flight["destination"]
            )

            if member:

                await permissions.set_write_access(
                    guild,
                    member,
                    flight["destination"],
                    allowed=True
                )

                await self._notify(
                    member,
                    f"🏝️ You've landed in "
                    f"**{_dest_name(flight['destination'])}**! "
                    f"Enjoy your vacation.",
                    location_code=flight["destination"]
                )

    async def _scan_return_reminders(self, guild, now):

        for flight in database.flights_by_status("on_vacation"):

            if flight["return_reminded"]:
                continue

            return_at = _parse(flight["return_at"])
            remaining = (return_at - now).total_seconds()

            if remaining > FLIGHT_RETURN_REMINDER_SECONDS:
                continue

            if remaining <= 0:
                # Too late for a heads-up — _scan_returns will
                # handle the actual return this same pass.
                continue

            user_id = int(flight["user_id"])
            member = guild.get_member(user_id)

            database.update_flight(
                user_id,
                return_reminded=1
            )

            if member:

                await self._notify(
                    member,
                    f"⏳ Your vacation in "
                    f"**{_dest_name(flight['destination'])}** "
                    f"ends in about "
                    f"{_fmt_seconds(max(0, remaining))} — "
                    f"you'll be flown back automatically.",
                    location_code=flight["destination"]
                )

    async def _scan_returns(self, guild, now):

        for flight in database.flights_by_status("on_vacation"):

            return_at = _parse(flight["return_at"])

            if now < return_at:
                continue

            user_id = int(flight["user_id"])
            member = guild.get_member(user_id)

            if member:

                await permissions.move_write_access(
                    guild,
                    member,
                    old_code=flight["destination"],
                    new_code=FLIGHT_AGENCY_LOCATION
                )

                role = discord.utils.get(
                    guild.roles,
                    name=FLIGHT_VACATION_ROLE
                )

                if role is not None:

                    try:
                        await member.remove_roles(
                            role,
                            reason="Returned from vacation"
                        )

                    except discord.Forbidden:
                        pass

                await self._notify(
                    member,
                    f"🛬 Welcome back from "
                    f"**{_dest_name(flight['destination'])}**! "
                    f"You've arrived at "
                    f"{LOCATIONS[FLIGHT_AGENCY_LOCATION]['name']}."
                )

            database.update_player(
                user_id,
                location=FLIGHT_AGENCY_LOCATION
            )

            database.delete_flight(user_id)

    async def _notify(
        self,
        member: discord.Member,
        content: str,
        location_code: str = FLIGHT_AGENCY_LOCATION
    ):

        channel = permissions.get_channel_for_code(
            member.guild,
            location_code
        )

        try:

            if channel is not None:
                await channel.send(f"{member.mention} {content}")
            else:
                await member.send(content)

        except discord.Forbidden:
            pass

        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(FlightCog(bot))
