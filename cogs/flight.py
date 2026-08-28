"""
Flights — Dubai and Maldives.

Flow:
    1. !bookflight <destination> <stay_minutes>
       Creates a PENDING travel request. No charge happens yet
       and there is no time constraint — the request just waits
       for an Immigration Officer to review it. (Booker's
       balance is checked up front purely so they get instant
       feedback if they obviously can't afford it; the real
       deduction — and re-check — happens at approval time.)

    2. An Immigration Officer reviews the request (posted to the
       immigration office / #help-desk with Approve/Deny
       buttons, or via !approveflight / !denyflight).
         - Approve -> balance is re-checked and the round-trip
           fare is deducted NOW, the check-in deadline starts
           counting from THIS moment, and the booker is notified
           by DM (falls back to a channel ping if DMs are
           closed).
         - Deny -> the request is deleted. No charge was ever
           made. The booker is notified by DM the same way.
       This means travelers can be denied travel outright.

    3. !checkin
       Must be typed in #travel-agency, and the player's
       database location must actually be "agency" (see
       checks.require_location). Must happen before the
       departure deadline (or the one-time reschedule
       deadline) — both of which only start existing once
       approved. Grants the "On vacation" role and removes
       write access everywhere — the player is "in the air".

    4. After FLIGHT_DURATION_SECONDS, the player automatically
       arrives: write access opens in the destination channel
       only.

    5. After stay_seconds, the player automatically returns:
       "On vacation" role is removed, write access moves back
       to #travel-agency, database location is set to "agency".

Missed check-in (only possible once approved, i.e. status is
"booked" — a still-pending request can never be "missed"):
    - 1st miss  -> departure deadline is pushed back by
                   FLIGHT_RESCHEDULE_WINDOW_SECONDS. One time
                   only.
    - 2nd miss  -> ticket is forfeited. No refund, no further
                   reschedule.

book_flight_for() below is a standalone function (not tied to
ctx) so the phone UI can call the exact same logic that
!bookflight uses — it already does, and needed no changes: it
just now returns "request submitted" instead of "booked",
transparently.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import checks
import database
import permissions

from cogs import hotel
from cogs import areas

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
    IMMIGRATION_OFFICER_ROLE,
    FLIGHT_IMMIGRATION_LOCATION,
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
            "⛔ You already have a pending or active flight "
            "booking. Use `!flightstatus` to check it."
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

    # No charge yet, and no departure deadline yet — both only
    # start once an Immigration Officer approves this request.
    # departure_at is just a placeholder until then (ignored:
    # the missed-check-in scan only ever looks at "booked"
    # flights, never "pending_approval" ones).
    database.book_flight(
        user_id,
        destination=destination,
        price_paid=round_trip_price,
        stay_seconds=stay_seconds,
        departure_at=_iso(_now()),
        status="pending_approval"
    )

    return True, (
        f"🛂 Travel request submitted for "
        f"**{_dest_name(destination)}** "
        f"({_fmt_seconds(stay_seconds)} vacation, "
        f"₦{round_trip_price:,} round trip).\n\n"
        f"This needs Immigration Officer approval before "
        f"anything is charged — there's no time limit on the "
        f"review, but you'll be notified by DM the moment it's "
        f"approved or denied. Use `!flightstatus` any time to "
        f"check where things stand."
    )


# ================================================================
# IMMIGRATION APPROVAL
# ================================================================

def _has_officer_role(member: discord.Member) -> bool:
    return discord.utils.get(
        member.roles,
        name=IMMIGRATION_OFFICER_ROLE
    ) is not None


async def _dm_or_notify(member: discord.Member, content: str) -> None:
    """
    The spec calls for a DM. If the booker has DMs closed,
    fall back to a ping in the immigration office channel
    rather than silently losing the notification.
    """

    try:
        await member.send(content)
        return

    except (discord.Forbidden, discord.HTTPException):
        pass

    channel = permissions.get_channel_for_code(
        member.guild,
        FLIGHT_IMMIGRATION_LOCATION
    )

    if channel is None:
        return

    try:
        await channel.send(f"{member.mention} {content}")

    except discord.HTTPException:
        pass


async def _approve_flight(
    guild: discord.Guild,
    user_id: int,
    officer: discord.Member
) -> str:
    """
    Shared by the Approve button and !approveflight. Re-checks
    the flight is still pending (guards double-clicks / two
    officers acting on the same request) and re-checks the
    booker's balance (it may have changed since the request was
    made) before actually charging them.

    Returns a short outcome line for whoever actioned it.
    """

    flight = database.get_flight(user_id)

    if flight is None or flight["status"] != "pending_approval":
        return "⚠️ This request is no longer pending — already handled."

    player = database.get_or_create_player(user_id)
    price = flight["price_paid"]

    if player["balance"] < price:

        # Can't charge them — the request can't just sit here
        # forever silently unpaid, so it's auto-denied instead.
        database.delete_flight(user_id)

        member = guild.get_member(user_id)

        if member is not None:
            await _dm_or_notify(
                member,
                f"❌ Your flight request to "
                f"{_dest_name(flight['destination'])} couldn't "
                f"be approved — you no longer have the "
                f"₦{price:,} round-trip fare. No charge was "
                f"made. Feel free to book again once you do."
            )

        return (
            f"❌ Auto-denied — <@{user_id}> can no longer "
            f"afford the ₦{price:,} fare."
        )

    database.update_player(
        user_id,
        balance=player["balance"] - price
    )

    departure_at = _now() + timedelta(
        seconds=FLIGHT_CHECKIN_WINDOW_SECONDS
    )

    database.update_flight(
        user_id,
        status="booked",
        departure_at=_iso(departure_at)
    )

    member = guild.get_member(user_id)

    if member is not None:
        await _dm_or_notify(
            member,
            f"✅ Your flight to "
            f"**{_dest_name(flight['destination'])}** has been "
            f"approved by immigration!\n"
            f"💵 ₦{price:,} (round trip) has been deducted.\n"
            f"🕒 Check in at "
            f"{LOCATIONS[FLIGHT_AGENCY_LOCATION]['name']} "
            f"(`!checkin`) before "
            f"**{departure_at.strftime('%H:%M:%S UTC')}**.\n\n"
            f"⚠️ Miss check-in and you get one reschedule. "
            f"Miss it a second time and the ticket is "
            f"forfeited — no refund."
        )

    return (
        f"✅ Approved by {officer.mention} — <@{user_id}> has "
        f"been notified and their check-in window has started."
    )


async def _deny_flight(
    guild: discord.Guild,
    user_id: int,
    officer: discord.Member
) -> str:
    """Shared by the Deny button and !denyflight."""

    flight = database.get_flight(user_id)

    if flight is None or flight["status"] != "pending_approval":
        return "⚠️ This request is no longer pending — already handled."

    database.delete_flight(user_id)

    member = guild.get_member(user_id)

    if member is not None:
        await _dm_or_notify(
            member,
            f"❌ Your flight request to "
            f"{_dest_name(flight['destination'])} was denied "
            f"by immigration. No charge was made."
        )

    return (
        f"❌ Denied by {officer.mention} — <@{user_id}> has "
        f"been notified. No charge was made."
    )


class FlightApprovalView(discord.ui.View):
    """
    Posted in the immigration office (#help-desk) for every new
    pending travel request. Deliberately timeout=None — there is
    no time constraint on immigration review. !approveflight /
    !denyflight exist as a text-command fallback in case the
    bot restarts and these buttons go dead (views aren't
    persisted across restarts in this bot).
    """

    def __init__(self, bot: commands.Bot, user_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = user_id
        self._resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        member = interaction.user

        if not isinstance(member, discord.Member) or not _has_officer_role(member):

            await interaction.response.send_message(
                f"⛔ Only an **{IMMIGRATION_OFFICER_ROLE}** can "
                f"review this.",
                ephemeral=True
            )

            return False

        return True

    async def _resolve(self, interaction: discord.Interaction, outcome: str):

        self._resolved = True

        for child in self.children:
            child.disabled = True

        try:
            await interaction.message.edit(content=outcome, view=self)

        except discord.HTTPException:
            pass

        self.stop()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.defer()

        outcome = await _approve_flight(
            interaction.guild,
            self.user_id,
            interaction.user
        )

        await self._resolve(interaction, outcome)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="🛑")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.defer()

        outcome = await _deny_flight(
            interaction.guild,
            self.user_id,
            interaction.user
        )

        await self._resolve(interaction, outcome)


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

        if flight["status"] == "pending_approval":

            await ctx.send(
                f"🛂 Your request to fly to **{name}** is "
                f"awaiting Immigration Officer approval. No "
                f"time limit — you'll be notified by DM the "
                f"moment it's reviewed."
            )

        elif flight["status"] == "booked":

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
    # !APPROVEFLIGHT / !DENYFLIGHT / !IMMIGRATIONQUEUE
    #
    # Text-command fallback for the Approve/Deny buttons — useful
    # if the bot has restarted since a request was posted (views
    # don't persist across restarts here).
    # ------------------------------------------------------------

    @commands.command(name="approveflight")
    async def approveflight(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not _has_officer_role(ctx.author):

            await ctx.send(
                f"⛔ You need the **{IMMIGRATION_OFFICER_ROLE}** "
                f"role to do this."
            )

            return

        if member is None:

            await ctx.send("Usage: `!approveflight @player`")
            return

        outcome = await _approve_flight(ctx.guild, member.id, ctx.author)
        await ctx.send(outcome)

    @commands.command(name="denyflight")
    async def denyflight(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not _has_officer_role(ctx.author):

            await ctx.send(
                f"⛔ You need the **{IMMIGRATION_OFFICER_ROLE}** "
                f"role to do this."
            )

            return

        if member is None:

            await ctx.send("Usage: `!denyflight @player`")
            return

        outcome = await _deny_flight(ctx.guild, member.id, ctx.author)
        await ctx.send(outcome)

    @commands.command(name="immigrationqueue")
    async def immigrationqueue(self, ctx: commands.Context):

        if not _has_officer_role(ctx.author):

            await ctx.send(
                f"⛔ You need the **{IMMIGRATION_OFFICER_ROLE}** "
                f"role to do this."
            )

            return

        pending = database.flights_by_status("pending_approval")

        if not pending:

            await ctx.send("🛂 No pending travel requests.")
            return

        lines = []

        for flight in pending:

            lines.append(
                f"• <@{flight['user_id']}> → "
                f"**{_dest_name(flight['destination'])}** "
                f"({_fmt_seconds(flight['stay_seconds'])}, "
                f"₦{flight['price_paid']:,})"
            )

        await ctx.send(
            "🛂 **Pending travel requests:**\n" + "\n".join(lines)
        )

    # ------------------------------------------------------------
    # BACKGROUND SCAN — approvals, missed check-ins, arrivals, returns
    # ------------------------------------------------------------

    @tasks.loop(seconds=FLIGHT_SCAN_INTERVAL_SECONDS)
    async def scan_flights(self):

        now = _now()

        for guild in self.bot.guilds:
            await self._scan_pending_approvals(guild)
            await self._scan_missed_checkins(guild, now)
            await self._scan_arrivals(guild, now)
            await self._scan_return_reminders(guild, now)
            await self._scan_returns(guild, now)

    @scan_flights.before_loop
    async def before_scan_flights(self):
        await self.bot.wait_until_ready()

    async def _scan_pending_approvals(self, guild):
        """
        Posts a review card (with Approve/Deny buttons) to the
        immigration office for every pending request that hasn't
        been posted yet — tracked via officer_notified so it's
        never posted twice.
        """

        channel = permissions.get_channel_for_code(
            guild,
            FLIGHT_IMMIGRATION_LOCATION
        )

        if channel is None:
            return

        role = discord.utils.get(
            guild.roles,
            name=IMMIGRATION_OFFICER_ROLE
        )

        role_mention = role.mention if role else f"**{IMMIGRATION_OFFICER_ROLE}**"

        for flight in database.flights_by_status("pending_approval"):

            if flight["officer_notified"]:
                continue

            user_id = int(flight["user_id"])
            member = guild.get_member(user_id)

            if member is None:
                # Try again next tick — they may just not have
                # cached yet, or belong to a different guild.
                continue

            try:

                await channel.send(
                    content=(
                        f"🛂 {role_mention} — new travel request\n"
                        f"{member.mention} wants to fly to "
                        f"**{_dest_name(flight['destination'])}** "
                        f"for {_fmt_seconds(flight['stay_seconds'])}. "
                        f"Round-trip fare: "
                        f"₦{flight['price_paid']:,}."
                    ),
                    view=FlightApprovalView(self.bot, user_id)
                )

                database.update_flight(user_id, officer_notified=1)

            except discord.HTTPException:
                pass

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

                await hotel.cleanup_hotel_for_user(guild, user_id)
                await areas.cleanup_area_for_user(guild, user_id)

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

                await areas.post_area_menu(
                    guild,
                    member,
                    flight["destination"]
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

            await hotel.cleanup_hotel_for_user(guild, user_id)
            await areas.cleanup_area_for_user(guild, user_id)

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
