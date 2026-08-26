"""
Taxi system — standard/premium ride-hailing on top of private
vehicles.

A taxi ride always has exactly ONE destination, shared by the
driver and every rider (no multi-stop reordering like carpool).

See the TAXI SYSTEM block in config.py for the full flow.

This file does NOT change how tolls, fuel, or vehicle condition
are calculated — travel.py drives a confirmed taxi ride exactly
like a normal solo trip. This file only handles registration,
booking, matching, and the fare/payout on arrival.
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
    TAXI_ELIGIBLE_ROLE,
    TAXI_DRIVER_ROLE,
    TAXI_REGISTRATION_CODE,
    TAXI_MAX_RIDERS,
    TAXI_BASE_FARE_PER_KM,
    TAXI_TIER_MULTIPLIER,
    TAXI_COMPANY_CUT,
    TAXI_REQUEST_TIMEOUT_SECONDS,
    TAXI_MESSAGE_DELETE_DELAY_SECONDS,
)


# ================================================================
# MODULE-LEVEL STATE
#
# Module-level (not on the Cog instance) so travel.py can call the
# helper functions below without needing this Cog's instance.
#
#     _pending_ride_requests[driver_id] = {
#         "booker_id":    int,
#         "rider_ids":    [int, ...],   # includes the booker
#         "destination":  str,
#         "origin":       str,
#         "tier":         str,
#         "fare":         int,          # total, not per-rider
#         "guild_id":     int,
#         "timeout_task": asyncio.Task,
#     }
#
#     _confirmed_ride[driver_id] = { same shape as above, minus
#                                     timeout_task }
# ================================================================

_pending_ride_requests: dict[int, dict] = {}
_confirmed_ride: dict[int, dict] = {}


# ================================================================
# SMALL HELPERS
#
# Duplicated (not imported) from travel.py on purpose, to avoid a
# circular import between travel.py and taxi.py.
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


def _normalise_tier(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip().lower()

    return value if value in TAXI_TIER_MULTIPLIER else None


def _is_road_destination(code: str) -> bool:
    location = LOCATIONS.get(code)

    if location is None:
        return False

    return location.get("zone") != "overseas"


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


def _calculate_fare(distance_km: float, tier: str) -> int:

    fare = (
        distance_km
        * TAXI_BASE_FARE_PER_KM
        * TAXI_TIER_MULTIPLIER[tier]
    )

    return max(1, round(fare))


async def _send_and_delete(
    ctx: commands.Context,
    content: str
) -> None:

    """
    Sends `content`, deletes it after
    TAXI_MESSAGE_DELETE_DELAY_SECONDS, and immediately deletes the
    triggering command message — keeps the channel from clogging
    up, same pattern requested for the driver flow.
    """

    msg = await ctx.send(content)

    async def _delete_later():

        await asyncio.sleep(
            TAXI_MESSAGE_DELETE_DELAY_SECONDS
        )

        try:
            await msg.delete()

        except (discord.Forbidden, discord.NotFound):
            pass

    asyncio.create_task(_delete_later())

    try:
        await ctx.message.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


def _driver_current_location(driver_id: int) -> str | None:

    player = database.get_player(driver_id)

    if player is None:
        return None

    if player["traveling"]:
        return None

    return _normalise_code(player["location"])


def _nearest_online_driver(
    tier: str,
    origin: str
) -> tuple[int, float] | None:

    """
    Among online, idle, unbooked drivers of `tier`, find the one
    with the shortest road distance from `origin`. Returns
    (driver_id, distance_km) or None if nobody qualifies.
    """

    best_driver = None
    best_distance = None

    for row in database.get_online_taxi_drivers(tier):

        driver_id = int(row["user_id"])

        if driver_id in _pending_ride_requests:
            continue

        if driver_id in _confirmed_ride:
            continue

        driver_location = _driver_current_location(driver_id)

        if driver_location is None:
            continue

        try:
            _, distance = find_route(origin, driver_location)

        except NoRouteError:
            continue

        if best_distance is None or distance < best_distance:
            best_driver = driver_id
            best_distance = distance

    if best_driver is None:
        return None

    return (best_driver, best_distance)


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
    it, and gives every rider (booker + added riders) arrival
    location/channel access exactly like the driver gets.

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

    return (
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

            await ctx.send(
                "Usage: `!becometaxidriver standard` or "
                "`!becometaxidriver premium`"
            )

            return

        player = database.get_or_create_player(ctx.author.id)

        origin = _normalise_code(player["location"])

        if origin != TAXI_REGISTRATION_CODE:

            await ctx.send(
                f"You need to be at "
                f"{_name(TAXI_REGISTRATION_CODE)} to register "
                f"as a taxi driver."
            )

            return

        expected_channel = LOCATIONS[
            TAXI_REGISTRATION_CODE
        ]["channel"]

        if ctx.channel.name != expected_channel:

            await ctx.send(
                f"You need to be in #{expected_channel} to "
                f"register as a taxi driver."
            )

            return

        eligible_role = discord.utils.get(
            ctx.author.roles,
            name=TAXI_ELIGIBLE_ROLE
        )

        if eligible_role is None:

            await ctx.send(
                f"⛔ You need the **{TAXI_ELIGIBLE_ROLE}** role "
                f"to become a taxi driver."
            )

            return

        existing = database.get_taxi_driver(ctx.author.id)

        if existing is not None:

            await ctx.send(
                f"You're already registered as a "
                f"**{existing['tier']}** taxi driver."
            )

            return

        database.register_taxi_driver(ctx.author.id, tier)

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
            f"**{tier}** taxi driver! Use `!taxistart` to go "
            f"online and start receiving ride requests."
        )

    # ============================================================
    # !TAXISTART / !TAXISTOP
    # ============================================================

    @commands.command(name="taxistart")
    async def taxistart(self, ctx: commands.Context):

        driver = database.get_taxi_driver(ctx.author.id)

        if driver is None:

            await ctx.send(
                "You're not a registered taxi driver. Use "
                "`!becometaxidriver standard` or "
                "`!becometaxidriver premium` first."
            )

            return

        player = database.get_or_create_player(ctx.author.id)

        if not player["vehicle"]:

            await ctx.send(
                "You need to own a vehicle before going online."
            )

            return

        database.set_taxi_online(ctx.author.id, True)

        await ctx.send(
            f"🟢 {ctx.author.mention} is now online and "
            f"bookable as a **{driver['tier']}** taxi."
        )

    @commands.command(name="taxistop")
    async def taxistop(self, ctx: commands.Context):

        driver = database.get_taxi_driver(ctx.author.id)

        if driver is None:

            await ctx.send(
                "You're not a registered taxi driver."
            )

            return

        database.set_taxi_online(ctx.author.id, False)

        await ctx.send(
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

            await ctx.send(
                "Usage: `!book standard <destination>` or "
                "`!book premium <destination>`"
            )

            return

        destination = _normalise_code(destination)

        booker = ctx.author

        booker_player = database.get_or_create_player(booker.id)

        if booker_player["traveling"]:

            await ctx.send(
                "You're already travelling."
            )

            return

        for request in _pending_ride_requests.values():

            if booker.id in request["rider_ids"]:

                await ctx.send(
                    "You already have a pending taxi request."
                )

                return

        origin = _normalise_code(booker_player["location"])

        expected_channel = LOCATIONS.get(
            origin, {}
        ).get("channel")

        if ctx.channel.name != expected_channel:

            await ctx.send(
                f"You are not at {_name(origin)}'s channel "
                f"right now."
            )

            return

        # --------------------------------------------------------
        # DESTINATION VALIDITY
        # --------------------------------------------------------

        if destination not in LOCATIONS:

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
                f"You are already at {_name(destination)}."
            )

            return

        if not _has_location_access(booker, destination):

            await ctx.send(
                f"⛔ You don't have access to "
                f"**{_name(destination)}**."
            )

            return

        # --------------------------------------------------------
        # FIND NEAREST ONLINE DRIVER
        # --------------------------------------------------------

        match = _nearest_online_driver(tier, origin)

        if match is None:

            await ctx.send(
                f"No **{tier}** taxi drivers are online right "
                f"now. Try again shortly."
            )

            return

        driver_id, driver_distance = match

        # --------------------------------------------------------
        # FARE — based on the RIDE distance (origin to
        # destination), not the driver's distance to pick you up.
        # --------------------------------------------------------

        try:
            _, ride_distance = find_route(origin, destination)

        except NoRouteError:

            await ctx.send(
                f"No road route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )

            return

        fare = _calculate_fare(ride_distance, tier)

        timeout_task = asyncio.create_task(
            self._expire_request(driver_id, ctx)
        )

        _pending_ride_requests[driver_id] = {
            "booker_id": booker.id,
            "rider_ids": [booker.id],
            "destination": destination,
            "origin": origin,
            "tier": tier,
            "fare": fare,
            "guild_id": ctx.guild.id,
            "timeout_task": timeout_task,
        }

        driver_member = ctx.guild.get_member(driver_id)
        driver_mention = (
            driver_member.mention
            if driver_member
            else f"<@{driver_id}>"
        )

        await ctx.send(
            f"🚕 Ride request sent to a nearby **{tier}** taxi "
            f"driver ({driver_mention}).\n"
            f"Destination: **{_name(destination)}**\n"
            f"Fare: ₦{fare:,}\n\n"
            f"{booker.mention}, you can add up to "
            f"{TAXI_MAX_RIDERS - 1} more rider(s) going to the "
            f"same place with `!addrider <@user>` before the "
            f"driver responds."
        )

    # ============================================================
    # !ADDRIDER (booker only, before driver accepts/declines)
    # ============================================================

    @commands.command(name="addrider")
    async def addrider(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if member is None:

            await ctx.send(
                "Usage: `!addrider <@user>`"
            )

            return

        request = None

        for candidate in _pending_ride_requests.values():

            if candidate["booker_id"] == ctx.author.id:
                request = candidate
                break

        if request is None:

            await ctx.send(
                "You don't have a pending taxi request to add "
                "riders to."
            )

            return

        if member.id in request["rider_ids"]:

            await ctx.send(
                f"{member.mention} is already on this ride."
            )

            return

        if len(request["rider_ids"]) >= TAXI_MAX_RIDERS:

            await ctx.send(
                f"This taxi is already full "
                f"({TAXI_MAX_RIDERS} riders max)."
            )

            return

        added_player = database.get_or_create_player(member.id)

        if _normalise_code(added_player["location"]) != request["origin"]:

            await ctx.send(
                f"{member.mention} is not at "
                f"{_name(request['origin'])} right now."
            )

            return

        if added_player["traveling"]:

            await ctx.send(
                f"{member.mention} is already travelling."
            )

            return

        if not _has_location_access(member, request["destination"]):

            await ctx.send(
                f"⛔ {member.mention} does not have access to "
                f"**{_name(request['destination'])}**."
            )

            return

        request["rider_ids"].append(member.id)

        await ctx.send(
            f"✅ {member.mention} added to the ride to "
            f"**{_name(request['destination'])}** "
            f"({len(request['rider_ids'])}/{TAXI_MAX_RIDERS})."
        )

    # ============================================================
    # REQUEST TIMEOUT
    # ============================================================

    async def _expire_request(
        self,
        driver_id: int,
        ctx: commands.Context
    ) -> None:

        await asyncio.sleep(TAXI_REQUEST_TIMEOUT_SECONDS)

        request = _pending_ride_requests.get(driver_id)

        if request is None:
            return

        _pending_ride_requests.pop(driver_id, None)

        try:
            await ctx.send(
                f"⌛ Taxi request for <@{request['booker_id']}> "
                f"expired — no response from the driver."
            )

        except (discord.Forbidden, discord.NotFound):
            pass

    # ============================================================
    # !TAXIACCEPT
    # ============================================================

    @commands.command(name="taxiaccept")
    async def taxiaccept(self, ctx: commands.Context):

        request = _pending_ride_requests.get(ctx.author.id)

        if request is None:

            await ctx.send(
                "You have no pending taxi request."
            )

            return

        request["timeout_task"].cancel()

        _pending_ride_requests.pop(ctx.author.id, None)

        _confirmed_ride[ctx.author.id] = {
            "booker_id": request["booker_id"],
            "rider_ids": request["rider_ids"],
            "origin": request["origin"],
            "destination": request["destination"],
            "tier": request["tier"],
            "fare": request["fare"],
            "boarded": False,
        }

        rider_mentions = ", ".join(
            f"<@{rider_id}>" for rider_id in request["rider_ids"]
        )

        await _send_and_delete(
            ctx,
            f"✅ {ctx.author.mention} accepted the ride "
            f"({rider_mentions}) to "
            f"**{_name(request['destination'])}**.\n"
            f"Drive yourself to **{_name(request['origin'])}** "
            f"to pick them up, then use `!taxipickup`."
        )

    # ============================================================
    # !TAXIPICKUP (driver, only once physically at the origin)
    # ============================================================

    @commands.command(name="taxipickup")
    async def taxipickup(self, ctx: commands.Context):

        ride = _confirmed_ride.get(ctx.author.id)

        if ride is None:

            await ctx.send(
                "You don't have a confirmed ride to pick up."
            )

            return

        if ride["boarded"]:

            await ctx.send(
                "You've already picked up this ride — use "
                f"`!drive {ride['destination']}` to head out."
            )

            return

        driver_player = database.get_or_create_player(
            ctx.author.id
        )

        if driver_player["traveling"]:

            await ctx.send(
                "You can't pick up passengers mid-trip."
            )

            return

        driver_location = _normalise_code(
            driver_player["location"]
        )

        if driver_location != ride["origin"]:

            await ctx.send(
                f"You're not at {_name(ride['origin'])} yet — "
                f"that's where your passenger(s) are waiting."
            )

            return

        expected_channel = LOCATIONS.get(
            ride["origin"], {}
        ).get("channel")

        if ctx.channel.name != expected_channel:

            await ctx.send(
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

            await ctx.send(
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
    # !CANCELRIDE (driver-side safety valve, mirrors carpool)
    # ============================================================

    @commands.command(name="cancelride")
    async def cancelride(self, ctx: commands.Context):

        request = _pending_ride_requests.get(ctx.author.id)

        if request is not None:

            request["timeout_task"].cancel()

            _pending_ride_requests.pop(ctx.author.id, None)

            await ctx.send(
                "Cancelled the pending ride request."
            )

            return

        ride = _confirmed_ride.get(ctx.author.id)

        if ride is not None and not ride["boarded"]:

            _confirmed_ride.pop(ctx.author.id, None)

            await ctx.send(
                "Cancelled the confirmed ride "
                "(not yet picked up)."
            )

            return

        await ctx.send(
            "You have no cancellable ride right now."
        )

    # ============================================================
    # !TAXIDECLINE
    # ============================================================

    @commands.command(name="taxidecline")
    async def taxidecline(self, ctx: commands.Context):

        request = _pending_ride_requests.get(ctx.author.id)

        if request is None:

            await ctx.send(
                "You have no pending taxi request."
            )

            return

        request["timeout_task"].cancel()

        _pending_ride_requests.pop(ctx.author.id, None)

        await _send_and_delete(
            ctx,
            f"❌ {ctx.author.mention} declined the ride "
            f"request from <@{request['booker_id']}>."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TaxiCog(bot))
