"""
Carpool system for private vehicles.

Lets a private vehicle owner queue up passengers before starting a
trip with !drive. Each queued passenger gets their own drop-off
destination, and must accept the ride before they're actually added.

FLOW:

    1. Driver: !dropoffuser <@user> <destination>
       -> only works if both driver and target are currently at the
          same location (same channel).
       -> only works if the vehicle still has a free passenger seat.
       -> the target gets a confirmation request, NOT an instant add.

    2. Target: !accept  (or !decline)
       -> only on !accept is the passenger actually queued.
       -> requests expire automatically after
          CARPOOL_CONFIRM_TIMEOUT_SECONDS.

    3. Driver: !drive <own-destination>
       -> travel.py calls take_confirmed_stops() to pick up whatever
          got confirmed, and build_multi_leg_route() to turn that
          into a single path: origin -> nearest stop -> next nearest
          stop -> ... -> driver's own destination.
       -> travel.py then drives that combined path exactly like a
          normal trip, calling handle_arrival() at every step so the
          right passenger gets dropped (and gets channel access) at
          the right node.

This file does NOT change how tolls, fuel, or vehicle condition are
calculated — those remain a single lump sum over the FULL combined
distance, exactly like a normal !drive trip. It only decides ROUTE
ORDER and handles the mid-trip passenger drop-off events.
"""

import asyncio

import discord
from discord.ext import commands

import database
import permissions

from routing import (
    find_route,
    tolls_on_route,
    NoRouteError,
)

from config import (
    LOCATIONS,
    VEHICLES,
    TRAVEL_MESSAGE_DELETE_DELAY_SECONDS,
    CARPOOL_CONFIRM_TIMEOUT_SECONDS,
    CARPOOL_DEFAULT_PASSENGER_CAPACITY,
)


# ================================================================
# MODULE-LEVEL QUEUE STATE
#
# These are intentionally module-level (not on the Cog instance)
# so travel.py can call the helper functions below without needing
# a reference to this Cog's instance.
#
#     _pending_requests[target_user_id] = {
#         "driver_id":  int,
#         "destination": str,
#         "origin":      str,
#         "guild_id":    int,
#         "channel_id":  int,
#         "timeout_task": asyncio.Task,
#     }
#
#     _confirmed_queue[driver_id] = [
#         {"user_id": int, "destination": str},
#         ...
#     ]
# ================================================================

_pending_requests: dict[int, dict] = {}
_confirmed_queue: dict[int, list] = {}


# ================================================================
# SMALL LOCATION HELPERS
#
# Duplicated (not imported) from travel.py on purpose, to avoid a
# circular import between travel.py and carpool.py.
# ================================================================

def _name(code: str) -> str:
    loc = LOCATIONS.get(code)

    return loc["name"] if loc else code


def _normalise_code(value: str) -> str:
    return (
        str(value)
        .strip()
        .lower()
        .replace(" ", "-")
    )


def _is_road_destination(code: str) -> bool:

    location = LOCATIONS.get(code)

    if location is None:
        return False

    if location.get("zone") == "overseas":
        return False

    return True


def _has_location_access(
    member: discord.Member,
    code: str
) -> bool:

    location = LOCATIONS.get(code)

    if location is None:
        return False

    required_roles = location.get("roles")

    if not required_roles:
        return True

    member_roles = {
        role.name.strip().lower()
        for role in member.roles
    }

    for required_role in required_roles:

        if required_role.strip().lower() in member_roles:
            return True

    return False


def _vehicle_capacity(vehicle_name: str | None) -> int:

    vehicle_cfg = VEHICLES.get(
        vehicle_name,
        {}
    )

    return vehicle_cfg.get(
        "passenger_capacity",
        CARPOOL_DEFAULT_PASSENGER_CAPACITY
    )


def _driver_seat_count(driver_id: int) -> int:
    """Confirmed + still-pending seats currently held for a driver."""

    confirmed = len(
        _confirmed_queue.get(driver_id, [])
    )

    pending = sum(
        1
        for request in _pending_requests.values()
        if request["driver_id"] == driver_id
    )

    return confirmed + pending


# ================================================================
# FUNCTIONS CALLED FROM travel.py
# ================================================================

def take_confirmed_stops(driver_id: int) -> list:
    """
    Pop and return this driver's confirmed passenger queue.

    Called once, right when !drive actually starts a trip. Returns
    [] if the driver has no confirmed passengers (a completely
    normal, solo !drive trip).
    """

    return _confirmed_queue.pop(
        driver_id,
        []
    )


def requeue_stops(driver_id: int, stops: list) -> None:
    """
    Put confirmed stops back if !drive fails AFTER already having
    popped them (e.g. no route to the driver's own destination), so
    the driver doesn't lose their confirmed passengers.
    """

    if not stops:
        return

    _confirmed_queue.setdefault(
        driver_id,
        []
    )

    _confirmed_queue[driver_id] = (
        stops
        + _confirmed_queue[driver_id]
    )


