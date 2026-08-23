import discord
from discord.ext import commands

import database
from checks import require_location
from config import VEHICLES

FUEL_COST_PER_UNIT = 500  # ₦ per unit of fuel — tune as needed
FUEL_CODE = "fuel"


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="balance")
    async def balance(self, ctx: commands.Context):
        player = database.get_or_create_player(ctx.author.id)
        embed = discord.Embed(title="💰 Balance", color=discord.Color.gold())
        embed.add_field(name="Amount", value=f"₦{player['balance']:,}")
        await ctx.send(embed=embed)

    @commands.command(name="refuel")
    @require_location(FUEL_CODE)
    async def refuel(self, ctx: commands.Context):
        player = database.get_player(ctx.author.id)
        if not player["vehicle"]:
            await ctx.send("You don't own a vehicle to refuel.")
            return

        capacity = VEHICLES.get(player["vehicle"], {}).get("fuel_capacity", 60)
        needed = capacity - player["fuel"]
        if needed <= 0:
            await ctx.send("Your tank is already full.")
            return

        cost = round(needed * FUEL_COST_PER_UNIT)
        if player["balance"] < cost:
            affordable_units = player["balance"] / FUEL_COST_PER_UNIT
            database.update_player(
                ctx.author.id,
                balance=0,
                fuel=player["fuel"] + affordable_units,
            )
            await ctx.send(
                f"You could only afford a partial refuel: +{affordable_units:.1f} fuel for ₦{player['balance']:,}."
            )
            return

        database.update_player(
            ctx.author.id,
            balance=player["balance"] - cost,
            fuel=capacity,
        )
        await ctx.send(f"⛽ Tank filled to {capacity} for ₦{cost:,}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
