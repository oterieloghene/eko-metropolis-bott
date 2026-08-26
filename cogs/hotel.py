"""
Hotels — Dubai and Maldives room booking.

Only usable while the player is physically overseas already (their DB
location is "dubai" or "maldives" — i.e. mid-vacation via flight.py).

Flow:
    !bookhotel standard
    !bookhotel luxury [@guest]

    - Price scales with the player's REMAINING vacation time (their
      flight's return_at minus now), between the same 2-30 minute
      bounds flights use — not the original full stay, so a room
      booked partway through a trip only charges for time left.
    - A private Discord thread is created in that destination's
      channel, named "🏨 Standard Room N" / "🏬 Luxury Room N", N
      reused from a small per-destination-per-tier free pool
      (HOTEL_ROOMS_PER_TIER of each).
    - Luxury bookings charge and create the room immediately for the
      booker. If a guest was named, they're DMed an Accept/Decline
      button (HOTEL_GUEST_RESPONSE_TIMEOUT_SECONDS to respond). Accept
      adds them to the thread. Decline/timeout/no response changes
      nothing else — the booker already has their room either way.
    - 3 room-service flavor messages land in the thread at check-in,
      1/3, and 2/3 of the room's stay (reusing the tasks.loop pattern
      from flight.py). Purely cosmetic — no inventory, no tracking.
    - !eat only works inside an active hotel thread. Always the same
      canned reply.

Cleanup is triggered by flight.py itself (two small hook calls there)
whenever a player's vacation actually ends or their ticket is
forfeited pre-arrival — see cleanup_hotel_for_user() below, which is
safe to call even when the player has no room.
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
    OVERSEAS,
    HOTEL_ROOMS_PER_TIER,
    HOTEL_STANDARD_MIN_PRICE,
    HOTEL_STANDARD_MAX_PRICE,
    HOTEL_LUXURY_MULTIPLIER,
    HOTEL_GUEST_RESPONSE_TIMEOUT_SECONDS,
    HOTEL_ROOM_SERVICE_FRACTIONS,
    HOTEL_DISHES,
    HOTEL_SCAN_INTERVAL_SECONDS,
    FLIGHT_MIN_STAY_SECONDS,
    FLIGHT_MAX_STAY_SECONDS,
)

ROOM_ICON = {"standard": "\U0001f3e8", "luxury": "\U0001f3ec"}  # 🏨 / 🏬


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


def _price_for(tier: str, remaining_seconds: int) -> int:
    """Linear price between the standard min/max, scaled by remaining
    vacation time (clamped to the same bounds flights use)."""
    clamped = max(FLIGHT_MIN_STAY_SECONDS, min(FLIGHT_MAX_STAY_SECONDS, remaining_seconds))
    span = FLIGHT_MAX_STAY_SECONDS - FLIGHT_MIN_STAY_SECONDS
    fraction = (clamped - FLIGHT_MIN_STAY_SECONDS) / span if span else 0
    standard_price = HOTEL_STANDARD_MIN_PRICE + fraction * (HOTEL_STANDARD_MAX_PRICE - HOTEL_STANDARD_MIN_PRICE)

    if tier == "luxury":
        return round(standard_price * HOTEL_LUXURY_MULTIPLIER)
    return round(standard_price)


def _next_free_room_number(destination: str, tier: str) -> int | None:
    taken = {row["room_number"] for row in database.rooms_in_use(destination, tier)}
    for n in range(1, HOTEL_ROOMS_PER_TIER + 1):
        if n not in taken:
            return n
    return None


async def cleanup_hotel_for_user(guild: discord.Guild, user_id: int) -> None:
    """
    Delete a player's active hotel room/thread, if they have one —
    as booker OR as an accepted guest. Called by flight.py when a
    vacation ends or a ticket is forfeited. Safe no-op otherwise.
    """
    room = database.get_hotel_room(user_id)

    if room is None:
        # They might be someone else's accepted guest instead.
        room = database.get_hotel_room_as_guest(user_id)
        if room is None:
            return

    thread = guild.get_thread(int(room["thread_id"]))
    if thread is None:
        try:
            thread = await guild.fetch_channel(int(room["thread_id"]))
        except discord.HTTPException:
            thread = None

    if thread is not None:
        try:
            await thread.delete()
        except discord.HTTPException:
            pass

    database.delete_hotel_room(int(room["booker_id"]))


# ================================================================
# GUEST INVITE UI
# ================================================================

class _GuestInviteView(discord.ui.View):
    def __init__(self, booker_id: int, guest_id: int, room_label: str, thread_id: int, bot: commands.Bot):
        super().__init__(timeout=HOTEL_GUEST_RESPONSE_TIMEOUT_SECONDS)
        self.booker_id = booker_id
        self.guest_id = guest_id
        self.room_label = room_label
        self.thread_id = thread_id
        self.bot = bot
        self.responded = False

    async def _disable(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="\u2705")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.guest_id:
            await interaction.response.send_message("This invite isn't for you.", ephemeral=True)
            return

        self.responded = True
        await self._disable(interaction)

        room = database.get_hotel_room(self.booker_id)
        if room is None or int(room["thread_id"]) != self.thread_id:
            await interaction.followup.send("That room is no longer available.", ephemeral=True)
            self.stop()
            return

        guild = self.bot.get_guild(interaction.guild_id) if interaction.guild_id else self.bot.guilds[0]
        thread = guild.get_thread(self.thread_id)

        if thread is not None:
            try:
                await thread.add_user(interaction.user)
                await thread.send(f"{interaction.user.mention} has joined **{self.room_label}**. \U0001f389")
            except discord.HTTPException:
                pass

        database.update_hotel_room(self.booker_id, guest_id=str(self.guest_id))
        await interaction.followup.send(f"Joined **{self.room_label}**!", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="\u274c")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.guest_id:
            await interaction.response.send_message("This invite isn't for you.", ephemeral=True)
            return

        self.responded = True
        await self._disable(interaction)
        await interaction.followup.send("Invite declined.", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        pass  # booker already has the room either way — nothing to undo


# ================================================================
# BOOKING (shared logic — usable by !bookhotel and, later, phone)
# ================================================================

async def book_hotel_for(
    bot: commands.Bot,
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    user_id: int,
    tier: str,
    guest: discord.Member | None,
) -> str:
    """
    Attempt to book a hotel room. Returns a message to send back to
    the booker (the caller sends it — keeps this callable from both
    !bookhotel and a future phone button, same pattern as flight.py).
    """
    tier = tier.strip().lower()
    if tier not in ("standard", "luxury"):
        return "\u26d4 Choose a tier: `standard` or `luxury`."

    player = database.get_or_create_player(user_id)
    destination = player["location"]

    if destination not in OVERSEAS:
        return "\u26d4 You're not overseas right now."

    if database.get_hotel_room(user_id) is not None:
        return "\u26d4 You already have an active hotel room. Use `!hotelstatus` to check it."

    if database.get_hotel_room_as_guest(user_id) is not None:
        return "\u26d4 You're already a guest in someone else's room."

    flight = database.get_flight(user_id)
    if flight is None or flight["status"] != "on_vacation":
        return "\u26d4 You need to be on an active vacation to book a room."

    return_at = _parse(flight["return_at"])
    remaining = (return_at - _now()).total_seconds()

    if remaining <= 0:
        return "\u26d4 Your vacation is about to end — no time left to book a room."

    room_number = _next_free_room_number(destination, tier)
    if room_number is None:
        return f"\u26d4 All {tier.title()} rooms at {_dest_name(destination)} are currently full."

    if guest is not None:
        if guest.id == user_id:
            return "\u26d4 You can't invite yourself."

        if tier != "luxury":
            return "\u26d4 Only Luxury rooms can have a guest."

        guest_player = database.get_player(guest.id)
        if guest_player is None or guest_player["location"] != destination:
            return f"\u26d4 {guest.mention} isn't at {_dest_name(destination)} right now."

        if database.get_hotel_room(guest.id) is not None or database.get_hotel_room_as_guest(guest.id) is not None:
            return f"\u26d4 {guest.mention} is already occupying a room."

    price = _price_for(tier, int(remaining))
    if player["balance"] < price:
        return f"\u26d4 You need \u20a6{price:,} for a {tier.title()} room. You have \u20a6{player['balance']:,}."

    # Charge + create the room now — guest response (if any) never
    # affects this part.
    database.update_player(user_id, balance=player["balance"] - price)

    dest_channel = permissions.get_channel_for_code(guild, destination)
    room_label = f"{ROOM_ICON[tier]} {tier.title()} Room {room_number}"

    try:
        thread = await dest_channel.create_thread(
            name=room_label,
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        booker_member = guild.get_member(user_id)
        if booker_member:
            await thread.add_user(booker_member)
    except discord.HTTPException:
        database.update_player(user_id, balance=player["balance"])  # refund — room creation failed
        return "\u26d4 Couldn't create the room thread. Try again — you have not been charged."

    database.book_hotel_room(
        booker_id=user_id,
        destination=destination,
        tier=tier,
        room_number=room_number,
        thread_id=thread.id,
        price_paid=price,
        checked_in_at=_iso(_now()),
        stay_seconds=int(remaining),
    )

    await thread.send(
        f"{room_label}\nWelcome, <@{user_id}>! \u20a6{price:,} paid.\n"
        f"Checked out automatically when your vacation ends."
    )

    reply = f"\u2705 Checked into **{room_label}** at {_dest_name(destination)} for \u20a6{price:,}."

    if guest is not None:
        try:
            dm_view = _GuestInviteView(user_id, guest.id, room_label, thread.id, bot)
            await guest.send(
                f"\U0001f3e8 <@{user_id}> invited you to join their **{room_label}** "
                f"at {_dest_name(destination)}. Accept to join them there.",
                view=dm_view,
            )
            reply += f"\n\U0001f4e9 Invite sent to {guest.mention} — they have {HOTEL_GUEST_RESPONSE_TIMEOUT_SECONDS // 60} min to accept."
        except discord.Forbidden:
            reply += f"\n\u26a0\ufe0f Couldn't DM {guest.mention} the invite (their DMs may be closed)."

    return reply


# ================================================================
# COG
# ================================================================

class HotelCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scan_room_service.start()

    def cog_unload(self):
        self.scan_room_service.cancel()

    # ------------------------------------------------------------
    # !bookhotel <standard|luxury> [@guest]
    # ------------------------------------------------------------

    @commands.command(name="bookhotel")
    async def bookhotel(self, ctx: commands.Context, tier: str = None, guest: discord.Member = None):
        if tier is None:
            await ctx.send("Usage: `!bookhotel <standard|luxury> [@guest]` (guest only for luxury)")
            return

        message = await book_hotel_for(self.bot, ctx.guild, ctx.channel, ctx.author.id, tier, guest)
        await ctx.send(message)

    # ------------------------------------------------------------
    # !hotelstatus
    # ------------------------------------------------------------

    @commands.command(name="hotelstatus")
    async def hotelstatus(self, ctx: commands.Context):
        room = database.get_hotel_room(ctx.author.id)
        as_guest = False

        if room is None:
            room = database.get_hotel_room_as_guest(ctx.author.id)
            as_guest = True

        if room is None:
            await ctx.send("You don't have an active hotel room.")
            return

        label = f"{ROOM_ICON[room['tier']]} {room['tier'].title()} Room {room['room_number']}"
        role = "guest in" if as_guest else "booked"
        await ctx.send(f"You are {role} **{label}** at {_dest_name(room['destination'])}.")

    # ------------------------------------------------------------
    # !eat — only inside an active hotel thread, always the same
    # flavor line, no state.
    # ------------------------------------------------------------

    @commands.command(name="eat")
    async def eat(self, ctx: commands.Context):
        room = database.get_hotel_room_by_thread(ctx.channel.id)

        if room is None:
            await ctx.send("\u26d4 There's nothing to eat here.")
            return

        await ctx.send("\U0001f60b You have eaten.")

    # ------------------------------------------------------------
    # BACKGROUND SCAN — room service deliveries
    # ------------------------------------------------------------

    @tasks.loop(seconds=HOTEL_SCAN_INTERVAL_SECONDS)
    async def scan_room_service(self):
        now = _now()

        for room in database.all_hotel_rooms():
            service_index = room["service_index"]

            if service_index >= len(HOTEL_ROOM_SERVICE_FRACTIONS):
                continue

            checked_in_at = _parse(room["checked_in_at"])
            stay_seconds = room["stay_seconds"]
            due_at = checked_in_at + timedelta(
                seconds=stay_seconds * HOTEL_ROOM_SERVICE_FRACTIONS[service_index]
            )

            if now < due_at:
                continue

            guild = None
            for g in self.bot.guilds:
                if g.get_thread(int(room["thread_id"])) is not None:
                    guild = g
                    break

            thread = guild.get_thread(int(room["thread_id"])) if guild else None

            if thread is not None:
                dish = HOTEL_DISHES[service_index % len(HOTEL_DISHES)]
                portion = "two plates of" if room["tier"] == "luxury" else "a plate of"

                try:
                    await thread.send(
                        f"\U0001f37d\ufe0f Room service has delivered {portion} your **{dish}**."
                    )
                except discord.HTTPException:
                    pass

            database.update_hotel_room(int(room["booker_id"]), service_index=service_index + 1)

    @scan_room_service.before_loop
    async def before_scan_room_service(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(HotelCog(bot))
