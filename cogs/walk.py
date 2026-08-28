"""
Walking — !walk / !trek + collapse/unconscious + !resuscitate.

MOVEMENT MODEL
    Steps through routing.GRAPH one node/channel at a time, same
    underlying idea as cogs/bus.py's stop-by-stop progression,
    but write access only moves TWICE for the whole trip --
    revoked at the origin, granted at the destination (or frozen
    at the collapse point) -- exactly like a bus passenger's
    access only moves origin -> final destination, never at each
    intermediate stop (see cogs/bus.py's _drop_passengers).

    No tolls, no police checkpoints -- a pedestrian doesn't pay a
    road toll or get pulled over the way a vehicle does.

MESSAGING (see config.py's WALKING block for the full spec)
    1. Departure     -- origin channel, player TAGGED.
    2. Intermediate   -- each channel in between, name only, NO
       hops               mention, short-lived.
    3. Collapse (if)  -- wherever they are when it happens,
                          player TAGGED.
    4. Arrival        -- destination channel, player TAGGED.

STATS
    Every hop drains hunger/thirst/hygiene/happiness based on
    that ROAD SEGMENT'S DISTANCE (config.WALK_STAT_DECAY_PER_KM),
    not just elapsed time -- a 5km hop always costs more than a
    0.5km hop. health and breath are never drained by walking.

COLLAPSE
    If hunger, thirst, health, or happiness hits 0 after a hop's
    decay is applied, the player collapses right there: given the
    Unconscious role, frozen at that location in the database,
    and blocked from every command by bot.py's global check.
    A COLLAPSE_TIMEOUT_SECONDS clock starts -- if nobody
    resuscitates or transports them by ambulance before it fires,
    they're auto-teleported to the hospital and treated there.

RESUSCITATION
    !resuscitate <@player> -- a Medic Staff member standing in
    the SAME channel as the collapsed player can revive them on
    the spot. (Ambulance transport is the other path -- see
    cogs/ambulance.py.)
"""

import asyncio

import discord
from discord.ext import commands

import database
import permissions

from routing import (
    find_route,
    GRAPH,
    NoRouteError,
)

from config import (
    LOCATIONS,
    WALK_MIN_TRAVEL_TIME_SECONDS,
    WALK_MAX_TRAVEL_TIME_SECONDS,
    WALK_SECONDS_PER_KM,
    WALK_MESSAGE_DELETE_DELAY_SECONDS,
    WALK_STAT_DECAY_PER_KM,
    COMPOUND_UNHAPPINESS_ENABLED,
    COMPOUND_UNHAPPINESS_THRESHOLD,
    COMPOUND_UNHAPPINESS_EXTRA_PER_KM,
    COLLAPSE_STATS,
    UNCONSCIOUS_ROLE,
    COLLAPSE_TIMEOUT_SECONDS,
    COLLAPSE_RECOVERY_STAT_VALUE,
    RESUSCITATE_ROLE,
    STARTING_LOCATION,
)


# ================================================================
# LOCATION HELPERS (duplicated, not imported, same pattern
# cogs/police.py / cogs/carpool.py use to avoid circular imports)
# ================================================================

def _name(code: str) -> str:
    loc = LOCATIONS.get(code)
    return loc["name"] if loc else code


def _normalise_code(value: str) -> str:
    return str(value).strip().lower().replace(" ", "-")


def _walk_duration(distance: float) -> float:
    return max(
        WALK_MIN_TRAVEL_TIME_SECONDS,
        min(
            distance * WALK_SECONDS_PER_KM,
            WALK_MAX_TRAVEL_TIME_SECONDS,
        ),
    )


# ================================================================
# ACTIVE WALKS
#
# Module-level so a restart-safe "already walking" check is
# possible, and so cogs/ambulance.py could in principle look a
# player up here later without needing this Cog's instance --
# same pattern cogs/police.py's _active_patrols uses.
#
#     _active_walks[user_id] = True
# ================================================================

_active_walks: dict[int, bool] = {}

# user_id -> asyncio.Task for that player's pending collapse
# timeout. Cancelled the moment they're resuscitated/transported
# so the timeout doesn't fire on someone already saved.
_collapse_timeouts: dict[int, asyncio.Task] = {}


# ================================================================
# MESSAGE HELPERS
# ================================================================

async def _delete_after_delay(msg: discord.Message, delay: float) -> None:

    await asyncio.sleep(delay)

    try:
        await msg.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


def _channel_for(guild: discord.Guild, code: str):

    return permissions.get_channel_for_code(guild, code)


# ================================================================
# STAT DECAY FOR ONE HOP
# ================================================================

