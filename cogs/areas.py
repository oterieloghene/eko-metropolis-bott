"""
Overseas Areas — Downtown Dubai, Dubai Desert, Dubai Marina,
Paradise Resort, Blue Lagoon, Ocean Excursion.

Each area is a PRIVATE Discord thread living inside the
destination's main channel (#dubai or #maldives) — same
create_thread(type=private_thread, invitable=False) +
add_user/remove_user pattern hotel.py uses for hotel rooms.

A player is only ever a member of the thread for whichever area
they are CURRENTLY in. Nobody sees an area thread they haven't
been placed into:

    - On arrival (hooked from flight.py), a "Where do you want
      to go?" dropdown is posted in the destination channel.
      Picking an option (or typing !goto) is only allowed once
      the player has booked a hotel room.
    - !goto <area> removes the player from their old area's
      thread and adds them to the new one, storing the choice as
      current_area in the DB.
    - Threads are NEVER deleted — only archived when they become
      empty — so history is preserved and the thread is simply
      unarchived + reused the next time someone goes there.

flight.py calls cleanup_area_for_user() (safe no-op if the
player has no current area) whenever a vacation actually ends or
a ticket is forfeited, mirroring the hotel.py cleanup hook.

Shop (!mall/!fastfood/!spa) and event (!compete/!try) commands
enforce "must be run inside the matching area's thread" via
checks.require_area() in shops.py / events.py — this file only
owns getting players into and out of the right thread.
"""

import discord
from discord.ext import commands

import database
import permissions

from config import AREAS, AREAS_BY_COUNTRY, LOCATIONS


# ================================================================
# HELPERS
# ================================================================

def _dest_name(code: str) -> str:
    loc = LOCATIONS.get(code)
    return loc["name"] if loc else code.title()


def _area_label(area_code: str) -> str:
    cfg = AREAS[area_code]
    return f"{cfg['emoji']} {cfg['name']}"


def _has_hotel_room(user_id: int) -> bool:
    return (
        database.get_hotel_room(user_id) is not None
        or database.get_hotel_room_as_guest(user_id) is not None
    )


def resolve_area_code(raw: str) -> str | None:
    """Accept either the slug ('dubai-desert') or the display
    name ('Dubai Desert') for !goto, case-insensitive."""
    normalized = raw.strip().lower().replace(" ", "-")

    if normalized in AREAS:
        return normalized

    for code, cfg in AREAS.items():
        if cfg["name"].lower() == raw.strip().lower():
            return code

    return None


async def _fetch_thread(guild: discord.Guild, thread_id: str) -> discord.Thread | None:
    thread = guild.get_thread(int(thread_id))

    if thread is None:
        try:
            thread = await guild.fetch_channel(int(thread_id))
        except discord.HTTPException:
            thread = None

    return thread