def build_multi_leg_route(
    origin: str,
    stops: list,
    final_destination: str
):
    """
    Build one combined path: origin -> (stops, nearest-first) ->
    final_destination.

    Returns:

        (
            full_path,      # list[str] combined node path
            total_distance, # float, sum of every leg
            pending_tolls,  # list[str], NOT deduped across legs —
                             # crossing the same toll zone twice
                             # means paying it twice
            stop_markers,   # list of {"index", "user_id",
                             # "destination"} — index into full_path
                             # where that passenger is dropped
        )

    Raises NoRouteError if any leg has no valid road route.
    """

    remaining = list(stops)
    ordered = []
    current = origin

    # ------------------------------------------------------------
    # GREEDY NEAREST-FIRST ORDERING
    #
    # At each step, drop off whichever remaining queued passenger
    # is closest from the car's current position.
    # ------------------------------------------------------------

    while remaining:

        best = None
        best_distance = None

        for stop in remaining:

            try:
                _, distance = find_route(
                    current,
                    stop["destination"]
                )

            except NoRouteError:
                continue

            if (
                best_distance is None
                or distance < best_distance
            ):
                best = stop
                best_distance = distance

        if best is None:
            # None of the remaining stops are reachable from here.
            # This shouldn't happen in a connected road network,
            # but fail loudly rather than looping forever.
            raise NoRouteError(
                "No road route to one or more queued passengers "
                "from the current position."
            )

        ordered.append(best)
        remaining.remove(best)
        current = best["destination"]

    # ------------------------------------------------------------
    # BUILD THE COMBINED PATH, LEG BY LEG
    # ------------------------------------------------------------

    full_path = [origin]
    total_distance = 0.0
    pending_tolls: list[str] = []
    stop_markers = []

    current = origin

    for stop in ordered:

        leg_path, leg_distance = find_route(
            current,
            stop["destination"]
        )

        # Per-leg toll check, deliberately NOT merged/deduped
        # across legs — see module docstring.
        pending_tolls.extend(
            tolls_on_route(leg_path)
        )

        full_path.extend(
            leg_path[1:]
        )

        total_distance += leg_distance

        stop_markers.append({
            "index": len(full_path) - 1,
            "user_id": stop["user_id"],
            "destination": stop["destination"],
        })

        current = stop["destination"]

    # ------------------------------------------------------------
    # FINAL LEG — DRIVER'S OWN DESTINATION
    # ------------------------------------------------------------

    leg_path, leg_distance = find_route(
        current,
        final_destination
    )

    pending_tolls.extend(
        tolls_on_route(leg_path)
    )

    full_path.extend(
        leg_path[1:]
    )

    total_distance += leg_distance

    return (
        full_path,
        total_distance,
        pending_tolls,
        stop_markers,
    )


async def lock_in_passengers(
    guild: discord.Guild,
    stops: list,
    origin: str
) -> None:
    """
    Called right when the trip actually starts (mirrors the
    existing "revoke origin write access" step for the driver).

    Revokes each confirmed passenger's write access at the origin
    and marks them as traveling, same as the driver.
    """

    for stop in stops:

        member = guild.get_member(
            stop["user_id"]
        )

        if member is not None:

            await permissions.set_write_access(
                guild,
                member,
                origin,
                allowed=False
            )

        database.update_player(
            stop["user_id"],
            traveling=1
        )