def _apply_hop_decay(user_id: int, distance_km: float) -> dict:

    """
    Apply distance-based stat decay for walking one road segment.
    Returns the player's stats AFTER the decay (and after the
    compound-unhappiness top-up, if it fired).
    """

    current = database.get_stats(user_id)

    deltas = {
        stat_name: -(rate * distance_km)
        for stat_name, rate in WALK_STAT_DECAY_PER_KM.items()
    }

    # ----------------------------------------------------------
    # COMPOUND UNHAPPINESS
    #
    # Being hungry or thirsty AND walking is more miserable than
    # walking alone -- a little extra happiness cost on top of
    # the base rate above, checked against stats BEFORE this
    # hop's own decay (i.e. how hungry/thirsty they already were
    # walking into this hop).
    # ----------------------------------------------------------

    if COMPOUND_UNHAPPINESS_ENABLED:

        already_struggling = (
            current["hunger"] <= COMPOUND_UNHAPPINESS_THRESHOLD
            or current["thirst"] <= COMPOUND_UNHAPPINESS_THRESHOLD
        )

        if already_struggling:

            deltas["happiness"] = (
                deltas.get("happiness", 0.0)
                - (COMPOUND_UNHAPPINESS_EXTRA_PER_KM * distance_km)
            )

    return database.adjust_stats(user_id, **deltas)


def _collapsed_stat(stats: dict) -> str | None:

    """
    Return the name of the first collapse-triggering stat sitting
    at (or below) 0, or None if the player is fine.
    """

    for stat_name in COLLAPSE_STATS:

        if stats[stat_name] <= 0:
            return stat_name

    return None


# ================================================================
# COLLAPSE / RECOVERY
# ================================================================

async def _collapse(
    guild: discord.Guild,
    member: discord.Member,
    location_code: str,
    trigger_stat: str,
) -> None:

    """
    Put a player into the collapsed/unconscious state at
    location_code: freeze their database location there, mark
    them unconscious, hand them the Unconscious role, announce
    it in that location's channel (tagged), and start their
    collapse-timeout clock.
    """

    database.update_player(
        member.id,
        location=location_code,
        traveling=0,
    )

    database.set_unconscious(member.id, True)

    role = discord.utils.get(guild.roles, name=UNCONSCIOUS_ROLE)

    if role is not None:

        try:
            await member.add_roles(
                role, reason="Collapsed"
            )

        except (discord.Forbidden, discord.HTTPException):
            pass

    channel = _channel_for(guild, location_code)

    if channel is not None:

        try:
            await channel.send(
                f"\U0001F4A5 **{member.mention}** collapses in the "
                f"street from severe {trigger_stat}."
            )

        except (discord.Forbidden, discord.HTTPException):
            pass

    _start_collapse_timeout(guild, member, location_code)


def _start_collapse_timeout(
    guild: discord.Guild,
    member: discord.Member,
    location_code: str,
) -> None:

    # Cancel any stale timer for this player first (shouldn't
    # normally happen -- you can't collapse again while already
    # unconscious, since every command is blocked -- but this
    # keeps the dict from ever leaking a duplicate task).
    existing = _collapse_timeouts.pop(member.id, None)

    if existing is not None and not existing.done():
        existing.cancel()

    task = asyncio.create_task(
        _collapse_timeout(guild, member, location_code)
    )

    _collapse_timeouts[member.id] = task


async def _collapse_timeout(
    guild: discord.Guild,
    member: discord.Member,
    location_code: str,
) -> None:

    try:
        await asyncio.sleep(COLLAPSE_TIMEOUT_SECONDS)

    except asyncio.CancelledError:
        return

    # Still unconscious after the full window -- nobody got to
    # them in time. Auto-teleport to the hospital and treat them
    # there, same recovery as a manual !resuscitate.
    if not database.is_unconscious(member.id):
        return

    await recover_player(
        guild,
        member,
        new_location="hospital",
        announce_channel_code="hospital",
        reason=(
            f"\U0001F691 {member.mention} was rushed to the "
            f"hospital after collapsing and has been treated."
        ),
    )


async def recover_player(
    guild: discord.Guild,
    member: discord.Member,
    new_location: str,
    announce_channel_code: str | None,
    reason: str,
) -> None:

    """
    Shared recovery path used by !resuscitate (on-the-spot),
    the collapse timeout (auto-teleport), and cogs/ambulance.py
    (hospital drop-off) -- restores whichever collapse stats are
    still low, clears the unconscious flag/role, relocates the
    player if needed, and restores write access there.

    new_location            -- where the player ends up.
    announce_channel_code   -- where to post `reason` (usually
                                the same as new_location). Pass
                                None to skip the announcement.
    """

    stats = database.get_stats(member.id)

    restored = {
        stat_name: COLLAPSE_RECOVERY_STAT_VALUE
        for stat_name in COLLAPSE_STATS
        if stats[stat_name] < COLLAPSE_RECOVERY_STAT_VALUE
    }

    if restored:
        database.set_stats(member.id, **restored)

    old_location = database.get_or_create_player(member.id)["location"]

    database.update_player(
        member.id,
        location=new_location,
        traveling=0,
    )

    database.set_unconscious(member.id, False)

    role = discord.utils.get(guild.roles, name=UNCONSCIOUS_ROLE)

    if role is not None:

        try:
            await member.remove_roles(
                role, reason="Recovered"
            )

        except (discord.Forbidden, discord.HTTPException):
            pass

    await permissions.move_write_access(
        guild, member, old_code=old_location, new_code=new_location
    )

    timeout_task = _collapse_timeouts.pop(member.id, None)

    if timeout_task is not None and not timeout_task.done():
        timeout_task.cancel()

    if announce_channel_code:

        channel = _channel_for(guild, announce_channel_code)

        if channel is not None:

            try:
                await channel.send(reason)

            except (discord.Forbidden, discord.HTTPException):
                pass