async def _get_or_create_area_thread(guild: discord.Guild, area_code: str) -> discord.Thread | None:
    """Return the area's private thread, creating it on first use
    or unarchiving it if it's currently empty/archived."""

    cfg = AREAS[area_code]
    row = database.get_area(area_code)

    if row is not None and row["thread_id"]:
        thread = await _fetch_thread(guild, row["thread_id"])

        if thread is not None:
            if row["archived"]:
                try:
                    await thread.edit(archived=False)
                except discord.HTTPException:
                    pass
                database.set_area_archived(area_code, False)

            return thread

    # No usable existing thread — create a fresh one.
    channel = permissions.get_channel_for_code(guild, cfg["country"])

    if channel is None:
        return None

    try:
        thread = await channel.create_thread(
            name=_area_label(area_code),
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
    except discord.HTTPException:
        return None

    database.upsert_area_thread(area_code, cfg["country"], thread.id, archived=False)

    return thread


async def _archive_if_empty(guild: discord.Guild, thread: discord.Thread) -> None:
    """After removing a player, archive the thread if nobody
    (non-bot) is left in it. Never deletes — messages/activity
    stay intact for the next visit."""

    try:
        members = await thread.fetch_members()
    except discord.HTTPException:
        return

    if len(members) > 0:
        return

    area_row = database.get_area_by_thread(thread.id)

    try:
        await thread.edit(archived=True)
    except discord.HTTPException:
        pass

    if area_row is not None:
        database.set_area_archived(area_row["area_code"], True)


# ================================================================
# CORE — MOVE A PLAYER INTO AN AREA
# ================================================================

async def enter_area(guild: discord.Guild, member: discord.Member, area_code: str) -> str:
    """
    Attempt to move `member` into `area_code`. Returns a message
    to send back to them. Shared by !goto and the arrival
    dropdown menu.
    """

    cfg = AREAS.get(area_code)

    if cfg is None:
        return f"\u26d4 Unknown area `{area_code}`."

    player = database.get_or_create_player(member.id)

    if player["location"] != cfg["country"]:
        return f"\u26d4 You need to be in {_dest_name(cfg['country'])} to go there."

    if not _has_hotel_room(member.id):
        return "\u26d4 Book a hotel room first with `!bookhotel` before heading out."

    if player["current_area"] == area_code:
        return f"You're already at **{_area_label(area_code)}**."

    # Leave the old area, if any.
    old_area_code = player["current_area"]

    if old_area_code:
        old_row = database.get_area(old_area_code)

        if old_row is not None and old_row["thread_id"]:
            old_thread = await _fetch_thread(guild, old_row["thread_id"])

            if old_thread is not None:
                try:
                    await old_thread.remove_user(member)
                except discord.HTTPException:
                    pass

                await _archive_if_empty(guild, old_thread)

    new_thread = await _get_or_create_area_thread(guild, area_code)

    if new_thread is None:
        return "\u26d4 Couldn't open that area right now. Try again shortly."

    try:
        await new_thread.add_user(member)
    except discord.HTTPException:
        return "\u26d4 Couldn't add you to that area's thread. Try again."

    database.update_player(member.id, current_area=area_code)

    try:
        await new_thread.send(f"{member.mention} has arrived at **{_area_label(area_code)}**.")
    except discord.HTTPException:
        pass

    return f"\u2705 You're now at **{_area_label(area_code)}** \u2014 {new_thread.mention}"


async def cleanup_area_for_user(guild: discord.Guild, user_id: int) -> None:
    """
    Remove a player from whatever area thread they're currently
    in, archiving it if that leaves it empty, and clear
    current_area. Called by flight.py on vacation return/ticket
    forfeiture. Safe no-op if they have no current area.
    """

    player = database.get_player(user_id)

    if player is None or not player["current_area"]:
        return

    area_code = player["current_area"]
    row = database.get_area(area_code)

    if row is not None and row["thread_id"]:
        thread = await _fetch_thread(guild, row["thread_id"])

        if thread is not None:
            member = guild.get_member(user_id)

            if member is not None:
                try:
                    await thread.remove_user(member)
                except discord.HTTPException:
                    pass

            await _archive_if_empty(guild, thread)

    database.update_player(user_id, current_area=None)


# ================================================================
# "WHERE DO YOU WANT TO GO?" — posted on arrival
# ================================================================

class _AreaSelect(discord.ui.Select):

    def __init__(self, country: str):
        codes = AREAS_BY_COUNTRY[country]

        options = [
            discord.SelectOption(
                label=AREAS[code]["name"],
                value=code,
                emoji=AREAS[code]["emoji"],
            )
            for code in codes
        ]

        super().__init__(
            placeholder="Where do you want to go?",
            options=options,
            min_values=1,
            max_values=1,
            custom_id=f"area_select:{country}",
        )

    async def callback(self, interaction: discord.Interaction):
        area_code = self.values[0]

        message = await enter_area(interaction.guild, interaction.user, area_code)

        await interaction.response.send_message(message, ephemeral=True)


class AreaMenuView(discord.ui.View):
    """
    Not owner-locked on purpose — this is a standing "Where do
    you want to go?" prompt any arriving tourist in the channel
    can use; each selection only ever affects the person who made
    it. timeout=None so it doesn't expire mid-vacation.
    """

    def __init__(self, country: str):
        super().__init__(timeout=None)
        self.add_item(_AreaSelect(country))


async def post_area_menu(guild: discord.Guild, member: discord.Member, country: str) -> None:
    channel = permissions.get_channel_for_code(guild, country)

    if channel is None:
        return

    try:
        await channel.send(
            f"{member.mention} \U0001f9ed **Where do you want to go?**\n"
            f"(Book a hotel room first with `!bookhotel` if you haven't already.)",
            view=AreaMenuView(country),
        )
    except discord.HTTPException:
        pass


# ================================================================
# COG
# ================================================================

class AreasCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------
    # !goto <area>
    # ------------------------------------------------------------

    @commands.command(name="goto")
    async def goto(self, ctx: commands.Context, *, area: str = None):
        if area is None:
            available = ", ".join(cfg["name"] for cfg in AREAS.values())
            await ctx.send(f"Usage: `!goto <area>`\nAreas: {available}")
            return

        area_code = resolve_area_code(area)

        if area_code is None:
            await ctx.send(f"\u26d4 Unknown area `{area}`.")
            return

        message = await enter_area(ctx.guild, ctx.author, area_code)
        await ctx.send(message)

    # ------------------------------------------------------------
    # !area — show current area
    # ------------------------------------------------------------

    @commands.command(name="area")
    async def area_status(self, ctx: commands.Context):
        player = database.get_or_create_player(ctx.author.id)

        if not player["current_area"]:
            await ctx.send("You're not currently in any area.")
            return

        await ctx.send(f"You're at **{_area_label(player['current_area'])}**.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AreasCog(bot))
