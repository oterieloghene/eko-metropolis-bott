import asyncio

import discord
from discord.ext import commands

import database
import permissions

from cogs import carpool
from cogs import taxi
from cogs import mechanic

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
)


# ================================================================
# LOCATION HELPERS
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

    location = LOCATIONS.get(code)

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

    location = LOCATIONS.get(code)

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

    location = LOCATIONS.get(destination)

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

        if destination not in LOCATIONS:

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

        if origin not in LOCATIONS:

            await ctx.send(
                "⛔ Your current location is invalid. "
                "Please contact an administrator."
            )

            return

        # --------------------------------------------------------
        # MUST ACTUALLY BE IN CURRENT LOCATION CHANNEL
        # --------------------------------------------------------

        expected_channel = LOCATIONS[
            origin
        ]["channel"]

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

        if destination not in LOCATIONS:

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
            if taxi_ride is not None
            else carpool.take_confirmed_stops(ctx.author.id)
        )

        # --------------------------------------------------------
        # FIND ROUTE
        # --------------------------------------------------------

        carpool_tolls = None
        stop_markers = []

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

        consumption = vehicle_cfg.get(
            "fuel_consumption",
            0.1
        )

        fuel_needed = (
            distance * consumption
        )

        # --------------------------------------------------------
        # FUEL CHECK
        # --------------------------------------------------------

        if player["fuel"] < fuel_needed:

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
        #
        # Taxi trips are always a single leg (one pickup point,
        # one destination), so the plain tolls_on_route(path) is
        # correct for them too.
        # --------------------------------------------------------

        tolls = (
            carpool_tolls
            if carpool_stops
            else tolls_on_route(path)
        )

        total_travel_time = _travel_duration(
            distance
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

        # Taxi: same origin lock for every rider, plus a "trip
        # started" heads-up so they know the channel's about to
        # lock (mirrors the carpool departure behaviour).
        if taxi_ride is not None:

            await taxi.lock_in_riders(
                ctx.guild,
                taxi_ride,
                origin
            )

            rider_mentions = ", ".join(
                f"<@{rider_id}>"
                for rider_id in taxi_ride["rider_ids"]
            )

            await ctx.send(
                f"🚕 Trip started — {rider_mentions} are on "
                f"the way to **{_name(destination)}**."
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
            "stops": stop_markers,
            "taxi_ride": taxi_ride,
            "mechanic_job": mechanic_job,
        }

        # --------------------------------------------------------
        # START MESSAGE
        # --------------------------------------------------------

        stops_text = ""

        if stop_markers:

            # ----------------------------------------------------
            # Tag each passenger by mention next to their own
            # drop-off point. This is also a visible confirmation
            # that they were actually locked into the trip — if a
            # passenger you expect isn't tagged here, they were
            # NOT picked up and !drive ran as a solo trip.
            # ----------------------------------------------------

            passenger_lines = "\n".join(
                f"🧍 <@{marker['user_id']}> → "
                f"{_name(marker['destination'])}"
                for marker in stop_markers
            )

            stops_text = (
                f"\n**Passengers:**\n{passenger_lines}"
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

            current_zone = LOCATIONS.get(
                current_node,
                {}
            ).get("zone")

            next_zone = LOCATIONS.get(
                next_node,
                {}
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
            # ----------------------------------------------------

            segment_distance = GRAPH[
                current_node
            ][
                next_node
            ]

            segment_time = _travel_duration(
                segment_distance
            )

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

        # --------------------------------------------------------
        # DESTINATION REACHED
        # --------------------------------------------------------

        await self._complete_journey(
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

        pending_tolls = journey[
            "pending_tolls"
        ]

        # --------------------------------------------------------
        # LEAVE THE TOLL NODE WE JUST PAID AT
        #
        # The toll for departing this node has already been
        # resolved (removed from pending_tolls) and paid for.
        # Travel that one segment immediately, WITHOUT re-running
        # the toll check below — otherwise, on a carpool trip
        # where the same toll zone appears twice in pending_tolls
        # (once per leg), the second entry would be matched and
        # silently consumed right here, at the first crossing,
        # instead of at the later crossing it was meant for.
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

        while current_index < len(path) - 1:

            current_node = path[
                current_index
            ]

            next_node = path[
                current_index + 1
            ]

            current_zone = LOCATIONS.get(
                current_node,
                {}
            ).get("zone")

            next_zone = LOCATIONS.get(
                next_node,
                {}
            ).get("zone")

            # ----------------------------------------------------
            # NEXT TOLL
            # ----------------------------------------------------

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

                return

            # ----------------------------------------------------
            # TRAVEL NEXT SEGMENT
            # ----------------------------------------------------

            segment_distance = GRAPH[
                current_node
            ][
                next_node
            ]

            segment_time = _travel_duration(
                segment_distance
            )

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

        # --------------------------------------------------------
        # DESTINATION
        # --------------------------------------------------------

        await self._complete_journey(
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
        # --------------------------------------------------------

        condition_lost = (
            journey["distance"]
            * CONDITION_LOSS_PER_KM
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
            vehicle_location=destination,
            fuel=new_fuel,
            vehicle_condition=new_condition,
            traveling=0,
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
