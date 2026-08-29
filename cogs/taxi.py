"""
Taxi system — standard/premium ride-hailing on top of private
vehicles.

A taxi ride always has exactly ONE destination, shared by the
driver and every rider (no multi-stop reordering like carpool).

A new booking is BROADCAST to every online, idle driver of the
matching tier at once — each pinged in their OWN current-location
channel — and whichever driver accepts first gets the ride.

See the TAXI SYSTEM block in config.py for the full flow.

This file does NOT change how tolls, fuel, or vehicle condition
are calculated — travel.py drives a confirmed taxi ride exactly
like a normal solo trip. This file only handles registration,
booking, matching, queueing, the pickup ETA, and the fare/payout
on arrival.
"""

import asyncio

import discord
from discord.ext import commands

import database
import permissions

from routing import (
    find_route,
    NoRouteError,
)

from config import (
    LOCATIONS,
    VEHICLES,
    TAXI_ELIGIBLE_ROLE,
    TAXI_DRIVER_ROLE,
    TAXI_REGISTRATION_CODE,
    TAXI_MAX_RIDERS,
    TAXI_BASE_FARE_PER_KM,
    TAXI_TIER_MULTIPLIER,
    TAXI_MIN_FARE,
    TAXI_COMPANY_CUT,
    TAXI_COMPANY_VEHICLE,
    TAXI_REQUEST_TIMEOUT_SECONDS,
    TAXI_QUEUE_TIMEOUT_SECONDS,
    TAXI_PICKUP_BUFFER_SECONDS,
    TAXI_MESSAGE_DELETE_DELAY_SECONDS,
    MIN_TRAVEL_TIME_SECONDS,
    MAX_TRAVEL_TIME_SECONDS,
    TRAVEL_SECONDS_PER_KM,
)


# ================================================================
# MODULE-LEVEL STATE
#
# Module-level (not on the Cog instance) so travel.py can call the
# helper functions below without needing this Cog's instance.
#
# A ride request is now BROADCAST to every online, idle driver of
# the matching tier at once (each gets their own ping, in their
# OWN current-location channel) instead of being sent to only the
# single nearest driver. Whoever accepts first gets the ride; the
# ping is pulled from every other notified driver's channel.
#
#     _open_requests[booker_id] = {
#         "booker_id":    int,
#         "rider_ids":    [int, ...],   # includes the booker
#         "destination":  str,
#         "origin":       str,
#         "tier":         str,
#         "fare":         int,          # total, not per-rider
#         "guild_id":     int,
#         "channel_id":   int,   # booker's origin channel
#         "notified": {
#             driver_id: {"channel_id": int, "message_id": int | None},
#             ...
#         },
#         "timeout_task": asyncio.Task,
#     }
#
#     _driver_notice[driver_id] = booker_id
#         # reverse lookup so !taxiaccept / !taxidecline (typed by
#         # the driver, wherever they are) can find the request in
#         # _open_requests without scanning every entry.
#
#     _confirmed_ride[driver_id] = { same shape as an entry above,
#                                     minus notified/timeout_task,
#                                     plus "boarded": bool }
#
#     _queue[tier] = [ entry, entry, ... ]   # FIFO, same shape as
#                                             # a pending request but
#                                             # with no driver yet
# ================================================================

_open_requests: dict[int, dict] = {}
_driver_notice: dict[int, int] = {}
_confirmed_ride: dict[int, dict] = {}
_queue: dict[str, list[dict]] = {}


# ================================================================
# SMALL HELPERS
#
# Duplicated (not imported) from travel.py on purpose, to avoid a
# circular import between travel.py and taxi.py.
# ================================================================

def _name(code: str) -> str:
    loc = database.get_location_data(code)

    return loc["name"] if loc else code


def _normalise_code(value: str) -> str:
    return (
        str(value)
        .strip()
        .lower()
        .replace(" ", "-")
    )


