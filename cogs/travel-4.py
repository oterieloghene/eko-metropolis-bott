import asyncio

import discord
from discord.ext import commands

import database
import permissions

from cogs import carpool
from cogs import taxi
from cogs import mechanic
from cogs import dispatch
from cogs import police

from routing import (
    find_route,
    tolls_on_route,
    NoRouteError,
)

from config import (
    LOCATIONS,
    VEHICLES,
    TOLL_ZONES,
    TRAVEL_MESSAGE_DELETE_DELAY_SECONDS,
    CONDITION_LOSS_PER_KM,
    MIN_TRAVEL_TIME_SECONDS,
    MAX_TRAVEL_TIME_SECONDS,
    TRAVEL_SECONDS_PER_KM,
    DISPATCH_BICYCLE_TIME_MULTIPLIER,
)


# ================================================================
# LOCATION HELPERS
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


# ================================================================
# ROAD DESTINATION CHECK
# ================================================================

def _is_road_destination(code: str) -> bool:
    """
    Determine whether a location can be reached by road.

    Any registered location can be a road destination unless
    it belongs to the overseas zone.

    Therefore:

        Ghetto             -> road
        Makoko             -> road
        Ajegunle           -> road
        Face Me I Face You -> road
        Farmland           -> road

    But:

        Dubai              -> NOT road
        Maldives           -> NOT road
    """

    code = _normalise_code(code)

    location = database.get_location_data(code)

    if location is None:
        return False

    if location.get("zone") == "overseas":
        return False

    return True


# ================================================================
# LOCATION ACCESS
# ================================================================

def _has_location_access(
    member: discord.Member,
    code: str
) -> tuple[bool, list[str]]:

    location = database.get_location_data(code)

    if location is None:
        return False, []

    required_roles = location.get("roles")

    # ------------------------------------------------------------
    # FREE LOCATION
    #
    # roles=None means EVERYONE can access it.
    # ------------------------------------------------------------

    if not required_roles:
        return True, []

    member_roles = {
        role.name.strip().lower()
        for role in member.roles
    }

    for required_role in required_roles:

        if required_role.strip().lower() in member_roles:
            return True, required_roles

    return False, required_roles


# ================================================================
# ACCESS DENIED MESSAGE
# ================================================================

async def _send_access_denied(
    ctx: commands.Context,
    destination: str
) -> None:

    location = database.get_location_data(destination)

    if location is None:
        await ctx.send(
            "⛔ That location does not exist."
        )
        return

    required_roles = location.get("roles") or []

    role_text = ", ".join(
        f"`{role}`"
        for role in required_roles
    )

    await ctx.send(
        f"⛔ **Access denied.**\n\n"
        f"📍 **{location['name']}** is a restricted location.\n"
        f"🔐 Required role: {role_text}\n\n"
        f"You cannot travel to this location because you "
        f"do not have the required access."
    )


# ================================================================
# TRAVEL TIME
# ================================================================

def _travel_duration(distance: float) -> float:

    return max(
        MIN_TRAVEL_TIME_SECONDS,
        min(
            distance * TRAVEL_SECONDS_PER_KM,
            MAX_TRAVEL_TIME_SECONDS,
        ),
    )


# ================================================================
# TRAVEL COG
# ================================================================

