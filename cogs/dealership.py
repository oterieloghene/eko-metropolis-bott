import discord
from discord.ext import commands

import database
from checks import require_location
from config import VEHICLES, LOCATIONS

DEALERSHIP_CODE = "dealership"


class DealershipCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="cars")
    @require_location(DEALERSHIP_CODE)
    async def cars(self, ctx: commands.Context):
        embed = discord.Embed(
            title=f"🚘 {LOCATIONS[DEALERSHIP_CODE]['name']} — Available Vehicles",
            color=discord.Color.blue(),
        )
        for name, cfg in VEHICLES.items():
    embed.add_field(
        name=name,
        value=(
            f"Price: ₦{cfg['price']:,}\n"
            f"In stock: {cfg['quantity']}\n"
            f"Fuel capacity: {cfg['fuel_capacity']}\n"
            f"Passenger capacity: {cfg['passenger_capacity']}"
        ),
        inline=True,
    )
        embed.set_footer(text="Use !buy <vehicle name> to purchase.")
        await ctx.send(embed=embed)

    @commands.command(name="buy")
    @require_location(DEALERSHIP_CODE)
    async def buy(self, ctx: commands.Context, *, vehicle_name: str = None):
        if not vehicle_name:
            await ctx.send("Usage: `!buy <vehicle name>` — see `!cars` for options.")
            return

        # Case-insensitive match against configured vehicle names.
        match = next((name for name in VEHICLES if name.lower() == vehicle_name.strip().lower()), None)
        if not match:
            await ctx.send(f"`{vehicle_name}` isn't a vehicle we sell. Check `!cars`.")
            return

        cfg = VEHICLES[match]
        player = database.get_player(ctx.author.id)

        if player["vehicle"]:
            await ctx.send(f"You already own a {player['vehicle']}. You can't buy another one right now.")
            return

        if cfg["quantity"] <= 0:
            await ctx.send(f"{match} is currently out of stock.")
            return

        if player["balance"] < cfg["price"]:
            await ctx.send(f"You need ₦{cfg['price']:,} but only have ₦{player['balance']:,}.")
            return

        owned = database.get_vehicles(ctx.author.id)
        owned.append(match)

        database.update_player(
            ctx.author.id,
            balance=player["balance"] - cfg["price"],
            vehicle=match,
            vehicle_location=DEALERSHIP_CODE,
            fuel=cfg["fuel_capacity"],
            vehicle_condition=cfg["condition"],
            vehicles=owned,
            location=DEALERSHIP_CODE,  # purchase keeps the player at the dealership
        )

        role_name = cfg.get("role")
        if role_name:
            role = discord.utils.get(ctx.guild.roles, name=role_name)
            if role:
                try:
                    await ctx.author.add_roles(role, reason="Vehicle purchase")
                except discord.Forbidden:
                    pass

        await ctx.send(f"🎉 {ctx.author.mention} bought a **{match}** for ₦{cfg['price']:,}!")

    @commands.command(name="vehicle")
    async def vehicle(self, ctx: commands.Context):
        player = database.get_or_create_player(ctx.author.id)
        if not player["vehicle"]:
            await ctx.send("You don't own a vehicle yet.")
            return

        loc = LOCATIONS.get(player["vehicle_location"])
        loc_name = loc["name"] if loc else "Unknown"

        embed = discord.Embed(title="🚗 Your Vehicle", color=discord.Color.dark_blue())
        embed.add_field(name="Vehicle", value=player["vehicle"], inline=True)
        embed.add_field(name="Location", value=loc_name, inline=True)
        embed.add_field(name="Fuel", value=f"{player['fuel']:.1f}", inline=True)
        embed.add_field(name="Condition", value=f"{player['vehicle_condition']:.0f}", inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(DealershipCog(bot))