def _normalise_tier(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip().lower()

    return value if value in TAXI_TIER_MULTIPLIER else None


def _is_road_destination(code: str) -> bool:
    location = database.get_location_data(code)

    if location is None:
        return False

    return location.get("zone") != "overseas"


def _has_location_access(
    member: discord.Member,
    code: str
) -> bool:

    location = database.get_location_data(code)

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


def _calculate_fare(distance_km: float, tier: str) -> int:

    fare = (
        distance_km
        * TAXI_BASE_FARE_PER_KM
        * TAXI_TIER_MULTIPLIER[tier]
    )

    return max(TAXI_MIN_FARE[tier], round(fare))


def _travel_duration(distance_km: float) -> float:

    return max(
        MIN_TRAVEL_TIME_SECONDS,
        min(
            distance_km * TRAVEL_SECONDS_PER_KM,
            MAX_TRAVEL_TIME_SECONDS,
        ),
    )


def _pickup_eta_seconds(distance_km: float) -> int:
    """
    How long the driver will take to reach the pickup point, plus
    a small buffer so the quoted time is never a hard deadline.
    """

    from cogs.weather import get_movement_multiplier

    return round(
        _travel_duration(distance_km) * get_movement_multiplier()
    ) + TAXI_PICKUP_BUFFER_SECONDS


def _format_eta(seconds: int) -> str:

    seconds = max(0, round(seconds))

    minutes, secs = divmod(seconds, 60)

    if minutes and secs:
        return f"{minutes}m {secs}s"

    if minutes:
        return f"{minutes}m"

    return f"{secs}s"


async def _send_and_delete(
    ctx: commands.Context,
    content: str
) -> None:

    """
    Sends `content`, deletes it after
    TAXI_MESSAGE_DELETE_DELAY_SECONDS, and immediately deletes the
    triggering command message — keeps the channel from clogging
    up. Used for every transient status/confirmation message.
    """

    msg = await ctx.send(content)

    asyncio.create_task(
        _delete_after_delay(msg, TAXI_MESSAGE_DELETE_DELAY_SECONDS)
    )

    try:
        await ctx.message.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _delete_after_delay(
    msg: discord.Message,
    delay: float
) -> None:

    await asyncio.sleep(delay)

    try:
        await msg.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _send_and_delete_channel(
    channel: discord.abc.Messageable | None,
    content: str
) -> None:

    """
    Same auto-delete pattern as `_send_and_delete`, but for
    messages that don't originate from a command invocation —
    e.g. a notification posted into the booker's channel from a
    command the DRIVER typed somewhere else entirely.
    """

    if channel is None:
        return

    try:
        msg = await channel.send(content)

    except (discord.Forbidden, discord.NotFound):
        return

    asyncio.create_task(
        _delete_after_delay(msg, TAXI_MESSAGE_DELETE_DELAY_SECONDS)
    )


async def _delete_driver_ping(
    guild: discord.Guild,
    notice: dict
) -> None:
    """
    Deletes a single driver's live "you've got a ride request"
    ping — used once that driver accepts/declines, or once the
    whole request is resolved/expired/cancelled and every other
    driver's ping needs to be pulled too.
    """

    channel_id = notice.get("channel_id")
    message_id = notice.get("message_id")

    if channel_id is None or message_id is None:
        return

    channel = guild.get_channel(channel_id)

    if channel is None:
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.delete()

    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def _clear_all_driver_pings(
    guild: discord.Guild,
    entry: dict,
    except_driver_id: int | None = None
) -> None:
    """
    Pulls the ping from every driver who was notified about this
    request, except (optionally) the one who just accepted it —
    so the ride disappears from everyone else's channel the
    moment it's no longer available.
    """

    for driver_id, notice in list(entry.get("notified", {}).items()):

        if driver_id == except_driver_id:
            continue

        await _delete_driver_ping(guild, notice)

        _driver_notice.pop(driver_id, None)


def _driver_current_location(driver_id: int) -> str | None:

    player = database.get_player(driver_id)

    if player is None:
        return None

    if player["traveling"]:
        return None

    return _normalise_code(player["location"])


def _find_available_drivers(
    guild: discord.Guild,
    tier: str,
    origin: str
) -> list[tuple[int, discord.TextChannel, float]]:

    """
    EVERY online, idle, not-already-notified/not-already-booked
    driver of `tier` who has a road route from `origin` AND a
    reachable current-location channel to be pinged in. Returns
    (driver_id, driver_channel, distance_km) tuples, nearest
    first (nearest is only used to order the booker's summary —
    every qualifying driver gets notified, not just the nearest).
    """

    candidates = []

    for row in database.get_online_taxi_drivers(tier):

        driver_id = int(row["user_id"])

        if driver_id in _driver_notice:
            continue

        if driver_id in _confirmed_ride:
            continue

        driver_location = _driver_current_location(driver_id)

        if driver_location is None:
            continue

        driver_channel = permissions.get_channel_for_code(
            guild, driver_location
        )

        if driver_channel is None:
            continue

        try:
            _, distance = find_route(origin, driver_location)

        except NoRouteError:
            continue

        candidates.append((driver_id, driver_channel, distance))

    candidates.sort(key=lambda c: c[2])

    return candidates


def _find_active_entry(booker_id: int):
    """
    Locate a booker's active request, wherever it currently lives.

    Returns one of:
        ("pending", request_dict, None)
        ("queued", entry_dict, None)
        (None, None, None)
    """

    entry = _open_requests.get(booker_id)

    if entry is not None:
        return ("pending", entry, None)

    for entries in _queue.values():

        for queued_entry in entries:

            if queued_entry["booker_id"] == booker_id:
                return ("queued", queued_entry, None)

    return (None, None, None)


# ================================================================
# DISPATCH / QUEUE ENGINE
# ================================================================

async def _broadcast_to_drivers(
    guild: discord.Guild,
    entry: dict,
    drivers: list[tuple[int, discord.TextChannel, float]],
    prefix: str = ""
) -> None:
    """
    Pings EVERY driver in `drivers` — each in their own current
    location channel — and opens the request under the booker's
    id so the first `!taxiaccept` wins. Assumes `drivers` is
    non-empty (callers check via `_find_available_drivers` first).
    """

    timeout_task = asyncio.create_task(
        _expire_request(guild, entry["booker_id"])
    )

    request = {
        "booker_id": entry["booker_id"],
        "rider_ids": entry["rider_ids"],
        "destination": entry["destination"],
        "origin": entry["origin"],
        "tier": entry["tier"],
        "fare": entry["fare"],
        "guild_id": guild.id,
        "channel_id": entry["channel_id"],
        "notified": {},
        "timeout_task": timeout_task,
    }

    _open_requests[entry["booker_id"]] = request

    for driver_id, driver_channel, distance in drivers:

        _driver_notice[driver_id] = entry["booker_id"]

        driver_member = guild.get_member(driver_id)
        driver_mention = (
            driver_member.mention
            if driver_member
            else f"<@{driver_id}>"
        )

        eta_text = _format_eta(_pickup_eta_seconds(distance))
        message_id = None

        try:
            msg = await driver_channel.send(
                f"🚕 New **{entry['tier']}** ride request!\n"
                f"Pickup: **{_name(entry['origin'])}** "
                f"(about **{eta_text}** away)\n"
                f"Destination: **{_name(entry['destination'])}**\n"
                f"Fare: ₦{entry['fare']:,}\n\n"
                f"{driver_mention}, use `!taxiaccept` or "
                f"`!taxidecline` — whichever driver accepts "
                f"first gets the ride."
            )

            message_id = msg.id

        except (discord.Forbidden, discord.NotFound):
            pass

        request["notified"][driver_id] = {
            "channel_id": driver_channel.id,
            "message_id": message_id,
        }

    booker_channel = guild.get_channel(entry["channel_id"])
    driver_count = len(drivers)
    driver_word = "driver" if driver_count == 1 else "drivers"

    await _send_and_delete_channel(
        booker_channel,
        f"{prefix}"
        f"🚕 Ride request sent to {driver_count} nearby "
        f"**{entry['tier']}** taxi {driver_word}.\n"
        f"Destination: **{_name(entry['destination'])}**\n"
        f"Fare: ₦{entry['fare']:,}\n"
        f"Whichever driver accepts first will pick you up — "
        f"you'll be told how long they'll take once they do.\n\n"
        f"<@{entry['booker_id']}>, you can add up to "
        f"{TAXI_MAX_RIDERS - len(entry['rider_ids'])} "
        f"more rider(s) going to the same place with "
        f"`!addrider <@user>` before a driver accepts."
    )


async def _queue_entry(guild: discord.Guild, entry: dict, prefix: str = "") -> None:
    """
    No driver currently free — place `entry` at the back of its
    tier's queue with its own wait-limit timeout, and let the
    booker know.
    """

    entry["timeout_task"] = asyncio.create_task(
        _expire_queue_entry(guild, entry)
    )

    _queue.setdefault(entry["tier"], []).append(entry)

    channel = guild.get_channel(entry["channel_id"])

    await _send_and_delete_channel(
        channel,
        f"{prefix}"
        f"🚕 No **{entry['tier']}** taxi drivers are free right "
        f"now. <@{entry['booker_id']}>, you've been placed in "
        f"the queue and will be auto-matched with the next "
        f"available driver (up to "
        f"{_format_eta(TAXI_QUEUE_TIMEOUT_SECONDS)} wait).\n"
        f"Use `!cancelride` to leave the queue."
    )


async def _dispatch_or_queue(
    guild: discord.Guild,
    entry: dict,
    prefix: str = ""
) -> None:
    """
    Called whenever a booking has no driver yet (or lost the one
    it had) — a fresh !book, every notified driver declining, or
    a whole-request timeout. Broadcasts to everyone currently
    qualifying; falls back to the queue if nobody does.
    """

    drivers = _find_available_drivers(
        guild, entry["tier"], entry["origin"]
    )

    if drivers:

        await _broadcast_to_drivers(
            guild, entry, drivers, prefix=prefix
        )

        return

    await _queue_entry(guild, entry, prefix=prefix)


async def _try_dispatch_queue(guild: discord.Guild, tier: str) -> None:
    """
    Called any time a driver of `tier` becomes free (goes online,
    finishes a trip, declines, times out, or cancels a
    not-yet-boarded ride). Matches as many queued bookers as
    possible, in FIFO order, broadcasting to every currently
    qualifying driver each time.
    """

    queue = _queue.get(tier)

    if not queue:
        return

    while queue:

        entry = queue[0]

        drivers = _find_available_drivers(guild, tier, entry["origin"])

        if not drivers:
            break

        queue.pop(0)

        entry["timeout_task"].cancel()

        await _broadcast_to_drivers(
            guild,
            entry,
            drivers,
            prefix="🔔 A driver is now free for your queued ride!\n\n"
        )


async def _expire_queue_entry(guild: discord.Guild, entry: dict) -> None:

    await asyncio.sleep(TAXI_QUEUE_TIMEOUT_SECONDS)

    queue = _queue.get(entry["tier"])

    if queue is None or entry not in queue:
        return

    queue.remove(entry)

    channel = guild.get_channel(entry["channel_id"])

    await _send_and_delete_channel(
        channel,
        f"⌛ <@{entry['booker_id']}>, no **{entry['tier']}** taxi "
        f"driver became available in time — your queued request "
        f"was cancelled. Try `!book` again."
    )


async def _expire_request(
    guild: discord.Guild,
    booker_id: int
) -> None:

    await asyncio.sleep(TAXI_REQUEST_TIMEOUT_SECONDS)

    request = _open_requests.get(booker_id)

    if request is None:
        return

    _open_requests.pop(booker_id, None)

    await _clear_all_driver_pings(guild, request)

    channel = guild.get_channel(request["channel_id"])

    await _send_and_delete_channel(
        channel,
        f"⌛ Taxi request for <@{request['booker_id']}> expired "
        f"— no driver accepted in time. Looking for another "
        f"driver..."
    )

    await _dispatch_or_queue(guild, request)


# ================================================================
# FUNCTIONS CALLED FROM travel.py
# ================================================================

def peek_confirmed_ride(driver_id: int) -> dict | None:
    """
    Look at this driver's confirmed, BOARDED ride WITHOUT popping
    it. Used by travel.py early in !drive to override whatever
    destination the driver typed with the ride's actual
    destination, before any of the normal destination checks run.
    """

    ride = _confirmed_ride.get(driver_id)

    if ride is None or not ride["boarded"]:
        return None

    return ride


def take_confirmed_ride(driver_id: int) -> dict | None:
    """
    Pop and return this driver's confirmed, BOARDED taxi ride.

    Called once, right when !drive actually starts a trip.
    Returns None if the driver has no ride, or has one but
    hasn't run !taxipickup yet — in that case !drive must behave
    as a completely normal, unrestricted solo trip, so the driver
    can freely reposition to the pickup point.
    """

    ride = _confirmed_ride.get(driver_id)

    if ride is None or not ride["boarded"]:
        return None

    return _confirmed_ride.pop(driver_id)


def requeue_ride(driver_id: int, ride: dict) -> None:
    """
    Put a confirmed ride back if !drive fails AFTER already
    having popped it (e.g. not enough fuel), so the driver
    doesn't lose the booking.
    """

    _confirmed_ride[driver_id] = ride


async def lock_in_riders(
    guild: discord.Guild,
    ride: dict,
    origin: str
) -> None:
    """
    Called right when the trip actually starts. Revokes every
    rider's write access at the origin and marks them as
    travelling, same as the driver.
    """

    for rider_id in ride["rider_ids"]:

        member = guild.get_member(rider_id)

        if member is not None:

            await permissions.set_write_access(
                guild,
                member,
                origin,
                allowed=False
            )

        database.update_player(
            rider_id,
            traveling=1
        )


async def handle_taxi_arrival(
    guild: discord.Guild,
    driver: discord.Member,
    ride: dict
) -> str | None:
    """
    Called by travel.py once the trip's destination is reached.

    Charges the fare to the booker, pays the driver their cut of
    it, gives every rider (booker + added riders) arrival
    location/channel access exactly like the driver gets, and
    then — since the driver is idle and online again — tries to
    match them against anyone waiting in that tier's queue.

    Returns a short summary line to append to the arrival
    message, or None.
    """

    destination = ride["destination"]
    tier = ride["tier"]
    fare = ride["fare"]

    # ------------------------------------------------------------
    # CHARGE THE BOOKER
    #
    # The fare is a single trip-level amount (doesn't multiply
    # with rider count) and is billed to whoever booked the ride.
    # ------------------------------------------------------------

    booker = database.get_player(ride["booker_id"])

    if booker is not None:

        database.update_player(
            ride["booker_id"],
            balance=max(0, booker["balance"] - fare)
        )

    # ------------------------------------------------------------
    # PAY THE DRIVER (fare minus company cut)
    # ------------------------------------------------------------

    company_cut = round(fare * TAXI_COMPANY_CUT[tier])
    driver_payout = fare - company_cut

    driver_row = database.get_player(driver.id)

    if driver_row is not None:

        database.update_player(
            driver.id,
            balance=driver_row["balance"] + driver_payout
        )

    # ------------------------------------------------------------
    # DROP OFF EVERY RIDER
    # ------------------------------------------------------------

    for rider_id in ride["rider_ids"]:

        database.update_player(
            rider_id,
            location=destination,
            traveling=0,
        )

        member = guild.get_member(rider_id)

        if member is None:
            continue

        await permissions.set_write_access(
            guild,
            member,
            destination,
            allowed=True
        )

    # ------------------------------------------------------------
    # DRIVER IS FREE AGAIN — try to clear the queue for this tier
    # ------------------------------------------------------------

    driver_taxi = database.get_taxi_driver(driver.id)

    if driver_taxi is not None and driver_taxi["online"]:

        asyncio.create_task(_try_dispatch_queue(guild, tier))

    # ------------------------------------------------------------
    # TAG EVERY RIDER ON ARRIVAL — same passenger-tagging
    # mechanism used for private-car drop-offs (carpool.py's
    # "Dropped off @rider" message), instead of only mentioning
    # the driver + fare line.
    # ------------------------------------------------------------

    rider_mentions = ", ".join(
        f"<@{rider_id}>" for rider_id in ride["rider_ids"]
    )

    return (
        f"🧍 Dropped off {rider_mentions} — "
        f"goodbye, see you later! 👋\n"
        f"💰 Fare: ₦{fare:,} "
        f"(driver payout ₦{driver_payout:,}, "
        f"company cut ₦{company_cut:,})."
    )


# ================================================================
# COG
# ================================================================

class TaxiCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ============================================================
    # !BECOMETAXIDRIVER
    # ============================================================

    @commands.command(name="becometaxidriver")
    async def becometaxidriver(
        self,
        ctx: commands.Context,
        tier: str = None
    ):

        tier = _normalise_tier(tier)

        if tier is None:

            await _send_and_delete(
                ctx,
                "Usage: `!becometaxidriver standard` or "
                "`!becometaxidriver premium`"
            )

            return

        player = database.get_or_create_player(ctx.author.id)

        origin = _normalise_code(player["location"])

        if origin != TAXI_REGISTRATION_CODE:

            await _send_and_delete(
                ctx,
                f"You need to be at "
                f"{_name(TAXI_REGISTRATION_CODE)} to register "
                f"as a taxi driver."
            )

            return

        expected_channel = LOCATIONS[
            TAXI_REGISTRATION_CODE
        ]["channel"]

        if ctx.channel.name != expected_channel:

            await _send_and_delete(
                ctx,
                f"You need to be in #{expected_channel} to "
                f"register as a taxi driver."
            )

            return

        eligible_role = discord.utils.get(
            ctx.author.roles,
            name=TAXI_ELIGIBLE_ROLE
        )

        if eligible_role is None:

            await _send_and_delete(
                ctx,
                f"⛔ You need the **{TAXI_ELIGIBLE_ROLE}** role "
                f"to become a taxi driver."
            )

            return

        existing = database.get_taxi_driver(ctx.author.id)

        if existing is not None:

            await _send_and_delete(
                ctx,
                f"You're already registered as a "
                f"**{existing['tier']}** taxi driver."
            )

            return

        database.register_taxi_driver(ctx.author.id, tier)

        # --------------------------------------------------------
        # HAND OVER THE COMPANY CAR
        #
        # Must go through add_vehicle() — the same path dispatch.py
        # and police.py already use for their company vehicles —
        # so it lands in the player's owned-vehicle list. That list
        # (not the legacy flat columns) is what !vehicle/!vehicles
        # actually reads; add_vehicle() mirrors the selection onto
        # the flat columns for us, so !map/!refuel/!fixcar keep
        # working too.
        # --------------------------------------------------------

        company_vehicle_name = TAXI_COMPANY_VEHICLE[tier]
        vehicle_cfg = VEHICLES[company_vehicle_name]

        database.add_vehicle(
            ctx.author.id,
            name=company_vehicle_name,
            vehicle_type="taxi",
            location=TAXI_REGISTRATION_CODE,
            condition=vehicle_cfg.get("condition", 100),
            fuel=vehicle_cfg["fuel_capacity"],
            select=True,
        )

        # --------------------------------------------------------
        # SWAP ROLES
        # --------------------------------------------------------

        try:
            await ctx.author.remove_roles(
                eligible_role,
                reason="Became a taxi driver"
            )

        except discord.Forbidden:
            pass

        driver_role = discord.utils.get(
            ctx.guild.roles,
            name=TAXI_DRIVER_ROLE
        )

        if driver_role is not None:

            try:
                await ctx.author.add_roles(
                    driver_role,
                    reason="Became a taxi driver"
                )

            except discord.Forbidden:
                pass

        await ctx.send(
            f"🚕 {ctx.author.mention} is now a "
            f"**{tier}** taxi driver! The company has handed "
            f"you a **{company_vehicle_name}** — check "
            f"`!vehicle` any time. Use `!taxistart` to go "
            f"online and start receiving ride requests."
        )

    # ============================================================
    # !TAXISTART / !TAXISTOP
    # ============================================================

    @commands.command(name="taxistart")
    async def taxistart(self, ctx: commands.Context):

        driver = database.get_taxi_driver(ctx.author.id)

        if driver is None:

            await _send_and_delete(
                ctx,
                "You're not a registered taxi driver. Use "
                "`!becometaxidriver standard` or "
                "`!becometaxidriver premium` first."
            )

            return

        player = database.get_or_create_player(ctx.author.id)

        if not player["vehicle"]:

            await _send_and_delete(
                ctx,
                "You need a vehicle before going online."
            )

            return

        database.set_taxi_online(ctx.author.id, True)

        await _send_and_delete(
            ctx,
            f"🟢 {ctx.author.mention} is now online and "
            f"bookable as a **{driver['tier']}** taxi. You'll "
            f"stay online until you run `!taxistop`."
        )

        asyncio.create_task(
            _try_dispatch_queue(ctx.guild, driver["tier"])
        )

    @commands.command(name="taxistop")
    async def taxistop(self, ctx: commands.Context):

        driver = database.get_taxi_driver(ctx.author.id)

        if driver is None:

            await _send_and_delete(
                ctx,
                "You're not a registered taxi driver."
            )

            return

        database.set_taxi_online(ctx.author.id, False)

        await _send_and_delete(
            ctx,
            f"🔴 {ctx.author.mention} is now offline."
        )

    # ============================================================
    # !BOOK
    # ============================================================

    @commands.command(name="book")
    async def book(
        self,
        ctx: commands.Context,
        tier: str = None,
        *,
        destination: str = None
    ):

        tier = _normalise_tier(tier)

        if tier is None or not destination:

            await _send_and_delete(
                ctx,
                "Usage: `!book standard <destination>` or "
                "`!book premium <destination>`"
            )

            return

        destination = _normalise_code(destination)

        booker = ctx.author

        booker_player = database.get_or_create_player(booker.id)

        if booker_player["traveling"]:

            await _send_and_delete(
                ctx,
                "You're already travelling."
            )

            return

        kind, _existing, _ = _find_active_entry(booker.id)

        if kind is not None:

            await _send_and_delete(
                ctx,
                "You already have a pending taxi request."
            )

            return

        origin = _normalise_code(booker_player["location"])

        expected_channel = (
            database.get_location_data(origin) or {}
        ).get("channel")

        if ctx.channel.name != expected_channel:

            await _send_and_delete(
                ctx,
                f"You are not at {_name(origin)}'s channel "
                f"right now."
            )

            return

        # --------------------------------------------------------
        # DESTINATION VALIDITY
        # --------------------------------------------------------

        if not database.location_exists(destination):

            await _send_and_delete(
                ctx,
                f"⛔ `{destination}` is not a valid location."
            )

            return

        if not _is_road_destination(destination):

            await _send_and_delete(
                ctx,
                f"⛔ **{_name(destination)}** is not accessible "
                f"by road."
            )

            return

        if destination == origin:

            await _send_and_delete(
                ctx,
                f"You are already at {_name(destination)}."
            )

            return

        if not _has_location_access(booker, destination):

            await _send_and_delete(
                ctx,
                f"⛔ You don't have access to "
                f"**{_name(destination)}**."
            )

            return

        # --------------------------------------------------------
        # FARE — based on the RIDE distance (origin to
        # destination), not the driver's distance to pick you up.
        # --------------------------------------------------------

        try:
            _, ride_distance = find_route(origin, destination)

        except NoRouteError:

            await _send_and_delete(
                ctx,
                f"No road route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )

            return

        fare = _calculate_fare(ride_distance, tier)

        entry = {
            "booker_id": booker.id,
            "rider_ids": [booker.id],
            "destination": destination,
            "origin": origin,
            "tier": tier,
            "fare": fare,
            "guild_id": ctx.guild.id,
            "channel_id": ctx.channel.id,
        }

        try:
            await ctx.message.delete()

        except (discord.Forbidden, discord.NotFound):
            pass

        # --------------------------------------------------------
        # BROADCAST TO EVERY AVAILABLE DRIVER, OR QUEUE
        # --------------------------------------------------------

        await _dispatch_or_queue(ctx.guild, entry)

    # ============================================================
    # !ADDRIDER (booker only, before driver accepts/declines,
    # and while still queued)
    # ============================================================

    @commands.command(name="addrider")
    async def addrider(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if member is None:

            await _send_and_delete(
                ctx,
                "Usage: `!addrider <@user>`"
            )

            return

        kind, request, _driver_id = _find_active_entry(ctx.author.id)

        if request is None:

            await _send_and_delete(
                ctx,
                "You don't have a pending taxi request to add "
                "riders to."
            )

            return

        if member.id in request["rider_ids"]:

            await _send_and_delete(
                ctx,
                f"{member.mention} is already on this ride."
            )

            return

        if len(request["rider_ids"]) >= TAXI_MAX_RIDERS:

            await _send_and_delete(
                ctx,
                f"This taxi is already full "
                f"({TAXI_MAX_RIDERS} riders max)."
            )

            return

        added_player = database.get_or_create_player(member.id)

        if _normalise_code(added_player["location"]) != request["origin"]:

            await _send_and_delete(
                ctx,
                f"{member.mention} is not at "
                f"{_name(request['origin'])} right now."
            )

            return

        if added_player["traveling"]:

            await _send_and_delete(
                ctx,
                f"{member.mention} is already travelling."
            )

            return

        if not _has_location_access(member, request["destination"]):

            await _send_and_delete(
                ctx,
                f"⛔ {member.mention} does not have access to "
                f"**{_name(request['destination'])}**."
            )

            return

        request["rider_ids"].append(member.id)

        await _send_and_delete(
            ctx,
            f"✅ {member.mention} added to the ride to "
            f"**{_name(request['destination'])}** "
            f"({len(request['rider_ids'])}/{TAXI_MAX_RIDERS})."
        )

    # ============================================================
    # !TAXIACCEPT
    # ============================================================

    @commands.command(name="taxiaccept")
    async def taxiaccept(self, ctx: commands.Context):

        booker_id = _driver_notice.get(ctx.author.id)

        if booker_id is None:

            await _send_and_delete(
                ctx,
                "You have no pending taxi request."
            )

            return

        request = _open_requests.get(booker_id)
        notice = request["notified"].get(ctx.author.id) if request else None

        if request is None or notice is None:

            # Another driver already claimed it, or it expired,
            # right as this command was typed.
            _driver_notice.pop(ctx.author.id, None)

            await _send_and_delete(
                ctx,
                "That ride is no longer available — someone "
                "else likely got there first."
            )

            return

        # Claim it immediately, before doing anything else, so a
        # second driver's !taxiaccept arriving a moment later
        # can't also grab it.
        _open_requests.pop(booker_id, None)
        request["timeout_task"].cancel()

        # Pull this driver's own ping, then every OTHER notified
        # driver's ping — the ride is no longer up for grabs.
        await _delete_driver_ping(ctx.guild, notice)
        await _clear_all_driver_pings(
            ctx.guild, request, except_driver_id=ctx.author.id
        )
        _driver_notice.pop(ctx.author.id, None)

        _confirmed_ride[ctx.author.id] = {
            "booker_id": request["booker_id"],
            "rider_ids": request["rider_ids"],
            "origin": request["origin"],
            "destination": request["destination"],
            "tier": request["tier"],
            "fare": request["fare"],
            "boarded": False,
        }

        # --------------------------------------------------------
        # ETA TO PICKUP — recomputed now, from the driver's
        # CURRENT location, since time may have passed since the
        # ping went out.
        # --------------------------------------------------------

        eta_text = "shortly"

        driver_location = _driver_current_location(ctx.author.id)

        if driver_location is not None:

            try:
                _, distance = find_route(
                    request["origin"], driver_location
                )

                eta_text = _format_eta(
                    _pickup_eta_seconds(distance)
                )

            except NoRouteError:
                pass

        rider_mentions = ", ".join(
            f"<@{rider_id}>" for rider_id in request["rider_ids"]
        )

        # Notify the booker/riders in THEIR channel — the driver
        # may have typed !taxiaccept somewhere else entirely.
        booker_channel = ctx.guild.get_channel(request["channel_id"])

        await _send_and_delete_channel(
            booker_channel,
            f"✅ {ctx.author.mention} accepted the ride "
            f"({rider_mentions}) to "
            f"**{_name(request['destination'])}**.\n"
            f"Driver is about **{eta_text}** away."
        )

        await _send_and_delete(
            ctx,
            f"✅ Ride accepted. Drive yourself to "
            f"**{_name(request['origin'])}** to pick up "
            f"{rider_mentions}, then use `!taxipickup`."
        )

    # ============================================================
    # !TAXIPICKUP (driver, only once physically at the origin)
    # ============================================================

    @commands.command(name="taxipickup")
    async def taxipickup(self, ctx: commands.Context):

        ride = _confirmed_ride.get(ctx.author.id)

        if ride is None:

            await _send_and_delete(
                ctx,
                "You don't have a confirmed ride to pick up."
            )

            return

        if ride["boarded"]:

            await _send_and_delete(
                ctx,
                "You've already picked up this ride — use "
                f"`!drive {ride['destination']}` to head out."
            )

            return

        driver_player = database.get_or_create_player(
            ctx.author.id
        )

        if driver_player["traveling"]:

            await _send_and_delete(
                ctx,
                "You can't pick up passengers mid-trip."
            )

            return

        driver_location = _normalise_code(
            driver_player["location"]
        )

        if driver_location != ride["origin"]:

            await _send_and_delete(
                ctx,
                f"You're not at {_name(ride['origin'])} yet — "
                f"that's where your passenger(s) are waiting."
            )

            return

        expected_channel = (
            database.get_location_data(ride["origin"]) or {}
        ).get("channel")

        if ctx.channel.name != expected_channel:

            await _send_and_delete(
                ctx,
                f"You need to be in #{expected_channel} to "
                f"pick up your passengers."
            )

            return

        # --------------------------------------------------------
        # MAKE SURE EVERYONE'S STILL THERE AND FREE TO GO
        # --------------------------------------------------------

        missing = []

        for rider_id in ride["rider_ids"]:

            rider_player = database.get_player(rider_id)

            if (
                rider_player is None
                or _normalise_code(rider_player["location"])
                != ride["origin"]
                or rider_player["traveling"]
            ):

                missing.append(rider_id)

        if missing:

            mentions = ", ".join(
                f"<@{rider_id}>" for rider_id in missing
            )

            await _send_and_delete(
                ctx,
                f"⛔ Can't pick up — {mentions} is no longer "
                f"at {_name(ride['origin'])} or is travelling. "
                f"Use `!cancelride` if the trip is off."
            )

            return

        ride["boarded"] = True

        rider_mentions = ", ".join(
            f"<@{rider_id}>" for rider_id in ride["rider_ids"]
        )

        await ctx.send(
            f"🧍‍♂️🧍‍♀️ Picked up {rider_mentions}. "
            f"Use `!drive {ride['destination']}` to head to "
            f"**{_name(ride['destination'])}**."
        )

    # ============================================================
    # !CANCELRIDE
    #
    # Driver-side safety valve for a confirmed-but-not-boarded
    # ride, AND the booker's way to back out of a pending request
    # or leave the queue ("stay in queue or end").
    # ============================================================

    @commands.command(name="cancelride")
    async def cancelride(self, ctx: commands.Context):

        # --------------------------------------------------------
        # DRIVER: confirmed, not yet boarded
        # --------------------------------------------------------

        ride = _confirmed_ride.get(ctx.author.id)

        if ride is not None and not ride["boarded"]:

            _confirmed_ride.pop(ctx.author.id, None)

            await _send_and_delete(
                ctx,
                "Cancelled the confirmed ride "
                "(not yet picked up)."
            )

            driver = database.get_taxi_driver(ctx.author.id)

            if driver is not None and driver["online"]:

                asyncio.create_task(
                    _try_dispatch_queue(ctx.guild, ride["tier"])
                )

            return

        # --------------------------------------------------------
        # BOOKER: pending (broadcast to drivers) or queued
        # --------------------------------------------------------

        kind, entry, _unused = _find_active_entry(ctx.author.id)

        if kind == "pending":

            entry["timeout_task"].cancel()

            _open_requests.pop(ctx.author.id, None)

            await _clear_all_driver_pings(ctx.guild, entry)

            await _send_and_delete(
                ctx,
                "Cancelled the pending ride request."
            )

            return

        if kind == "queued":

            entry["timeout_task"].cancel()

            queue = _queue.get(entry["tier"])

            if queue is not None and entry in queue:
                queue.remove(entry)

            await _send_and_delete(
                ctx,
                "Left the queue — ride request cancelled."
            )

            return

        await _send_and_delete(
            ctx,
            "You have no cancellable ride right now."
        )

    # ============================================================
    # !TAXIDECLINE
    # ============================================================

    @commands.command(name="taxidecline")
    async def taxidecline(self, ctx: commands.Context):

        booker_id = _driver_notice.get(ctx.author.id)

        if booker_id is None:

            await _send_and_delete(
                ctx,
                "You have no pending taxi request."
            )

            return

        request = _open_requests.get(booker_id)
        notice = (
            request["notified"].pop(ctx.author.id, None)
            if request is not None
            else None
        )

        _driver_notice.pop(ctx.author.id, None)

        if request is None or notice is None:

            await _send_and_delete(
                ctx,
                "That ride is no longer available."
            )

            return

        await _delete_driver_ping(ctx.guild, notice)

        await _send_and_delete(
            ctx,
            f"❌ Declined the ride request from "
            f"<@{request['booker_id']}>."
        )

        # Only look for another driver / fall back to the queue
        # once EVERY currently-notified driver has declined —
        # otherwise the other pings already out there are still
        # live and one of them may still accept.
        if request["notified"]:
            return

        request["timeout_task"].cancel()

        _open_requests.pop(booker_id, None)

        booker_channel = ctx.guild.get_channel(request["channel_id"])

        await _send_and_delete_channel(
            booker_channel,
            f"❌ Every nearby **{request['tier']}** taxi driver "
            f"declined — looking for another..."
        )

        await _dispatch_or_queue(ctx.guild, request)


async def setup(bot: commands.Bot):
    await bot.add_cog(TaxiCog(bot))
