"""
Mechanic dispatch — "book a mechanic" via !bookmechanic (and the
phone), mirroring taxi.py's request/accept/timeout flow.

This file is intentionally self-contained. It does NOT modify
taxi.py or repair.py, and repair.py's !fixcar keeps working exactly
as it did before, untouched, for anyone who wants to flag a mechanic
down manually instead of booking one.

HOW IT DIFFERS FROM A TAXI RIDE:

    A repair job has one leg, not two. A taxi driver first drives
    to the RIDER's origin (pickup), then to the destination. A
    mechanic just drives straight to wherever the CUSTOMER's
    vehicle currently sits (vehicle_location — the same field
    !fixcar already keys off), and the repair fires automatically
    the moment they arrive. There's no separate pickup step and no
    destination to choose.

    It's also simpler on purpose: one job at a time per mechanic,
    no tiers, no queueing. If nobody is online, the customer is
    told to try again shortly rather than being queued — this can
    grow a queue later the same way taxi.py has one, if needed.

INTEGRATION POINTS (the only two places outside this file that
know mechanic.py exists):

    - bot.py adds "cogs.mechanic" to the COGS list.
    - travel.py calls peek_confirmed_job() / take_confirmed_job()
      / handle_mechanic_arrival(), the same way it already calls
      the equivalent taxi.py functions, so a mechanic can actually
      !drive to the job and have the repair resolve on arrival.
"""

import asyncio

import discord
from discord.ext import commands

import database
import permissions

from config import (
    LOCATIONS,
    MECHANIC_ROLE,
    REPAIR_COST_PER_POINT,
    MECHANIC_REQUEST_TIMEOUT_SECONDS,
    MECHANIC_MESSAGE_DELETE_DELAY_SECONDS,
)


# ================================================================
# MODULE-LEVEL STATE
#
#     _pending_requests[mechanic_id] = {
#         "owner_id":     int,   # whose vehicle needs fixing
#         "vehicle":      str,
#         "vehicle_code": str,   # where the vehicle is parked
#         "condition":    float,
#         "cost":         int,
#         "channel_id":   int,   # owner's channel, for updates
#         "guild_id":     int,
#         "request_message_id": int | None,
#         "_channel":     discord.TextChannel | None,
#         "timeout_task": asyncio.Task,
#     }
#
#     _confirmed_job[mechanic_id] = {
#         "owner_id":     int,
#         "vehicle_code": str,
#     }
# ================================================================

_pending_requests: dict[int, dict] = {}
_confirmed_job: dict[int, dict] = {}


# ================================================================
# SMALL HELPERS
# ================================================================

def _name(code: str) -> str:
    loc = LOCATIONS.get(code)
    return loc["name"] if loc else code


async def _send_and_delete(ctx: commands.Context, content: str) -> None:

    msg = await ctx.send(content)

    asyncio.create_task(
        _delete_after_delay(msg, MECHANIC_MESSAGE_DELETE_DELAY_SECONDS)
    )

    try:
        await ctx.message.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _delete_after_delay(msg: discord.Message, delay: float) -> None:

    await asyncio.sleep(delay)

    try:
        await msg.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _send_and_delete_channel(
    channel: discord.abc.Messageable | None,
    content: str
) -> None:

    if channel is None:
        return

    try:
        msg = await channel.send(content)

    except (discord.Forbidden, discord.NotFound):
        return

    asyncio.create_task(
        _delete_after_delay(msg, MECHANIC_MESSAGE_DELETE_DELAY_SECONDS)
    )


async def _delete_request_message(entry: dict) -> None:

    channel = entry.get("_channel")
    message_id = entry.get("request_message_id")

    if channel is None or message_id is None:
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.delete()

    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


def _nearest_free_mechanic() -> int | None:
    """
    First online mechanic not already juggling a request or a
    job. "Nearest" is aspirational naming to match taxi.py — this
    version doesn't do distance routing, it just takes the first
    free one, since a mechanic (unlike a taxi) has no fixed base
    to measure distance from until they're already assigned.
    """

    for row in database.get_online_mechanics():

        mechanic_id = int(row["user_id"])

        if mechanic_id in _pending_requests:
            continue

        if mechanic_id in _confirmed_job:
            continue

        return mechanic_id

    return None


def _find_active_entry(owner_id: int):
    """
    Returns ("pending", request, mechanic_id) or
    ("confirmed", job, mechanic_id) or (None, None, None).
    """

    for mechanic_id, request in _pending_requests.items():

        if request["owner_id"] == owner_id:
            return ("pending", request, mechanic_id)

    for mechanic_id, job in _confirmed_job.items():

        if job["owner_id"] == owner_id:
            return ("confirmed", job, mechanic_id)

    return (None, None, None)


# ================================================================
# DISPATCH ENGINE
# ================================================================

