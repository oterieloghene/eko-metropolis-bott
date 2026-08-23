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
)


def _name(code: str) -> str:
    loc = LOCATIONS.get(code)
    return loc["name"] if loc else code


class TravelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_journeys: dict[int, dict] = {}

    # -----------------------------------------------------------------
    # !route
    # -----------------------------------------------------------------
    @commands.command(name="route")
    async def route(self, ctx: commands.Context, *, destination: str = None):
        if not destination:
            await ctx.send("Usage: `!route <destination>`")
            return

        destination = destination.strip().lower().replace(" ", "-")
        player = database.get_or_create_player(ctx.author.id)
        origin = player["location"]

        if destination not in ROAD_DESTINATIONS:
            await ctx.send(f"`{destination}` is not a valid road destination.")
            return

        if destination == origin:
            await ctx.send("You are already there.")
            return

        try:
            path, distance = find_route(origin, destination)
        except NoRouteError:
            await ctx.send(
                f"No road route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )
            return

        tolls = tolls_on_route(path)
        readable_path = " → ".join(_name(c) for c in path)
        toll_text = (
            ", ".join(TOLL_ZONES[t]["name"] for t in tolls)
            if tolls
            else "None"
        )

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

    # -----------------------------------------------------------------
    # !drive
    # -----------------------------------------------------------------
    @commands.command(name="drive")
    async def drive(
        self,
        ctx: commands.Context,
        *,
        destination: str = None
    ):
        if not destination:
            await ctx.send("Usage: `!drive <destination>`")
            return

        destination = destination.strip().lower().replace(" ", "-")

        player = database.get_or_create_player(ctx.author.id)
        origin = player["location"]

        if player["traveling"]:
            await ctx.send("You are already travelling.")
            return

        if not player["vehicle"]:
            await ctx.send(
                "You don't own a vehicle. "
                "Buy one at the Vehicle Dealership first."
            )
            return

        if player["vehicle_condition"] <= 0:
            await ctx.send(
                f"Your {player['vehicle']} is too damaged to drive "
                f"(0 condition). Get it repaired at "
                f"{LOCATIONS['repair']['name']} first."
            )
            return

        expected_channel = LOCATIONS[origin]["channel"]

        if ctx.channel.name != expected_channel:
            await ctx.send(
                f"You are not physically at {_name(origin)}'s channel "
                f"right now — go to #{expected_channel} to drive from there."
            )
            return

        if destination not in ROAD_DESTINATIONS:
            await ctx.send(
                f"`{destination}` is not a valid road destination."
            )
            return

        if destination == origin:
            await ctx.send("You are already there.")
            return

        try:
            path, distance = find_route(origin, destination)
        except NoRouteError:
            await ctx.send(
                f"No road route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )
            return

        vehicle_cfg = VEHICLES.get(player["vehicle"], {})

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

        # -------------------------------------------------------------
        # START JOURNEY
        # -------------------------------------------------------------

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
        }

        start_embed = discord.Embed(
            title="🚗 Journey Started",
            description=(
                f"**Player:** {ctx.author.mention}\n"
                f"**From:** {_name(origin)}\n"
                f"**To:** {_name(destination)}\n"
                f"**Distance:** {distance:.1f} km"
            ),
            color=discord.Color.orange(),
        )

        start_msg = await ctx.send(
            embed=start_embed
        )

        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        async def _cleanup_start_message():
            await asyncio.sleep(
                TRAVEL_MESSAGE_DELETE_DELAY_SECONDS
            )

            try:
                await start_msg.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

        self.bot.loop.create_task(
            _cleanup_start_message()
        )

        if tolls:
            await self._prompt_next_toll(
                ctx.guild,
                ctx.author
            )
        else:
            await self._complete_journey(
                ctx.guild,
                ctx.author
            )

    # -----------------------------------------------------------------
    # TOLL CHECKPOINT
    # -----------------------------------------------------------------
    async def _prompt_next_toll(
        self,
        guild: discord.Guild,
        member: discord.Member
    ):
        journey = self.active_journeys.get(
            member.id
        )

        if not journey or not journey["pending_tolls"]:
            await self._complete_journey(
                guild,
                member
            )
            return

        toll_code = journey["pending_tolls"][0]

        toll_info = TOLL_ZONES[toll_code]

        channel = permissions.get_channel_for_code(
            guild,
            toll_code
        )

        if channel is None:
            # If the checkpoint channel doesn't exist,
            # skip this toll instead of permanently
            # trapping the player.
            journey["pending_tolls"].pop(0)

            await self._prompt_next_toll(
                guild,
                member
            )

            return

        # -------------------------------------------------------------
        # IMPORTANT:
        # The toll channel remains locked for @everyone.
        # Only the travelling player gets temporary write access.
        # -------------------------------------------------------------

        await permissions.set_write_access(
            guild,
            member,
            toll_code,
            allowed=True
        )

        await channel.send(
            f"🚦 {member.mention} has reached "
            f"**{toll_info['name']}**.\n\n"
            f"Pay ₦{toll_info['amount']:,} with "
            f"`!paytoll` to continue."
        )

    # -----------------------------------------------------------------
    # !paytoll
    # -----------------------------------------------------------------
    @commands.command(name="paytoll")
    async def paytoll(
        self,
        ctx: commands.Context
    ):
        journey = self.active_journeys.get(
            ctx.author.id
        )

        if not journey or not journey["pending_tolls"]:
            await ctx.send(
                "You have no pending toll here."
            )
            return

        toll_code = journey["pending_tolls"][0]

        toll_info = TOLL_ZONES[toll_code]

        expected_channel = LOCATIONS[toll_code]["channel"]

        if ctx.channel.name != expected_channel:
            await ctx.send(
                f"You must pay this toll in "
                f"#{expected_channel}."
            )
            return

        player = database.get_player(
            ctx.author.id
        )

        if player["balance"] < toll_info["amount"]:
            await ctx.send(
                f"You don't have enough money to pay "
                f"this toll (₦{toll_info['amount']:,})."
            )
            return

        # -------------------------------------------------------------
        # PAY TOLL
        # -------------------------------------------------------------

        database.update_player(
            ctx.author.id,
            balance=(
                player["balance"]
                - toll_info["amount"]
            )
        )

        journey["pending_tolls"].pop(0)

        # -------------------------------------------------------------
        # Remove temporary write access from this toll channel.
        # -------------------------------------------------------------

        await permissions.set_write_access(
            ctx.guild,
            ctx.author,
            toll_code,
            allowed=False
        )

        await ctx.send(
            f"✅ Toll paid at "
            f"{toll_info['name']}."
            f" Continuing journey..."
        )

        # -------------------------------------------------------------
        # NEXT TOLL OR DESTINATION
        # -------------------------------------------------------------

        if journey["pending_tolls"]:
            await self._prompt_next_toll(
                ctx.guild,
                ctx.author
            )
        else:
            await self._complete_journey(
                ctx.guild,
                ctx.author
            )

    # -----------------------------------------------------------------
    # JOURNEY COMPLETION
    # -----------------------------------------------------------------
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
        origin = journey["origin"]

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

        # Move write access to destination.
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
                    f" ⚠️ Vehicle condition is low "
                    f"({new_condition:.0f}) — visit "
                    f"{_name('repair')} soon."
                )

            await dest_channel.send(
                arrival_text
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(
        TravelCog(bot)
        )