class TravelCog(commands.Cog):

    def __init__(self, bot: commands.Bot):

        self.bot = bot

        # user_id -> journey information
        self.active_journeys: dict[int, dict] = {}

    # ============================================================
    # !ROUTE
    # ============================================================

    @commands.command(name="route")
    async def route(
        self,
        ctx: commands.Context,
        *,
        destination: str = None
    ):

        if not destination:

            await ctx.send(
                "Usage: `!route <destination>`"
            )

            return

        destination = _normalise_code(
            destination
        )

        player = database.get_or_create_player(
            ctx.author.id
        )

        origin = _normalise_code(
            player["location"]
        )

        # --------------------------------------------------------
        # DESTINATION EXISTS
        # --------------------------------------------------------

        if not database.location_exists(destination):

            await ctx.send(
                f"⛔ `{destination}` is not a valid location."
            )

            return

        # --------------------------------------------------------
        # OVERSEAS LOCATIONS ARE NOT ROAD ROUTES
        # --------------------------------------------------------

        if not _is_road_destination(destination):

            await ctx.send(
                f"⛔ **{_name(destination)}** "
                f"is not accessible by road."
            )

            return

        # --------------------------------------------------------
        # DESTINATION ACCESS
        # --------------------------------------------------------

        has_access, _ = _has_location_access(
            ctx.author,
            destination
        )

        if not has_access:

            await _send_access_denied(
                ctx,
                destination
            )

            return

        # --------------------------------------------------------
        # ALREADY THERE
        # --------------------------------------------------------

        if destination == origin:

            await ctx.send(
                "You are already there."
            )

            return

        # --------------------------------------------------------
        # FIND ROUTE
        # --------------------------------------------------------

        try:

            path, distance = find_route(
                origin,
                destination
            )

        except NoRouteError:

            await ctx.send(
                f"No road route exists between "
                f"{_name(origin)} and "
                f"{_name(destination)}."
            )

            return

        # --------------------------------------------------------
        # TOLLS
        # --------------------------------------------------------

        tolls = tolls_on_route(path)

        readable_path = " → ".join(
            _name(code)
            for code in path
        )

        toll_text = (
            ", ".join(
                TOLL_ZONES[t]["name"]
                for t in tolls
            )
            if tolls
            else "None"
        )

        travel_time = _travel_duration(
            distance
        )

        # --------------------------------------------------------
        # EMBED
        # --------------------------------------------------------

        embed = discord.Embed(
            title="🗺️ Route Preview",
            color=discord.Color.green()
        )

        embed.add_field(
            name="From",
            value=_name(origin),
            inline=True
        )

        embed.add_field(
            name="To",
            value=_name(destination),
            inline=True
        )

        embed.add_field(
            name="Distance",
            value=f"{distance:.1f} km",
            inline=True
        )

        embed.add_field(
            name="Travel Time",
            value=f"{travel_time:.0f} seconds",
            inline=True
        )

        embed.add_field(
            name="Path",
            value=readable_path,
            inline=False
        )

        embed.add_field(
            name="Toll Gates",
            value=toll_text,
            inline=False
        )

        await ctx.send(
            embed=embed
        )

    # ============================================================
    # !DRIVE
    # ============================================================

    @commands.command(name="drive")
    async def drive(
        self,
        ctx: commands.Context,
        *,
        destination: str = None
    ):

        if not destination:

            await ctx.send(
                "Usage: `!drive <destination>`"
            )

            return

        destination = _normalise_code(
            destination
        )

        player = database.get_or_create_player(
            ctx.author.id
        )

        origin = _normalise_code(
            player["location"]
        )

        # --------------------------------------------------------
        # TAXI: A BOARDED RIDE OVERRIDES WHATEVER DESTINATION WAS
        # TYPED
        #
        # !taxipickup already confirmed the driver is at the
        # ride's origin with every rider aboard — the destination
        # was locked in back when the ride was booked, so the
        # driver just needs to say "go", not retype it correctly.
        # --------------------------------------------------------

        taxi_ride = taxi.peek_confirmed_ride(
            ctx.author.id
        )

        if taxi_ride is not None:

            destination = taxi_ride["destination"]

        # --------------------------------------------------------
        # MECHANIC: A CONFIRMED JOB OVERRIDES WHATEVER DESTINATION
        # WAS TYPED
        #
        # Same idea as the taxi override above — !mechanicaccept
        # already committed the mechanic to a specific vehicle;
        # they just need to say "go", and !drive takes them
        # straight to wherever that vehicle is parked.
        # --------------------------------------------------------

        mechanic_job = mechanic.peek_confirmed_job(
            ctx.author.id
        )

        if mechanic_job is not None:

            destination = mechanic_job["vehicle_code"]

        # --------------------------------------------------------
        # DISPATCH: BOARDED ORDERS OVERRIDE WHATEVER DESTINATION
        # WAS TYPED
        #
        # Same idea as the taxi/mechanic overrides above —
        # !dispatchpickup already boarded every order this rider
        # is carrying; the typed destination is ignored and the
        # combined delivery route (built below via
        # dispatch.build_delivery_route) decides where they
        # actually end up, same as taxi.
        # --------------------------------------------------------

        dispatch_orders = dispatch.peek_confirmed_orders(
            ctx.author.id
        )

        if dispatch_orders:

            destination = dispatch_orders[-1]["destination"]

        # ========================================================
        # IMPORTANT:
        #
        # EVERYTHING BELOW THIS POINT THAT CAN DENY THE JOURNEY
        # HAPPENS BEFORE traveling=1.
        #
        # Therefore a failed journey NEVER locks the player out
        # of their current location.
        # ========================================================

        # --------------------------------------------------------
        # ALREADY TRAVELLING
        # --------------------------------------------------------

        if player["traveling"]:

            await ctx.send(
                "You are already travelling."
            )

            return

        # --------------------------------------------------------
        # EXISTING ACTIVE JOURNEY SAFETY CHECK
        # --------------------------------------------------------

        if ctx.author.id in self.active_journeys:

            await ctx.send(
                "You already have an active journey."
            )

            return

        # --------------------------------------------------------
        # VEHICLE
        # --------------------------------------------------------

        if not player["vehicle"]:

            await ctx.send(
                "You don't own a vehicle. "
                "Buy one at the Vehicle Dealership first."
            )

            return

        # --------------------------------------------------------
        # VEHICLE CONDITION
        # --------------------------------------------------------

        if player["vehicle_condition"] <= 0:

            await ctx.send(
                f"Your {player['vehicle']} is too damaged "
                f"to drive (0 condition). Get it repaired "
                f"at {LOCATIONS['repair']['name']} first."
            )

            return

        # --------------------------------------------------------
        # VEHICLE MUST BE WHERE THE PLAYER IS
        #
        # Owning a vehicle isn't enough — you can only drive it
        # from wherever it's actually parked. If the player got
        # here some other way (bus, being dropped off, etc.)
        # while the car stayed behind, they can't drive it.
        # --------------------------------------------------------

        if _normalise_code(player["vehicle_location"]) != origin:

            vehicle_loc_name = _name(
                player["vehicle_location"]
            )

            await ctx.send(
                f"⛔ Your {player['vehicle']} is at "
                f"**{vehicle_loc_name}**, not here. "
                f"Go get it before you can drive."
            )

            return

        # --------------------------------------------------------
        # CURRENT LOCATION MUST EXIST
        # --------------------------------------------------------

        if not database.location_exists(origin):

            await ctx.send(
                "⛔ Your current location is invalid. "
                "Please contact an administrator."
            )

            return

        # --------------------------------------------------------
        # MUST ACTUALLY BE IN CURRENT LOCATION CHANNEL
        # --------------------------------------------------------

        expected_channel = database.get_location_data(
            origin
        )["channel"]

        if ctx.channel.name != expected_channel:

            await ctx.send(
                f"You are not physically at "
                f"{_name(origin)}'s channel right now — "
                f"go to #{expected_channel} to drive from there."
            )

            return

        # --------------------------------------------------------
        # DESTINATION EXISTS
        # --------------------------------------------------------

        if not database.location_exists(destination):

            await ctx.send(
                f"⛔ `{destination}` is not a valid location."
            )

            return

        # --------------------------------------------------------
        # OVERSEAS CHECK
        #
        # Dubai and Maldives remain completely outside the road
        # routing system.
        # --------------------------------------------------------

        if not _is_road_destination(destination):

            await ctx.send(
                f"⛔ **{_name(destination)}** "
                f"is not accessible by road."
            )

            return

        # --------------------------------------------------------
        # DESTINATION ACCESS CHECK
        #
        # THIS MUST HAPPEN BEFORE THE JOURNEY STARTS.
        #
        # Example:
        #
        # Player without Streethustler
        # !drive farmland
        #
        # -> Access denied
        # -> NO traveling=1
        # -> NO active journey
        # -> NO permission changes
        # -> NO channel lock
        # -> Player stays where they are.
        # --------------------------------------------------------

        has_access, _ = _has_location_access(
            ctx.author,
            destination
        )

        # ----------------------------------------------------
        # TEMPORARY ACCESS FOR A TAXI DRIVER'S DROP-OFF
        #
        # A taxi driver doesn't need the destination's own role
        # to make this one trip — the RIDER'S access was already
        # checked back in taxi.py's !book (a restricted
        # destination only ever reaches here because a booker who
        # actually holds that role booked it). Letting the driver
        # through here is the "temporary access": it's the same
        # ordinary write-access grant every arrival gets, nothing
        # more — it's revoked the moment the driver leaves again,
        # by the standard origin-lock at the top of the NEXT
        # !drive (further up in this same command), exactly like
        # for anyone else. It never permanently unlocks the
        # location for the driver, and only applies to this taxi
        # ride.
        # ----------------------------------------------------

        if not has_access and taxi_ride is not None:
            has_access = True

        if not has_access:

            await _send_access_denied(
                ctx,
                destination
            )

            return

        # --------------------------------------------------------
        # ALREADY THERE
        # --------------------------------------------------------

        if destination == origin:

            await ctx.send(
                "You are already there."
            )

            return

        # --------------------------------------------------------
        # CARPOOL: PICK UP ANY CONFIRMED QUEUED PASSENGERS
        #
        # Skipped entirely for a boarded taxi ride — a driver
        # can't run both at once.
        #
        # If the driver has neither, this is a completely normal,
        # unchanged, solo !drive trip.
        # --------------------------------------------------------

        carpool_stops = (
            []
            if (taxi_ride is not None or dispatch_orders)
            else carpool.take_confirmed_stops(ctx.author.id)
        )

        # --------------------------------------------------------
        # FIND ROUTE
        # --------------------------------------------------------

        carpool_tolls = None
        dispatch_tolls = None
        stop_markers = []
        dispatch_markers = []

        if taxi_ride is not None:

            try:

                path, distance = find_route(
                    origin,
                    destination
                )

            except NoRouteError:

                await ctx.send(
                    f"No road route exists between "
                    f"{_name(origin)} and "
                    f"{_name(destination)}."
                )

                return

        elif dispatch_orders:

            try:

                (
                    path,
                    distance,
                    dispatch_tolls,
                    dispatch_markers,
                ) = dispatch.build_delivery_route(
                    origin,
                    dispatch_orders
                )

            except NoRouteError:

                dispatch.requeue_orders(
                    ctx.author.id,
                    dispatch_orders
                )

                await ctx.send(
                    f"No road route exists covering your boarded "
                    f"deliveries and {_name(destination)}."
                )

                return

        elif carpool_stops:

            try:

                (
                    path,
                    distance,
                    carpool_tolls,
                    stop_markers,
                ) = carpool.build_multi_leg_route(
                    origin,
                    carpool_stops,
                    destination
                )

            except NoRouteError:

                carpool.requeue_stops(
                    ctx.author.id,
                    carpool_stops
                )

                await ctx.send(
                    f"No road route exists covering your queued "
                    f"passengers and {_name(destination)}."
                )

                return

        else:

            try:

                path, distance = find_route(
                    origin,
                    destination
                )

            except NoRouteError:

                await ctx.send(
                    f"No road route exists between "
                    f"{_name(origin)} and "
                    f"{_name(destination)}."
                )

                return

        # --------------------------------------------------------
        # VEHICLE CONFIG
        # --------------------------------------------------------

        vehicle_cfg = VEHICLES.get(
            player["vehicle"],
            {}
        )

        # --------------------------------------------------------
        # DISPATCH BICYCLE: NO FUEL, NO TOLLS, LONGER TRAVEL TIME
        #
        # dispatch._is_bicycle() checks the vehicle name against
        # DISPATCH_FUEL_EXEMPT_VEHICLES — a Bicycle is exempt from
        # both fuel and tolls, but takes longer per km than every
        # other vehicle (DISPATCH_BICYCLE_TIME_MULTIPLIER), while
        # a Motorcycle (dispatch premium) still uses fuel, pays
        # tolls, and needs repair like any other vehicle.
        # --------------------------------------------------------

        is_bicycle = dispatch._is_bicycle(
            player["vehicle"]
        )

        consumption = vehicle_cfg.get(
            "fuel_consumption",
            0.1
        )

        fuel_needed = (
            0.0
            if is_bicycle
            else distance * consumption
        )

        # --------------------------------------------------------
        # FUEL CHECK
        # --------------------------------------------------------

        if not is_bicycle and player["fuel"] < fuel_needed:

            await ctx.send(
                f"Not enough fuel for this trip. "
                f"Need {fuel_needed:.1f}, "
                f"you have {player['fuel']:.1f}."
            )

            if carpool_stops:

                carpool.requeue_stops(
                    ctx.author.id,
                    carpool_stops
                )

            if dispatch_orders:

                dispatch.requeue_orders(
                    ctx.author.id,
                    dispatch_orders
                )

            # Boarded taxi ride wasn't popped yet at this point
            # (still just peeked), so there's nothing to requeue —
            # it's simply still there for the driver to retry.

            return

        # --------------------------------------------------------
        # TOLLS
        #
        # Carpool trips use per-leg tolls (carpool_tolls) instead
        # of tolls_on_route(full path) — a toll zone crossed twice
        # across two different legs must be paid twice, and
        # tolls_on_route() alone would only ever charge it once.
        # Dispatch delivery runs are the same multi-leg shape, so
        # they use dispatch_tolls the same way — except a Bicycle
        # never pays tolls at all.
        #
        # Taxi trips are always a single leg (one pickup point,
        # one destination), so the plain tolls_on_route(path) is
        # correct for them too.
        # --------------------------------------------------------

        if is_bicycle:
            tolls = []
        elif dispatch_orders:
            tolls = dispatch_tolls
        elif carpool_stops:
            tolls = carpool_tolls
        else:
            tolls = tolls_on_route(path)

        total_travel_time = _travel_duration(
            distance
        )

        if is_bicycle:

            total_travel_time *= (
                DISPATCH_BICYCLE_TIME_MULTIPLIER
            )

        # ========================================================
        # ONLY NOW DO WE START THE JOURNEY
        #
        # This is also the point where a boarded taxi ride is
        # actually committed to (popped) — everything above this
        # line can still fail safely without losing the ride.
        # ========================================================

        if taxi_ride is not None:
            taxi_ride = taxi.take_confirmed_ride(ctx.author.id)

        if mechanic_job is not None:
            mechanic_job = mechanic.take_confirmed_job(ctx.author.id)

        if dispatch_orders:
            dispatch_orders = dispatch.take_confirmed_orders(
                ctx.author.id
            )

        database.update_player(
            ctx.author.id,
            traveling=1
        )

        # Remove writing permission from the CURRENT location.
        await permissions.set_write_access(
            ctx.guild,
            ctx.author,
            origin,
            allowed=False
        )

        # Carpool: same origin lock, applied to every confirmed
        # passenger riding along too.
        if carpool_stops:

            await carpool.lock_in_passengers(
                ctx.guild,
                carpool_stops,
                origin
            )

        # Taxi: same origin lock for every rider. Riders are
        # tagged in the "Journey Started" embed below using the
        # exact same passenger-tagging mechanism as private-car
        # stop_markers — no separate "Trip started" message.
        if taxi_ride is not None:

            await taxi.lock_in_riders(
                ctx.guild,
                taxi_ride,
                origin
            )

        # --------------------------------------------------------
        # SAVE JOURNEY
        # --------------------------------------------------------

        self.active_journeys[
            ctx.author.id
        ] = {
            "guild_id": ctx.guild.id,
            "origin": origin,
            "destination": destination,
            "path": path,
            "distance": distance,
            "fuel_needed": fuel_needed,
            "pending_tolls": tolls.copy(),
            "travel_time": total_travel_time,
            "current_index": 0,
            "current_toll": None,
            "current_checkpoint": None,
            "stops": stop_markers,
            "dispatch_stops": dispatch_markers,
            "taxi_ride": taxi_ride,
            "mechanic_job": mechanic_job,
            "is_bicycle": is_bicycle,
        }

        # --------------------------------------------------------
        # START MESSAGE
        # --------------------------------------------------------

        stops_text = ""
        passenger_lines = []

        if stop_markers:

            # ----------------------------------------------------
            # Tag each passenger by mention next to their own
            # drop-off point. This is also a visible confirmation
            # that they were actually locked into the trip — if a
            # passenger you expect isn't tagged here, they were
            # NOT picked up and !drive ran as a solo trip.
            # ----------------------------------------------------

            passenger_lines.extend(
                f"🧍 <@{marker['user_id']}> → "
                f"{_name(marker['destination'])}"
                for marker in stop_markers
            )

        if taxi_ride is not None:

            # Taxi riders all share one destination — tagged here
            # using the exact same "🧍 @rider → destination" format
            # as carpool passengers, instead of a separate message.
            passenger_lines.extend(
                f"🧍 <@{rider_id}> → {_name(destination)}"
                for rider_id in taxi_ride["rider_ids"]
            )

        if dispatch_markers:

            # Every boarded order gets its own line, same idea as
            # a carpool passenger — except it tags the sender the
            # parcel is going to, not a traveling passenger.
            passenger_lines.extend(
                f"📦 <@{marker['order']['sender_id']}>'s delivery "
                f"→ {_name(marker['order']['destination'])}"
                for marker in dispatch_markers
            )

        if passenger_lines:

            stops_text = (
                f"\n**Passengers:**\n" + "\n".join(passenger_lines)
            )

        start_embed = discord.Embed(
            title="🚗 Journey Started",
            description=(
                f"**Player:** {ctx.author.mention}\n"
                f"**From:** {_name(origin)}\n"
                f"**To:** {_name(destination)}\n"
                f"**Distance:** {distance:.1f} km\n"
                f"**Estimated Travel Time:** "
                f"{total_travel_time:.0f} seconds"
                f"{stops_text}"
            ),
            color=discord.Color.orange(),
        )

        start_msg = await ctx.send(
            embed=start_embed
        )

        try:

            await ctx.message.delete()

        except (
            discord.Forbidden,
            discord.NotFound
        ):

            pass

        # --------------------------------------------------------
        # DELETE START MESSAGE LATER
        # --------------------------------------------------------

        async def delete_start_message():

            await asyncio.sleep(
                TRAVEL_MESSAGE_DELETE_DELAY_SECONDS
            )

            try:

                await start_msg.delete()

            except (
                discord.Forbidden,
                discord.NotFound
            ):

                pass

        self.bot.loop.create_task(
            delete_start_message()
        )

        # --------------------------------------------------------
        # BEGIN ROUTE
        # --------------------------------------------------------

        await self._travel_route(
            ctx.guild,
            ctx.author
        )

    # ============================================================
    # TOLL MESSAGE
    # ============================================================

    async def _stop_at_toll(
        self,
        guild: discord.Guild,
        member: discord.Member,
        toll_code: str
    ) -> None:

        journey = self.active_journeys.get(
            member.id
        )

        if not journey:
            return

        toll_info = TOLL_ZONES.get(
            toll_code
        )

        if not toll_info:
            return

        journey["current_toll"] = toll_code

        # --------------------------------------------------------
        # Give temporary write access to toll channel.
        # --------------------------------------------------------

        await permissions.set_write_access(
            guild,
            member,
            toll_code,
            allowed=True
        )

        channel = permissions.get_channel_for_code(
            guild,
            toll_code
        )

        if channel is not None:

            await channel.send(
                f"🚦 {member.mention} has arrived at "
                f"**{toll_info['name']}**.\n\n"
                f"💰 Toll fee: "
                f"**₦{toll_info['amount']:,}**\n\n"
                f"Use `!paytoll` to pay and continue "
                f"your journey."
            )

     # ============================================================
    # SEGMENT-BY-SEGMENT TRAVEL
    # ============================================================

    async def _travel_route(
        self,
        guild: discord.Guild,
        member: discord.Member
    ):

        journey = self.active_journeys.get(
            member.id
        )

        if not journey:
            return

        from routing import GRAPH

        path = journey["path"]

        current_index = journey.get(
            "current_index",
            0
        )

        pending_tolls = journey[
            "pending_tolls"
        ]

        # --------------------------------------------------------
        # TRAVEL
        # --------------------------------------------------------

        while current_index < len(path) - 1:

            current_node = path[
                current_index
            ]

            next_node = path[
                current_index + 1
            ]

            # ----------------------------------------------------
            # POLICE CHECKPOINT
            #
            # A mounted checkpoint pauses the journey the moment
            # it's reached, the same way a toll gate does — except
            # there's no self-service way through it. An officer
            # must !clear the player before _continue_after_toll's
            # checkpoint twin (resume_after_checkpoint) can move
            # them on. A Bicycle (dispatch standard) is exempt,
            # same as it is from tolls and fuel.
            # ----------------------------------------------------

            checkpoint = (
                None
                if journey.get("is_bicycle")
                else police.get_checkpoint(current_node)
            )

            if checkpoint is not None:

                journey["current_index"] = (
                    current_index
                )

                await self._stop_at_checkpoint(
                    guild,
                    member,
                    current_node
                )

                return

            # ----------------------------------------------------
            # IMPORTANT TOLL LOGIC
            #
            # A toll is charged when LEAVING a toll-controlled
            # zone.
            #
            # Example:
            #
            # Dealership
            #     ↓
            # Mainland
            #     ↓
            # Island
            #
            # The Mainland toll is paid BEFORE travelling
            # Mainland -> Island.
            # ----------------------------------------------------

            current_zone = (
                database.get_location_data(current_node) or {}
            ).get("zone")

            next_zone = (
                database.get_location_data(next_node) or {}
            ).get("zone")

            if (
                current_zone != next_zone
                and current_node in TOLL_ZONES
                and current_node in pending_tolls
            ):

                pending_tolls.remove(
                    current_node
                )

                journey["current_index"] = (
                    current_index
                )

                await self._stop_at_toll(
                    guild,
                    member,
                    current_node
                )

                # STOP.
                #
                # The journey remains in active_journeys.
                # traveling remains 1.
                #
                # !paytoll will resume it.
                return

            # ----------------------------------------------------
            # TRAVEL THIS ROAD SEGMENT
            #
            # A Bicycle takes DISPATCH_BICYCLE_TIME_MULTIPLIER
            # times longer per segment than every other vehicle —
            # see the "is_bicycle" note on total_travel_time in
            # !drive for why this can't just be baked into
            # _travel_duration() itself (that helper has no idea
            # what vehicle is being used).
            # ----------------------------------------------------

            segment_distance = GRAPH[
                current_node
            ][
                next_node
            ]

            segment_time = _travel_duration(
                segment_distance
            )

            if journey.get("is_bicycle"):

                segment_time *= (
                    DISPATCH_BICYCLE_TIME_MULTIPLIER
                )

            from cogs.weather import get_movement_multiplier
            from cogs.daynight import get_movement_multiplier as get_night_movement_multiplier
            segment_time *= get_movement_multiplier()
            segment_time *= get_night_movement_multiplier()

            await asyncio.sleep(
                segment_time
            )

            current_index += 1

            journey["current_index"] = (
                current_index
            )

            if journey.get("stops"):

                await carpool.handle_arrival(
                    guild,
                    journey,
                    current_index
                )

            if journey.get("dispatch_stops"):

                await self._handle_dispatch_arrival(
                    guild,
                    member,
                    journey,
                    current_index
                )

        # --------------------------------------------------------
        # DESTINATION REACHED
        # --------------------------------------------------------

        await self._complete_journey(
            guild,
            member
        )

    # ============================================================
    # DISPATCH DELIVERY ARRIVAL (intermediate stops)
    #
    # dispatch.handle_delivery_arrival() charges the sender, pays
    # the rider, and notifies the sender in the order's ORIGIN
    # channel on its own. It returns a short payout summary meant
    # for the rider, which has nowhere to go without this wrapper
    # — post it into the channel the rider is physically passing
    # through right now (the stop's own destination), same spot
    # carpool's "Dropped off" message would appear for a private-
    # car passenger.
    # ============================================================

    async def _handle_dispatch_arrival(
        self,
        guild: discord.Guild,
        member: discord.Member,
        journey: dict,
        current_index: int
    ) -> None:

        summary = await dispatch.handle_delivery_arrival(
            guild,
            member.id,
            journey,
            current_index
        )

        if not summary:
            return

        path = journey["path"]
        stop_location = path[current_index]

        stop_channel = permissions.get_channel_for_code(
            guild,
            stop_location
        )

        if stop_channel is None:
            return

        summary_msg = await stop_channel.send(
            f"{member.mention}\n{summary}"
        )

        async def _delete_later(msg=summary_msg):

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

        self.bot.loop.create_task(
            _delete_later()
        )

    # ============================================================
    # POLICE CHECKPOINT MESSAGE
    # ============================================================

    async def _stop_at_checkpoint(
        self,
        guild: discord.Guild,
        member: discord.Member,
        location_code: str
    ) -> None:

        journey = self.active_journeys.get(
            member.id
        )

        if not journey:
            return

        journey["current_checkpoint"] = location_code

        # The driver gets temporary write access to talk/respond
        # at the checkpoint — any passengers riding along stay
        # read-only, exactly like a toll stop.
        await permissions.set_write_access(
            guild,
            member,
            location_code,
            allowed=True
        )

        police.mark_paused(
            location_code,
            member.id
        )

        channel = permissions.get_channel_for_code(
            guild,
            location_code
        )

        if channel is not None:

            await channel.send(
                f"🚧 {member.mention} has been stopped at a "
                f"police checkpoint in **{_name(location_code)}**."
                f"\n\nAn officer must `!clear {member.mention}` "
                f"before you can continue."
            )

    # ============================================================
    # RESUME AFTER CHECKPOINT
    #
    # Called by police.py's !clear once an officer releases this
    # player. Mirrors _continue_after_toll: advance past the
    # checkpoint node we were just paused at, then fall back into
    # the normal segment-by-segment loop (which can hit another
    # toll or checkpoint further down the route).
    # ============================================================

    async def resume_after_checkpoint(
        self,
        guild: discord.Guild,
        member: discord.Member
    ) -> None:

        journey = self.active_journeys.get(
            member.id
        )

        if not journey:
            return

        # Remove write access to the checkpoint channel now that
        # the stop is over — same cleanup !paytoll does for tolls.
        current_checkpoint = journey.get(
            "current_checkpoint"
        )

        if current_checkpoint:

            await permissions.set_write_access(
                guild,
                member,
                current_checkpoint,
                allowed=False
            )

        journey["current_checkpoint"] = None

        from routing import GRAPH

        path = journey["path"]

        current_index = journey.get(
            "current_index",
            0
        )

        if current_index < len(path) - 1:

            current_node = path[
                current_index
            ]

            next_node = path[
                current_index + 1
            ]

            segment_distance = GRAPH[
                current_node
            ][
                next_node
            ]

            segment_time = _travel_duration(
                segment_distance
            )

            if journey.get("is_bicycle"):

                segment_time *= (
                    DISPATCH_BICYCLE_TIME_MULTIPLIER
                )

            from cogs.weather import get_movement_multiplier
            from cogs.daynight import get_movement_multiplier as get_night_movement_multiplier
            segment_time *= get_movement_multiplier()
            segment_time *= get_night_movement_multiplier()

            await asyncio.sleep(
                segment_time
            )

            current_index += 1

            journey["current_index"] = (
                current_index
            )

            if journey.get("stops"):

                await carpool.handle_arrival(
                    guild,
                    journey,
                    current_index
                )

            if journey.get("dispatch_stops"):

                await self._handle_dispatch_arrival(
                    guild,
                    member,
                    journey,
                    current_index
                )

        await self._travel_route(
            guild,
            member
        )

    # ============================================================
    # !PAYTOLL
    # ============================================================

    @commands.command(name="paytoll")
    async def paytoll(
        self,
        ctx: commands.Context
    ):

        # --------------------------------------------------------
        # IMPORTANT:
        #
        # Do NOT rely only on database traveling.
        #
        # The actual active journey is stored here while the
        # journey is running.
        # --------------------------------------------------------

        journey = self.active_journeys.get(
            ctx.author.id
        )

        if journey is None:

            await ctx.send(
                "You have no active journey."
            )

            return

        current_toll = journey.get(
            "current_toll"
        )

        if not current_toll:

            await ctx.send(
                "You do not have a toll waiting for payment."
            )

            return

        # --------------------------------------------------------
        # CHECK TOLL LOCATION
        # --------------------------------------------------------

        if current_toll not in LOCATIONS:

            await ctx.send(
                "⛔ Invalid toll checkpoint."
            )

            return

        expected_channel = LOCATIONS[
            current_toll
        ]["channel"]

        if ctx.channel.name != expected_channel:

            await ctx.send(
                f"You must pay this toll in "
                f"#{expected_channel}."
            )

            return

        # --------------------------------------------------------
        # TOLL DATA
        # --------------------------------------------------------

        toll_info = TOLL_ZONES.get(
            current_toll
        )

        if not toll_info:

            await ctx.send(
                "⛔ Toll information could not be found."
            )

            return

        # --------------------------------------------------------
        # PLAYER
        # --------------------------------------------------------

        player = database.get_player(
            ctx.author.id
        )

        if player is None:

            await ctx.send(
                "Player account not found."
            )

            return

        # --------------------------------------------------------
        # MONEY
        # --------------------------------------------------------

        if player["balance"] < toll_info["amount"]:

            await ctx.send(
                f"❌ You don't have enough money to pay "
                f"this toll.\n\n"
                f"Required: ₦{toll_info['amount']:,}\n"
                f"Balance: ₦{player['balance']:,}"
            )

            return

        # --------------------------------------------------------
        # PAY
        # --------------------------------------------------------

        database.update_player(
            ctx.author.id,
            balance=(
                player["balance"]
                - toll_info["amount"]
            )
        )

        # --------------------------------------------------------
        # REMOVE TOLL WRITE ACCESS
        # --------------------------------------------------------

        await permissions.set_write_access(
            ctx.guild,
            ctx.author,
            current_toll,
            allowed=False
        )

        # --------------------------------------------------------
        # CLEAR CURRENT TOLL
        # --------------------------------------------------------

        journey["current_toll"] = None

        await ctx.send(
            f"✅ **Toll paid.**\n"
            f"Amount: ₦{toll_info['amount']:,}\n\n"
            f"🚗 Continuing your journey..."
        )

        # --------------------------------------------------------
        # CONTINUE
        # --------------------------------------------------------

        await self._continue_after_toll(
            ctx.guild,
            ctx.author
        )

    # ============================================================
    # CONTINUE AFTER TOLL
    # ============================================================

    async def _continue_after_toll(
        self,
        guild: discord.Guild,
        member: discord.Member
    ):

        journey = self.active_journeys.get(
            member.id
        )

        if not journey:
            return

        from routing import GRAPH

        path = journey["path"]

        current_index = journey.get(
            "current_index",
            0
        )

        # --------------------------------------------------------
        # LEAVE THE TOLL NODE WE JUST PAID AT
        #
        # The toll for departing this node has already been
        # resolved (removed from pending_tolls) and paid for.
        # Travel that one segment immediately, WITHOUT re-running
        # the toll/checkpoint checks below — otherwise, on a
        # carpool/dispatch trip where the same toll zone appears
        # twice in pending_tolls (once per leg), the second entry
        # would be matched and silently consumed right here, at
        # the first crossing, instead of at the later crossing it
        # was meant for.
        # --------------------------------------------------------

        if current_index < len(path) - 1:

            current_node = path[
                current_index
            ]

            next_node = path[
                current_index + 1
            ]

            segment_distance = GRAPH[
                current_node
            ][
                next_node
            ]

            segment_time = _travel_duration(
                segment_distance
            )

            if journey.get("is_bicycle"):

                segment_time *= (
                    DISPATCH_BICYCLE_TIME_MULTIPLIER
                )

            from cogs.weather import get_movement_multiplier
            from cogs.daynight import get_movement_multiplier as get_night_movement_multiplier
            segment_time *= get_movement_multiplier()
            segment_time *= get_night_movement_multiplier()

            await asyncio.sleep(
                segment_time
            )

            current_index += 1

            journey["current_index"] = (
                current_index
            )

            if journey.get("stops"):

                await carpool.handle_arrival(
                    guild,
                    journey,
                    current_index
                )

            if journey.get("dispatch_stops"):

                await self._handle_dispatch_arrival(
                    guild,
                    member,
                    journey,
                    current_index
                )

        # --------------------------------------------------------
        # HAND BACK TO THE NORMAL SEGMENT-BY-SEGMENT LOOP
        #
        # _travel_route() already knows how to check for the next
        # toll, the next checkpoint, carpool/dispatch arrivals,
        # and bicycle timing — no need to duplicate that logic
        # here a second time.
        # --------------------------------------------------------

        await self._travel_route(
            guild,
            member
        )

    # ============================================================
    # COMPLETE JOURNEY
    # ============================================================

    async def _complete_journey(
        self,
        guild: discord.Guild,
        member: discord.Member
    ):

        journey = self.active_journeys.pop(
            member.id,
            None
        )

        if not journey:
            return

        destination = journey[
            "destination"
        ]

        origin = journey[
            "origin"
        ]

        player = database.get_player(
            member.id
        )

        if player is None:
            return

        # --------------------------------------------------------
        # VEHICLE CONDITION
        #
        # A Bicycle needs no repair — only a Motorcycle (dispatch
        # premium) wears down like every other vehicle.
        # --------------------------------------------------------

        condition_lost = (
            0.0
            if journey.get("is_bicycle")
            else journey["distance"] * CONDITION_LOSS_PER_KM
        )

        new_condition = max(
            0.0,
            player["vehicle_condition"]
            - condition_lost
        )

        # --------------------------------------------------------
        # FUEL
        # --------------------------------------------------------

        new_fuel = max(
            0.0,
            player["fuel"]
            - journey["fuel_needed"]
        )

        # --------------------------------------------------------
        # UPDATE DATABASE
        # --------------------------------------------------------

        database.update_player(
            member.id,
            location=destination,
            traveling=0,
        )

        # --------------------------------------------------------
        # PERSIST TO THE ACTUAL VEHICLE RECORD
        #
        # The line above only ever touched the legacy flat
        # mirror columns (vehicle_location/fuel/vehicle_condition).
        # Those columns always describe whichever vehicle is
        # CURRENTLY selected — so the moment a player is handed a
        # second vehicle (e.g. !assignpd) and it becomes selected,
        # the flat columns start describing THAT vehicle instead,
        # and this vehicle's real arrival location/fuel/condition
        # would never make it into its own record in the
        # `vehicles` list — leaving it stuck showing wherever it
        # was when first added (e.g. "dealership") instead of
        # where it was actually last driven to. Writing through
        # update_vehicle() keeps the per-vehicle record itself
        # correct regardless of what's selected later.
        # --------------------------------------------------------

        selected_vehicle = database.get_selected_vehicle(member.id)

        if selected_vehicle is not None:

            database.update_vehicle(
                member.id,
                selected_vehicle["id"],
                location=destination,
                fuel=new_fuel,
                condition=new_condition,
            )

        else:

            database.update_player(
                member.id,
                vehicle_location=destination,
                fuel=new_fuel,
                vehicle_condition=new_condition,
            )

        # --------------------------------------------------------
        # MOVE WRITE ACCESS
        # --------------------------------------------------------

        await permissions.move_write_access(
            guild,
            member,
            old_code=origin,
            new_code=destination
        )

        # --------------------------------------------------------
        # DESTINATION CHANNEL
        # --------------------------------------------------------

        dest_channel = permissions.get_channel_for_code(
            guild,
            destination
        )

        if dest_channel:

            arrival_text = (
                f"✅ {member.mention} has arrived at "
                f"**{_name(destination)}**."
            )

            if new_condition <= 20:

                arrival_text += (
                    f"\n⚠️ Vehicle condition is low "
                    f"({new_condition:.0f}). "
                    f"Visit {_name('repair')} for repairs."
                )

            # ------------------------------------------------
            # TAXI: SETTLE THE FARE + UNLOCK THE DESTINATION
            # CHANNEL FOR EVERY RIDER
            #
            # Driver's own write access was already moved above
            # via move_write_access(); this handles the riders,
            # who aren't the "member" driving.
            # ------------------------------------------------

            taxi_ride = journey.get("taxi_ride")

            if taxi_ride is not None:

                fare_text = await taxi.handle_taxi_arrival(
                    guild,
                    member,
                    taxi_ride
                )

                if fare_text:
                    arrival_text += f"\n{fare_text}"

            # ------------------------------------------------
            # MECHANIC: RUN THE REPAIR
            # ------------------------------------------------

            mechanic_job = journey.get("mechanic_job")

            if mechanic_job is not None:

                repair_text = await mechanic.handle_mechanic_arrival(
                    guild,
                    member,
                    mechanic_job
                )

                if repair_text:
                    arrival_text += f"\n{repair_text}"

            await dest_channel.send(
                arrival_text
            )

# ================================================================
# DISCORD EXTENSION SETUP
# ================================================================

async def setup(bot: commands.Bot):
    await bot.add_cog(TravelCog(bot))