async def _dispatch(
    guild: discord.Guild,
    entry: dict,
    mechanic_id: int,
    prefix: str = ""
) -> None:

    channel = guild.get_channel(entry["channel_id"])

    timeout_task = asyncio.create_task(
        _expire_request(mechanic_id, guild, entry["channel_id"])
    )

    request = {
        **entry,
        "guild_id": guild.id,
        "request_message_id": None,
        "_channel": channel,
        "timeout_task": timeout_task,
    }

    _pending_requests[mechanic_id] = request

    mechanic_member = guild.get_member(mechanic_id)

    mention = (
        mechanic_member.mention
        if mechanic_member
        else f"<@{mechanic_id}>"
    )

    if channel is not None:

        try:
            msg = await channel.send(
                f"{prefix}"
                f"\U0001f527 Mechanic request sent to {mention}.\n"
                f"Vehicle: **{entry['vehicle']}** at "
                f"**{_name(entry['vehicle_code'])}** "
                f"(condition {entry['condition']:.0f}%).\n"
                f"Estimated cost: \u20a6{entry['cost']:,}.\n\n"
                f"They have {MECHANIC_REQUEST_TIMEOUT_SECONDS}s to "
                f"respond with `!mechanicaccept` or "
                f"`!mechanicdecline`."
            )

            request["request_message_id"] = msg.id

        except (discord.Forbidden, discord.NotFound):
            pass


async def _handle_unavailable(
    guild: discord.Guild,
    entry: dict,
    prefix: str = ""
) -> None:

    mechanic_id = _nearest_free_mechanic()

    if mechanic_id is not None:

        await _dispatch(guild, entry, mechanic_id, prefix=prefix)

        return

    channel = guild.get_channel(entry["channel_id"])

    await _send_and_delete_channel(
        channel,
        f"\U0001f527 No mechanics are online right now, "
        f"<@{entry['owner_id']}>. Try `!bookmechanic` again "
        f"shortly."
    )


async def _expire_request(
    mechanic_id: int,
    guild: discord.Guild,
    channel_id: int
) -> None:

    await asyncio.sleep(MECHANIC_REQUEST_TIMEOUT_SECONDS)

    request = _pending_requests.get(mechanic_id)

    if request is None:
        return

    _pending_requests.pop(mechanic_id, None)

    await _delete_request_message(request)

    channel = guild.get_channel(channel_id)

    await _send_and_delete_channel(
        channel,
        f"\u231b Mechanic request for <@{request['owner_id']}> "
        f"expired — no response. Looking for another mechanic..."
    )

    await _handle_unavailable(guild, request)


# ================================================================
# FUNCTIONS CALLED FROM travel.py
# ================================================================

def peek_confirmed_job(mechanic_id: int) -> dict | None:
    """
    Look at this mechanic's confirmed job WITHOUT popping it.
    Used by travel.py early in !drive to override whatever
    destination the mechanic typed with the job's actual
    destination (the vehicle's location).
    """

    return _confirmed_job.get(mechanic_id)


def take_confirmed_job(mechanic_id: int) -> dict | None:
    """
    Pop and return this mechanic's confirmed job. Called once,
    right when !drive actually commits to starting the trip.
    """

    return _confirmed_job.pop(mechanic_id, None)


def requeue_job(mechanic_id: int, job: dict) -> None:
    """
    Put a confirmed job back if !drive fails AFTER already having
    popped it (e.g. not enough fuel), so the mechanic doesn't
    lose the job.
    """

    _confirmed_job[mechanic_id] = job


async def handle_mechanic_arrival(
    guild: discord.Guild,
    mechanic: discord.Member,
    job: dict
) -> str | None:
    """
    Called by travel.py once the mechanic reaches the vehicle's
    location. Runs the same repair math as !fixcar (repair.py is
    untouched — this is a parallel implementation, not a call
    into it, since !fixcar is written around a live ctx) and
    bills the OWNER, not the mechanic.

    Returns a short summary line to append to the arrival
    message, or None.
    """

    owner_id = job["owner_id"]

    owner = database.get_player(owner_id)

    if owner is None or not owner["vehicle"]:

        return (
            "\u26a0\ufe0f The owner no longer has a vehicle — "
            "nothing to repair."
        )

    needed = 100 - owner["vehicle_condition"]

    if needed <= 0:

        return (
            f"\U0001f527 {mechanic.mention} arrived, but the "
            f"vehicle was already at full condition."
        )

    cost = round(needed * REPAIR_COST_PER_POINT)
    owner_balance = owner["balance"]

    if owner_balance < cost:

        affordable_points = owner_balance / REPAIR_COST_PER_POINT

        database.update_player(
            owner_id,
            balance=0,
            vehicle_condition=owner["vehicle_condition"] + affordable_points,
        )

        return (
            f"\U0001f527 {mechanic.mention} could only afford a "
            f"partial repair on <@{owner_id}>'s vehicle: "
            f"+{affordable_points:.0f} condition "
            f"(\u20a6{owner_balance:,} spent)."
        )

    database.update_player(
        owner_id,
        balance=owner_balance - cost,
        vehicle_condition=100,
    )

    return (
        f"\U0001f527 {mechanic.mention} fully repaired "
        f"<@{owner_id}>'s {owner['vehicle']} for \u20a6{cost:,}."
    )


# ================================================================
# COG
# ================================================================

class MechanicCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ============================================================
    # !MECHANICSTART / !MECHANICSTOP
    # ============================================================

    @commands.command(name="mechanicstart")
    async def mechanicstart(self, ctx: commands.Context):

        role = discord.utils.get(ctx.author.roles, name=MECHANIC_ROLE)

        if role is None:

            await _send_and_delete(
                ctx,
                f"\u26d4 You need the **{MECHANIC_ROLE}** role to "
                f"go online."
            )

            return

        database.set_mechanic_online(ctx.author.id, True)

        await _send_and_delete(
            ctx,
            f"\U0001f7e2 {ctx.author.mention} is now online and "
            f"bookable as a mechanic. Stay online until "
            f"`!mechanicstop`."
        )

    @commands.command(name="mechanicstop")
    async def mechanicstop(self, ctx: commands.Context):

        role = discord.utils.get(ctx.author.roles, name=MECHANIC_ROLE)

        if role is None:

            await _send_and_delete(
                ctx,
                f"\u26d4 You need the **{MECHANIC_ROLE}** role."
            )

            return

        database.set_mechanic_online(ctx.author.id, False)

        await _send_and_delete(
            ctx,
            f"\U0001f534 {ctx.author.mention} is now offline."
        )

    # ============================================================
    # !BOOKMECHANIC
    # ============================================================

    @commands.command(name="bookmechanic")
    async def bookmechanic(self, ctx: commands.Context):

        player = database.get_or_create_player(ctx.author.id)

        if not player["vehicle"]:

            await _send_and_delete(
                ctx,
                "\u26d4 You don't own a vehicle."
            )

            return

        needed = 100 - player["vehicle_condition"]

        if needed <= 0:

            await _send_and_delete(
                ctx,
                "Your vehicle is already in perfect condition."
            )

            return

        status, _, _ = _find_active_entry(ctx.author.id)

        if status == "pending":

            await _send_and_delete(
                ctx,
                "You already have a pending mechanic request."
            )

            return

        if status == "confirmed":

            await _send_and_delete(
                ctx,
                "A mechanic is already on the way."
            )

            return

        vehicle_code = player["vehicle_location"] or player["location"]
        cost = round(needed * REPAIR_COST_PER_POINT)

        entry = {
            "owner_id": ctx.author.id,
            "vehicle": player["vehicle"],
            "vehicle_code": vehicle_code,
            "condition": player["vehicle_condition"],
            "cost": cost,
            "channel_id": ctx.channel.id,
        }

        mechanic_id = _nearest_free_mechanic()

        if mechanic_id is None:

            await _send_and_delete(
                ctx,
                "\U0001f527 No mechanics are online right now. "
                "Try again shortly."
            )

            return

        await _dispatch(ctx.guild, entry, mechanic_id)

        await _send_and_delete(
            ctx,
            f"\U0001f527 Looking for a mechanic for your "
            f"{player['vehicle']}..."
        )

    # ============================================================
    # !MECHANICACCEPT
    # ============================================================

    @commands.command(name="mechanicaccept")
    async def mechanicaccept(self, ctx: commands.Context):

        request = _pending_requests.get(ctx.author.id)

        if request is None:

            await _send_and_delete(
                ctx,
                "You have no pending mechanic request."
            )

            return

        request["timeout_task"].cancel()

        _pending_requests.pop(ctx.author.id, None)

        await _delete_request_message(request)

        _confirmed_job[ctx.author.id] = {
            "owner_id": request["owner_id"],
            "vehicle_code": request["vehicle_code"],
        }

        await _send_and_delete_channel(
            request.get("_channel"),
            f"\u2705 {ctx.author.mention} accepted — heading to "
            f"**{_name(request['vehicle_code'])}**."
        )

        await _send_and_delete(
            ctx,
            f"\u2705 Job accepted. `!drive` to "
            f"**{_name(request['vehicle_code'])}** — the repair "
            f"runs automatically the moment you arrive."
        )

    # ============================================================
    # !MECHANICDECLINE
    # ============================================================

    @commands.command(name="mechanicdecline")
    async def mechanicdecline(self, ctx: commands.Context):

        request = _pending_requests.pop(ctx.author.id, None)

        if request is None:

            await _send_and_delete(
                ctx,
                "You have no pending mechanic request."
            )

            return

        request["timeout_task"].cancel()

        await _delete_request_message(request)

        await _send_and_delete(
            ctx,
            "Declined."
        )

        await _handle_unavailable(ctx.guild, request)

    # ============================================================
    # !CANCELMECHANIC (customer side)
    # ============================================================

    @commands.command(name="cancelmechanic")
    async def cancelmechanic(self, ctx: commands.Context):

        status, entry, mechanic_id = _find_active_entry(ctx.author.id)

        if status == "pending":

            entry["timeout_task"].cancel()

            _pending_requests.pop(mechanic_id, None)

            await _delete_request_message(entry)

            await _send_and_delete(
                ctx,
                "Mechanic request cancelled."
            )

            return

        if status == "confirmed":

            _confirmed_job.pop(mechanic_id, None)

            await _send_and_delete(
                ctx,
                "Mechanic job cancelled."
            )

            return

        await _send_and_delete(
            ctx,
            "You have no active mechanic request."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MechanicCog(bot))
