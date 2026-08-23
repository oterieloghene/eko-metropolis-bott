import discord
from discord.ext import commands

import database
from config import LOCATIONS, ZONE_LABELS


class LocationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="location")
    async def location(self, ctx: commands.Context):
        """Report the player's ACTUAL database location — never guessed from channel."""
        player = database.get_or_create_player(ctx.author.id)
        code = player["location"]
        loc = LOCATIONS.get(code)

        if loc is None:
            await ctx.send("Your location data looks corrupted — please contact an admin.")
            return

        embed = discord.Embed(title="📍 Current Location", color=discord.Color.blurple())
        embed.add_field(name="Location", value=loc["name"], inline=True)
        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        embed.add_field(name="Zone", value=ZONE_LABELS.get(loc["zone"], loc["zone"]), inline=True)

        if player["vehicle"]:
            embed.add_field(name="Vehicle", value=player["vehicle"], inline=True)
            if player["vehicle_location"]:
                veh_loc = LOCATIONS.get(player["vehicle_location"])
                veh_loc_name = veh_loc["name"] if veh_loc else player["vehicle_location"]
                embed.add_field(name="Vehicle Location", value=veh_loc_name, inline=True)
            embed.add_field(name="Fuel", value=f"{player['fuel']:.1f}", inline=True)
        else:
            embed.add_field(name="Vehicle", value="None", inline=True)

        if player["traveling"]:
            embed.set_footer(text="You are currently travelling.")

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LocationCog(bot))
