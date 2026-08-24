import asyncio

import discord
from discord.ext import commands

import database
import permissions
from routing import find_route, tolls_on_route, NoRouteError
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

    We deliberately check LOCATIONS directly instead of relying
    only on ROAD_DESTINATIONS.

    This allows zone hubs such as:

        mainland
        island
        ghetto
        farmland

    to be valid road destinations.

    Overseas destinations remain unavailable to road travel.
    """

    code = _normalise_code(code)

    location = LOCATIONS.get(code)

    if location is None:
        return False

    if location.get("zone") == "overseas":
        return False

    return True


# ================================================================
# LOCATION ACCESS / ROLE RESTRICTION
# ================================================================

def _has_location_access(
    member: discord.Member,
    code: str
) -> tuple[bool, list[str]]:
    """
    Check whether a Discord member has permission to access
    a location.

    LOCATIONS uses:

        roles = None

    for unrestricted locations.

    If roles are supplied, the player needs AT LEAST ONE of
    the listed roles.

    Examples:

        COS:
            Eko chiefs
            Eko deputies
            government officials

        Lekki:
            Lekki resident
            Island visitor

        Farmland:
            Streethustler
    """

    location = LOCATIONS.get(code)

    if location is None:
        return False, []

    required_roles = location.get("roles")

    # ------------------------------------------------------------
    # No role restriction.
    # ------------------------------------------------------------

    if not required_roles:
        return True, []

    member_role_names = {
        role.name.strip().lower()
        for role in member.roles
    }

    for required_role in required_roles:

        if required_role.strip().lower() in member_role_names:
            return True, required_roles

    return False, required_roles


# ================================================================
# RESTRICTION MESSAGE
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
    """
    Convert distance into travel time.

    Minimum: 10 seconds
    Maximum: 90 seconds
    """

    return max(
        MIN_TRAVEL_TIME_SECONDS,
        min(
            distance * TRAVEL_SECONDS_PER_KM,
            MAX_TRAVEL_TIME_SECONDS,
        ),
    )


# ================================================================
# ROUTE DISTANCE
# ================================================================

def _route_distance(
    path: list[str],
    start_index: int,
    end_index: int,
    graph
) -> float:

    total = 0.0

    for i in range(start_index, end_index):
        total += graph[path[i]][path[i + 1]]

    return total


# ================================================================
# TRAVEL COG
# ================================================================

class TravelCog(commands.Cog):

    def __init__(self, bot: commands.Bot):

        self.bot = bot

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
        # Destination must exist.
        # --------------------------------------------------------

        if destination not in LOCATIONS:

            await ctx.send(
                f"⛔ `{destination}` is not a valid location."
            )

            return

        # --------------------------------------------------------
        # Overseas destinations are not road destinations.
        # --------------------------------------------------------

        if not _is_road_destination(destination):

            await ctx.send(
                f"⛔ **{_name(destination)}** "
                f"is not accessible by road."
            )

            return

        # --------------------------------------------------------
        # LOCATION RESTRICTION
        # --------------------------------------------------------

        has_access, required_roles = _has_location_access(
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
        # Already there.
        # --------------------------------------------------------

        if destination == origin:

            await ctx.send(
                "You are already there."
            )

            return

        # --------------------------------------------------------
        # Find route.
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

        tolls = tolls_on_route(
            path
        )

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
        # ROUTE EMBED
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
        # ALREADY TRAVELLING
        # --------------------------------------------------------

        if player["traveling"]:

            await ctx.send(
                "You are already travelling."
            )

            return

        # --------------------------------------------------------
        # VEHICLE CHECK
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
        # ORIGIN CHANNEL CHECK
        # --------------------------------------------------------

        if origin not in LOCATIONS:

            await ctx.send(
                "⛔ Your current location is invalid. "
                "Please contact an administrator."
            )

            return

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
        # ROAD DESTINATION
        # --------------------------------------------------------

        if not _is_road_destination(destination):

            await ctx.send(
                f"⛔ **{_name(destination)}** "
                f"is not accessible by road."
            )

            return

        # --------------------------------------------------------
        # DESTINATION ACCESS RESTRICTION
        #
        # THIS IS THE IMPORTANT FIX.
        #
        # The bot checks the player's Discord roles BEFORE
        # starting the journey.
        #
        # Therefore a player without access cannot travel to
        # a restricted location and arrive there unable to view it.
        # --------------------------------------------------------

        has_access, required_roles = _has_location_access(
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

            return

        # --------------------------------------------------------
        # TOLLS
        # --------------------------------------------------------

        tolls = tolls_on_route(
            path
        )

        total_travel_time = _travel_duration(
            distance
        )

        # --------------------------------------------------------
        # START JOURNEY
        # --------------------------------------------------------

        database.update_player(
            ctx.author.id,
            traveling=1
        )

        await permissions.set_write_access(
            ctx.guild,
            ctx.author,
            origin,
            allowed=False
        )

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
        }

        start_embed = discord.Embed(
            title="🚗 Journey Started",
            description=(
                f"**Player:** {ctx.author.mention}\n"
                f"**From:** {_name(origin)}\n"
                f"**To:** {_name(destination)}\n"
                f"**Distance:** {distance:.1f} km\n"
                f"**Estimated Travel Time:** "
                f"{total_travel_time:.0f} seconds"
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

        path = journey["path"]

        from routing import GRAPH

        current_index = journey.get(
            "current_index",
            0
        )

        pending_tolls = journey[
            "pending_tolls"
        ]

        while current_index < len(path) - 1:

            next_node = path[
                current_index + 1
            ]

            segment_distance = GRAPH[
                path[current_index]
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

            # ----------------------------------------------------
            # TOLL CHECKPOINT
            # ----------------------------------------------------

            if (
                next_node in TOLL_ZONES
                and next_node in pending_tolls
            ):

                if pending_tolls:

                    pending_tolls.pop(0)

                journey[
                    "current_toll"
                ] = next_node

                await permissions.set_write_access(
                    guild,
                    member,
                    next_node,
                    allowed=True
                )

                channel = permissions.get_channel_for_code(
                    guild,
                    next_node
                )

                if channel is not None:

                    toll_info = TOLL_ZONES[
                        next_node
                    ]

                    await channel.send(
                        f"🚦 {member.mention} has arrived at "
                        f"**{toll_info['name']}**.\n\n"
                        f"💰 Toll fee: "
                        f"**₦{toll_info['amount']:,}**\n\n"
                        f"Use `!paytoll` to pay and continue "
                        f"your journey."
                    )

                return

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

        journey = self.active_journeys.get(
            ctx.author.id
        )

        if not journey:

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

        expected_channel = LOCATIONS[
            current_toll
        ]["channel"]

        if ctx.channel.name != expected_channel:

            await ctx.send(
                f"You must pay this toll in "
                f"#{expected_channel}."
            )

            return

        toll_info = TOLL_ZONES[
            current_toll
        ]

        player = database.get_player(
            ctx.author.id
        )

        if player is None:

            await ctx.send(
                "Player account not found."
            )

            return

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
        # REMOVE TOLL PERMISSION
        # --------------------------------------------------------

        await permissions.set_write_access(
            ctx.guild,
            ctx.author,
            current_toll,
            allowed=False
        )

        journey[
            "current_toll"
        ] = None

        await ctx.send(
            f"✅ **Toll paid.**\n"
            f"Amount: ₦{toll_info['amount']:,}\n\n"
            f"🚗 Continuing your journey..."
        )

        # --------------------------------------------------------
        # CONTINUE
        # --------------------------------------------------------

        path = journey["path"]

        toll_index = path.index(
            current_toll
        )

        await self._continue_after_toll(
            ctx.guild,
            ctx.author,
            toll_index
        )

    # ============================================================
    # CONTINUE AFTER TOLL
    # ============================================================

    async def _continue_after_toll(
        self,
        guild: discord.Guild,
        member: discord.Member,
        current_index: int
    ):

        journey = self.active_journeys.get(
            member.id
        )

        if not journey:
            return

        from routing import GRAPH

        path = journey["path"]

        index = current_index

        while index < len(path) - 1:

            next_node = path[
                index + 1
            ]

            segment_distance = GRAPH[
                path[index]
            ][
                next_node
            ]

            segment_time = _travel_duration(
                segment_distance
            )

            await asyncio.sleep(
                segment_time
            )

            index += 1

            journey[
                "current_index"
            ] = index

            # ----------------------------------------------------
            # ANOTHER TOLL
            # ----------------------------------------------------

            if next_node in TOLL_ZONES:

                journey[
                    "current_toll"
                ] = next_node

                await permissions.set_write_access(
                    guild,
                    member,
                    next_node,
                    allowed=True
                )

                channel = permissions.get_channel_for_code(
                    guild,
                    next_node
                )

                if channel:

                    toll_info = TOLL_ZONES[
                        next_node
                    ]

                    await channel.send(
                        f"🚦 {member.mention} has arrived at "
                        f"**{toll_info['name']}**.\n\n"
                        f"💰 Toll fee: "
                        f"**₦{toll_info['amount']:,}**\n\n"
                        f"Use `!paytoll` to pay and continue "
                        f"your journey."
                    )

                return

        # --------------------------------------------------------
        # DESTINATION REACHED
        # --------------------------------------------------------

        await self._complete_journey(
            guild,
            member
        )

    # ============================================================
    # JOURNEY COMPLETION
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

        player = database.get_player(
            member.id
        )

        if player is None:
            return

        # --------------------------------------------------------
        # CONDITION LOSS
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
        # UPDATE PLAYER
        # --------------------------------------------------------

        database.update_player(
            member.id,
            location=destination,
            vehicle_location=destination,
            fuel=max(
                0.0,
                player["fuel"]
                - journey["fuel_needed"]
            ),
            vehicle_condition=new_condition,
            traveling=0,
        )

        # --------------------------------------------------------
        # GIVE DESTINATION ACCESS
        #
        # This is safe because the destination restriction
        # was already checked BEFORE the journey started.
        # --------------------------------------------------------

        await permissions.move_write_access(
            guild,
            member,
            old_code=None,
            new_code=destination
        )

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

            await dest_channel.send(
                arrival_text
            )


# ================================================================
# COG SETUP
# ================================================================

async def setup(bot: commands.Bot):

    await bot.add_cog(
        TravelCog(bot)
        )
