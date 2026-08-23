import asyncio

import discord
from discord.ext import commands

import database
import permissions
from routing import find_route, tolls_on_route, NoRouteError
from config import (
    LOCATIONS,
    ROAD_DESTINATIONS,
    VEHICLES,
    TOLL_ZONES,
    TRAVEL_MESSAGE_DELETE_DELAY_SECONDS,
    CONDITION_LOSS_PER_KM,
    MIN_TRAVEL_TIME_SECONDS,
    MAX_TRAVEL_TIME_SECONDS,
    TRAVEL_SECONDS_PER_KM,
)


def _name(code: str) -> str:
    loc = LOCATIONS.get(code)
    return loc["name"] if loc else code


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


def _route_distance(path: list[str], start_index: int, end_index: int, graph) -> float:
    """
    Calculate the distance between two points in the route.
    """
    total = 0.0

    for i in range(start_index, end_index):
        total += graph[path[i]][path[i + 1]]

    return total


class TravelCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_journeys: dict[int, dict] = {}

    # ================================================================
    # !route
    # ================================================================

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

        destination = (
            destination
            .strip()
            .lower()
            .replace(" ", "-")
        )

        player = database.get_or_create_player(
            ctx.author.id
        )

        origin = player["location"]

        if destination not in ROAD_DESTINATIONS:
            await ctx.send(
                f"`{destination}` is not a valid road destination."
            )
            return

        if destination == origin:
            await ctx.send(
                "You are already there."
            )
            return

        try:
            path, distance = find_route(
                origin,
                destination
            )
        except NoRouteError:
            await ctx.send(
                f"No road route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )
            return

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

        travel_time = _travel_duration(distance)

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

        await ctx.send(embed=embed)

    # ================================================================
    # !drive
    # ================================================================

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

        destination = (
            destination
            .strip()
            .lower()
            .replace(" ", "-")
        )

        player = database.get_or_create_player(
            ctx.author.id
        )

        origin = player["location"]

        if player["traveling"]:
            await ctx.send(
                "You are already travelling."
            )
            return

        if not player["vehicle"]:
            await ctx.send(
                "You don't own a vehicle. "
                "Buy one at the Vehicle Dealership first."
            )
            return

        if player["vehicle_condition"] <= 0:
            await ctx.send(
                f"Your {player['vehicle']} is too damaged "
                f"to drive (0 condition). Get it repaired "
                f"at {LOCATIONS['repair']['name']} first."
            )
            return

        expected_channel = LOCATIONS[origin]["channel"]

        if ctx.channel.name != expected_channel:
            await ctx.send(
                f"You are not physically at "
                f"{_name(origin)}'s channel right now — "
                f"go to #{expected_channel} to drive from there."
            )
            return

        if destination not in ROAD_DESTINATIONS:
            await ctx.send(
                f"`{destination}` is not a valid road destination."
            )
            return

        if destination == origin:
            await ctx.send(
                "You are already there."
            )
            return

        try:
            path, distance = find_route(
                origin,
                destination
            )
        except NoRouteError:
            await ctx.send(
                f"No road route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )
            return

        vehicle_cfg = VEHICLES.get(
            player["vehicle"],
            {}
        )

        consumption = vehicle_cfg.get(
            "fuel_consumption",
            0.1
        )

        fuel_needed = distance * consumption

        if player["fuel"] < fuel_needed:
            await ctx.send(
                f"Not enough fuel for this trip. "
                f"Need {fuel_needed:.1f}, "
                f"you have {player['fuel']:.1f}."
            )
            return

        tolls = tolls_on_route(path)

        total_travel_time = _travel_duration(distance)

        # ------------------------------------------------------------
        # START JOURNEY
        # ------------------------------------------------------------

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

        self.active_journeys[ctx.author.id] = {
            "guild_id": ctx.guild.id,
            "origin": origin,
            "destination": destination,
            "path": path,
            "distance": distance,
            "fuel_needed": fuel_needed,
            "pending_tolls": tolls.copy(),
            "travel_time": total_travel_time,
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

        # ------------------------------------------------------------
        # ACTUAL ROUTE TRAVEL
        #
        # The player now travels segment-by-segment.
        #
        # Example:
        #
        # Dealership
        #      ↓
        # Mainland toll
        #      ↓
        # Island toll
        #      ↓
        # Chapel
        #
        # The bot stops at every toll before continuing.
        # ------------------------------------------------------------

        await self._travel_route(
            ctx.guild,
            ctx.author
        )

    # ================================================================
    # SEGMENT-BY-SEGMENT TRAVEL
    # ================================================================

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

        # ------------------------------------------------------------
        # Import GRAPH from routing so we use the exact same road
        # distances that Dijkstra used.
        # ------------------------------------------------------------

        from routing import GRAPH

        current_index = 0

        # Toll checkpoints that must be reached.
        pending_tolls = journey["pending_tolls"]

        while current_index < len(path) - 1:

            next_node = path[current_index + 1]

            # --------------------------------------------------------
            # Travel from current node to next node.
            # --------------------------------------------------------

            segment_distance = GRAPH[path[current_index]][next_node]

            segment_time = _travel_duration(
                segment_distance
            )

            await asyncio.sleep(
                segment_time
            )

            current_index += 1

            # --------------------------------------------------------
            # If this node is a toll checkpoint, STOP HERE.
            # --------------------------------------------------------

            if (
                next_node in TOLL_ZONES
                and next_node in pending_tolls
            ):

                # Remove this toll from the front only when reached.
                if pending_tolls:
                    pending_tolls.pop(0)

                # Put it back as the current required toll.
                journey["current_toll"] = next_node

                # Give the player permission to type in the toll
                # channel.
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

                    toll_info = TOLL_ZONES[next_node]

                    await channel.send(
                        f"🚦 {member.mention} has arrived at "
                        f"**{toll_info['name']}**.\n\n"
                        f"💰 Toll fee: "
                        f"**₦{toll_info['amount']:,}**\n\n"
                        f"Use `!paytoll` to pay and continue "
                        f"your journey."
                    )

                # STOP HERE.
                #
                # The player cannot continue until !paytoll.
                return

        # ------------------------------------------------------------
        # No more tolls — destination reached.
        # ------------------------------------------------------------

        await self._complete_journey(
            guild,
            member
        )

    # ================================================================
    # !paytoll
    # ================================================================

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

        expected_channel = LOCATIONS[current_toll]["channel"]

        if ctx.channel.name != expected_channel:
            await ctx.send(
                f"You must pay this toll in "
                f"#{expected_channel}."
            )
            return

        toll_info = TOLL_ZONES[current_toll]

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

        # ------------------------------------------------------------
        # PAY
        # ------------------------------------------------------------

        database.update_player(
            ctx.author.id,
            balance=(
                player["balance"]
                - toll_info["amount"]
            )
        )

        # ------------------------------------------------------------
        # Remove toll permission.
        # ------------------------------------------------------------

        await permissions.set_write_access(
            ctx.guild,
            ctx.author,
            current_toll,
            allowed=False
        )

        journey["current_toll"] = None

        await ctx.send(
            f"✅ **Toll paid.**\n"
            f"Amount: ₦{toll_info['amount']:,}\n\n"
            f"🚗 Continuing your journey..."
        )

        # ------------------------------------------------------------
        # Continue from the toll checkpoint.
        # ------------------------------------------------------------

        from routing import GRAPH

        path = journey["path"]

        # Find the toll's position in the route.
        toll_index = path.index(
            current_toll
        )

        # Continue from that toll.
        await self._continue_after_toll(
            ctx.guild,
            ctx.author,
            toll_index
        )

    # ================================================================
    # CONTINUE AFTER TOLL
    # ================================================================

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

        # ------------------------------------------------------------
        # Continue node-by-node until another toll or destination.
        # ------------------------------------------------------------

        index = current_index

        while index < len(path) - 1:

            next_node = path[index + 1]

            segment_distance = GRAPH[path[index]][next_node]

            segment_time = _travel_duration(
                segment_distance
            )

            await asyncio.sleep(
                segment_time
            )

            index += 1

            # --------------------------------------------------------
            # Another toll?
            # --------------------------------------------------------

            if next_node in TOLL_ZONES:

                journey["current_toll"] = next_node

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

                    toll_info = TOLL_ZONES[next_node]

                    await channel.send(
                        f"🚦 {member.mention} has arrived at "
                        f"**{toll_info['name']}**.\n\n"
                        f"💰 Toll fee: "
                        f"**₦{toll_info['amount']:,}**\n\n"
                        f"Use `!paytoll` to pay and continue "
                        f"your journey."
                    )

                return

        # ------------------------------------------------------------
        # Destination reached.
        # ------------------------------------------------------------

        await self._complete_journey(
            guild,
            member
        )

    # ================================================================
    # JOURNEY COMPLETION
    # ================================================================

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

        destination = journey["destination"]

        player = database.get_player(
            member.id
        )

        if player is None:
            return

        condition_lost = (
            journey["distance"]
            * CONDITION_LOSS_PER_KM
        )

        new_condition = max(
            0.0,
            player["vehicle_condition"]
            - condition_lost
        )

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

        # ------------------------------------------------------------
        # Give writing access at destination.
        # ------------------------------------------------------------

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


async 