async def handle_arrival(
    guild: discord.Guild,
    journey: dict,
    current_index: int
) -> None:
    """
    Called by travel.py's tick loop every time current_index
    advances. No-op unless the car has just reached one (or
    more) queued passengers' drop-off point.

    BUGFIX: when two or more passengers share the same drop-off
    index (e.g. two riders going to the same destination, or a
    greedy-router leg that adds zero new path nodes), ALL of
    them must be processed here — not just the first match.
    Previously this used `for candidate in stops: ... break`,
    which grabbed only one marker per call; since travel.py's
    tick loop only ever calls handle_arrival() once per index,
    every other passenger sharing that index was silently
    dropped and never reached their destination channel.
    """

    stops = journey.get("stops")

    if not stops:
        return

    # Collect EVERY marker at this index, not just the first.
    markers = [
        candidate
        for candidate in stops
        if candidate["index"] == current_index
    ]

    if not markers:
        return

    # Remove them all immediately so none can ever fire twice.
    for marker in markers:
        stops.remove(marker)

    dropped_mentions = []

    for marker in markers:

        destination = marker["destination"]

        member = guild.get_member(
            marker["user_id"]
        )

        # ----------------------------------------------------
        # UPDATE THE PASSENGER'S OWN STATE
        # ----------------------------------------------------

        database.update_player(
            marker["user_id"],
            location=destination,
            traveling=0,
        )

        if member is None:
            continue

        # ----------------------------------------------------
        # GRANT CHANNEL ACCESS ONLY TO THIS PASSENGER
        # ----------------------------------------------------

        await permissions.set_write_access(
            guild,
            member,
            destination,
            allowed=True
        )

        dropped_mentions.append(
            (member, destination)
        )

    if not dropped_mentions:
        return

    # ----------------------------------------------------------
    # ONE COMBINED DROP-OFF MESSAGE PER DESTINATION CHANNEL,
    # TAGGING EVERY PASSENGER DROPPED AT THIS STOP.
    # ----------------------------------------------------------

    by_destination: dict[str, list[discord.Member]] = {}

    for member, destination in dropped_mentions:
        by_destination.setdefault(destination, []).append(member)

    for destination, members in by_destination.items():

        dest_channel = permissions.get_channel_for_code(
            guild,
            destination
        )

        if dest_channel is None:
            continue

        mentions = ", ".join(
            member.mention for member in members
        )

        drop_msg = await dest_channel.send(
            f"🚗 Dropped off {mentions} — "
            f"goodbye, see you later! 👋"
        )

        async def _delete_later(msg=drop_msg):

            await asyncio.sleep(
                TRAVEL_MESSAGE_DELETE_DELAY_SECONDS
            )

            try:
                await msg.delete()

            except (
                discord.Forbidden,
                discord.NotFound
            ):
                pass

        asyncio.create_task(
            _delete_later()
        )


# ================================================================
# CARPOOL COG
# ================================================================

class CarpoolCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------
    # SELF-DELETING SEND (matches travel.py's cleanup pattern)
    # ------------------------------------------------------------

    async def _send_temp(
        self,
        ctx: commands.Context,
        content: str
    ) -> None:

        msg = await ctx.send(content)

        async def _delete_later():

            await asyncio.sleep(
                TRAVEL_MESSAGE_DELETE_DELAY_SECONDS
            )

            try:
                await msg.delete()

            except (
                discord.Forbidden,
                discord.NotFound
            ):
                pass

        asyncio.create_task(
            _delete_later()
        )

        try:
            await ctx.message.delete()

        except (
            discord.Forbidden,
            discord.NotFound
        ):
            pass

    # ============================================================
    # !DROPOFFUSER
    # ============================================================

    @commands.command(name="dropoffuser")
    async def dropoffuser(
        self,
        ctx: commands.Context,
        member: discord.Member = None,
        *,
        destination: str = None
    ):

        if member is None or not destination:

            await ctx.send(
                "Usage: `!dropoffuser <@user> <destination>`"
            )

            return

        destination = _normalise_code(
            destination
        )

        driver = ctx.author

        # --------------------------------------------------------
        # DRIVER MUST OWN A VEHICLE
        # --------------------------------------------------------

        player = database.get_or_create_player(
            driver.id
        )

        if not player["vehicle"]:

            await ctx.send(
                "You don't own a vehicle."
            )

            return

        if player["traveling"]:

            await ctx.send(
                "You're already travelling — you can't queue "
                "passengers mid-trip."
            )

            return

        origin = _normalise_code(
            player["location"]
        )

        # --------------------------------------------------------
        # MUST BE PHYSICALLY IN THAT LOCATION'S CHANNEL
        # --------------------------------------------------------

        expected_channel = LOCATIONS.get(
            origin,
            {}
        ).get("channel")

        if ctx.channel.name != expected_channel:

            await ctx.send(
                f"You are not at {_name(origin)}'s channel "
                f"right now."
            )

            return

        # --------------------------------------------------------
        # CAN'T QUEUE YOURSELF
        # --------------------------------------------------------

        if member.id == driver.id:

            await ctx.send(
                "You can't drop yourself off."
            )

            return

        # --------------------------------------------------------
        # TARGET MUST BE AT THE SAME LOCATION (SAME CHANNEL)
        # --------------------------------------------------------

        target_player = database.get_or_create_player(
            member.id
        )

        if _normalise_code(target_player["location"]) != origin:

            await ctx.send(
                f"{member.mention} is not at "
                f"{_name(origin)} right now."
            )

            return

        if target_player["traveling"]:

            await ctx.send(
                f"{member.mention} is already travelling."
            )

            return

        if member.id in _pending_requests:

            await ctx.send(
                f"{member.mention} already has a pending "
                f"pickup request."
            )

            return

        # --------------------------------------------------------
        # DESTINATION VALIDITY
        # --------------------------------------------------------

        if not database.location_exists(destination):

            await ctx.send(
                f"⛔ `{destination}` is not a valid location."
            )

            return

        if not _is_road_destination(destination):

            await ctx.send(
                f"⛔ **{_name(destination)}** is not accessible "
                f"by road."
            )

            return

        if destination == origin:

            await ctx.send(
                f"{member.mention} is already at "
                f"{_name(destination)}."
            )

            return

        if not _has_location_access(member, destination):

            await ctx.send(
                f"⛔ {member.mention} does not have access to "
                f"**{_name(destination)}**."
            )

            return

        # --------------------------------------------------------
        # CAPACITY
        # --------------------------------------------------------

        capacity = _vehicle_capacity(
            player["vehicle"]
        )

        if _driver_seat_count(driver.id) >= capacity:

            await ctx.send(
                f"Your {player['vehicle']} can only carry "
                f"{capacity} passenger(s) at once."
            )

            return

        # --------------------------------------------------------
        # CREATE PENDING REQUEST
        # --------------------------------------------------------

        timeout_task = asyncio.create_task(
            self._expire_request(
                member.id,
                ctx
            )
        )

        _pending_requests[member.id] = {
            "driver_id": driver.id,
            "destination": destination,
            "origin": origin,
            "guild_id": ctx.guild.id,
            "channel_id": ctx.channel.id,
            "timeout_task": timeout_task,
        }

        await ctx.send(
            f"🚕 {driver.mention} wants to drop "
            f"{member.mention} off at "
            f"**{_name(destination)}**.\n"
            f"{member.mention}, type `!accept` to confirm or "
            f"`!decline` to refuse "
            f"(expires in "
            f"{CARPOOL_CONFIRM_TIMEOUT_SECONDS}s)."
        )

    # ============================================================
    # REQUEST TIMEOUT
    # ============================================================

    async def _expire_request(
        self,
        target_id: int,
        ctx: commands.Context
    ) -> None:

        await asyncio.sleep(
            CARPOOL_CONFIRM_TIMEOUT_SECONDS
        )

        request = _pending_requests.get(
            target_id
        )

        if request is None:
            return

        _pending_requests.pop(
            target_id,
            None
        )

        try:
            await ctx.send(
                f"⌛ Pickup request for <@{target_id}> expired."
            )

        except (
            discord.Forbidden,
            discord.NotFound
        ):
            pass

    # ============================================================
    # !ACCEPT
    # ============================================================

    @commands.command(name="accept")
    async def accept(
        self,
        ctx: commands.Context
    ):

        request = _pending_requests.get(
            ctx.author.id
        )

        if request is None:

            await ctx.send(
                "You have no pending pickup request."
            )

            return

        request["timeout_task"].cancel()

        _pending_requests.pop(
            ctx.author.id,
            None
        )

        _confirmed_queue.setdefault(
            request["driver_id"],
            []
        ).append({
            "user_id": ctx.author.id,
            "destination": request["destination"],
        })

        await ctx.send(
            f"✅ {ctx.author.mention} confirmed the ride to "
            f"**{_name(request['destination'])}**."
        )

    # ============================================================
    # !DECLINE
    # ============================================================

    @commands.command(name="decline")
    async def decline(
        self,
        ctx: commands.Context
    ):

        request = _pending_requests.get(
            ctx.author.id
        )

        if request is None:

            await ctx.send(
                "You have no pending pickup request."
            )

            return

        request["timeout_task"].cancel()

        _pending_requests.pop(
            ctx.author.id,
            None
        )

        await ctx.send(
            f"❌ {ctx.author.mention} declined the ride."
        )

    # ============================================================
    # !CANCELPICKUP (driver-side safety valve)
    # ============================================================

    @commands.command(name="cancelpickup")
    async def cancelpickup(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if member is None:

            await ctx.send(
                "Usage: `!cancelpickup <@user>`"
            )

            return

        # Pending (not yet accepted) request from this driver.
        request = _pending_requests.get(
            member.id
        )

        if (
            request is not None
            and request["driver_id"] == ctx.author.id
        ):

            request["timeout_task"].cancel()

            _pending_requests.pop(
                member.id,
                None
            )

            await ctx.send(
                f"Cancelled the pending pickup request for "
                f"{member.mention}."
            )

            return

        # Already-confirmed passenger.
        queue = _confirmed_queue.get(
            ctx.author.id,
            []
        )

        for entry in queue:

            if entry["user_id"] == member.id:

                queue.remove(entry)

                await ctx.send(
                    f"Removed {member.mention} from your "
                    f"passenger queue."
                )

                return

        await ctx.send(
            f"{member.mention} is not in your passenger queue."
        )


# ================================================================
# DISCORD EXTENSION SETUP
# ================================================================

async def setup(bot: commands.Bot):
    await bot.add_cog(CarpoolCog(bot))