# ================================================================
# COG
# ================================================================

class WalkCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------
    # !WALK <destination> (alias !trek)
    # ------------------------------------------------------------

    @commands.command(name="walk", aliases=["trek"])
    async def walk(
        self,
        ctx: commands.Context,
        *,
        destination: str = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not destination:
            await ctx.send("Usage: `!walk <destination>` (or `!trek`)")
            return

        destination = _normalise_code(destination)

        player = database.get_or_create_player(ctx.author.id)

        origin = _normalise_code(player["location"])

        if player["traveling"] or ctx.author.id in _active_walks:
            await ctx.send("You're already on the move.")
            return

        if destination not in LOCATIONS:
            await ctx.send("\u26d4 Unknown destination.")
            return

        if destination == origin:
            await ctx.send("You're already there.")
            return

        try:
            path, _total_distance = find_route(origin, destination)

        except NoRouteError:

            await ctx.send(
                f"No walkable route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )

            return

        # --------------------------------------------------------
        # START THE WALK
        # --------------------------------------------------------

        await permissions.set_write_access(
            ctx.guild, ctx.author, origin, allowed=False
        )

        database.update_player(ctx.author.id, traveling=1)

        _active_walks[ctx.author.id] = True

        origin_channel = _channel_for(ctx.guild, origin)

        if origin_channel is not None:

            await origin_channel.send(
                f"\U0001F6B6 **{ctx.author.mention}** starts walking "
                f"toward {_name(destination)}."
            )

        asyncio.create_task(
            self._run_walk(ctx.guild, ctx.author, path)
        )

    # ------------------------------------------------------------
    # WALK STEPPER
    # ------------------------------------------------------------

    async def _run_walk(
        self,
        guild: discord.Guild,
        member: discord.Member,
        path: list[str],
    ) -> None:

        try:

            for index in range(len(path) - 1):

                current_node = path[index]
                next_node = path[index + 1]

                distance = GRAPH[current_node][next_node]

                await asyncio.sleep(_walk_duration(distance))

                stats_after = _apply_hop_decay(member.id, distance)

                collapsed_on = _collapsed_stat(stats_after)

                if collapsed_on:

                    _active_walks.pop(member.id, None)

                    await _collapse(
                        guild, member, next_node, collapsed_on
                    )

                    return

                is_final_hop = (index + 1) == len(path) - 1

                if not is_final_hop:

                    channel = _channel_for(guild, next_node)

                    if channel is not None:

                        msg = await channel.send(
                            f"\U0001F6B6 {member.display_name} walks past."
                        )

                        asyncio.create_task(
                            _delete_after_delay(
                                msg, WALK_MESSAGE_DELETE_DELAY_SECONDS
                            )
                        )

            # ----------------------------------------------------
            # ARRIVED
            # ----------------------------------------------------

            origin = path[0]
            destination = path[-1]

            database.update_player(
                member.id, location=destination, traveling=0
            )

            await permissions.move_write_access(
                guild, member, old_code=origin, new_code=destination
            )

            _active_walks.pop(member.id, None)

            channel = _channel_for(guild, destination)

            if channel is not None:

                await channel.send(
                    f"\U0001F6B6 **{member.mention}** has arrived."
                )

        except asyncio.CancelledError:
            raise

        except Exception as error:

            print(f"WALK ERROR [{member.id}]: {error}")

            _active_walks.pop(member.id, None)

    # ------------------------------------------------------------
    # !RESUSCITATE <@player>
    # ------------------------------------------------------------

    @commands.command(name="resuscitate")
    async def resuscitate(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        has_role = discord.utils.get(
            ctx.author.roles, name=RESUSCITATE_ROLE
        )

        if not has_role:

            await ctx.send(
                f"\u26d4 You need the **{RESUSCITATE_ROLE}** role to "
                f"resuscitate someone."
            )

            return

        if member is None:

            await ctx.send("Usage: `!resuscitate <@player>`")
            return

        if not database.is_unconscious(member.id):

            await ctx.send(
                f"{member.mention} isn't unconscious."
            )

            return

        target_player = database.get_or_create_player(member.id)

        target_channel = _channel_for(
            ctx.guild, target_player["location"]
        )

        if target_channel is None or ctx.channel.id != target_channel.id:

            await ctx.send(
                f"\u26d4 You need to be at "
                f"**{_name(target_player['location'])}** to "
                f"resuscitate {member.mention}."
            )

            return

        await recover_player(
            ctx.guild,
            member,
            new_location=target_player["location"],
            announce_channel_code=target_player["location"],
            reason=(
                f"\U0001F49A {ctx.author.mention} resuscitates "
                f"{member.mention}. They're back on their feet."
            ),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(WalkCog(bot))
