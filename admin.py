import discord
from discord.ext import commands

import database
import permissions
from config import LOCATIONS


def _is_admin():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.guild_permissions.administrator

    return commands.check(predicate)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="setlocation")
    @_is_admin()
    async def setlocation(self, ctx: commands.Context, member: discord.Member, code: str):
        """Explicitly change an EXISTING player's stored location (requirements #1, #21)."""
        code = code.strip().lower()
        if code not in LOCATIONS:
            await ctx.send(f"`{code}` is not a valid location code.")
            return

        player = database.get_or_create_player(member.id)
        old_code = player["location"]

        database.update_player(member.id, location=code, traveling=0)
        await permissions.move_write_access(ctx.guild, member, old_code=old_code, new_code=code)

        await ctx.send(f"Moved {member.mention} from {LOCATIONS[old_code]['name']} to {LOCATIONS[code]['name']}.")

    @commands.command(name="resetalllocations")
    @_is_admin()
    async def resetalllocations(self, ctx: commands.Context, code: str = "dealership"):
        """
        Mass-reset EVERY existing player's stored location. Changing the
        default in config only affects new players, so this is required
        separately (requirements #21).
        """
        code = code.strip().lower()
        if code not in LOCATIONS:
            await ctx.send(f"`{code}` is not a valid location code.")
            return

        count = database.reset_all_locations(code)
        await ctx.send(f"Reset {count} player record(s) to {LOCATIONS[code]['name']}. "
                        f"Note: Discord channel write permissions were NOT bulk-updated — "
                        f"players will get corrected access next time they move or interact.")

    @commands.command(name="resetvehicledata")
    @_is_admin()
    async def resetvehicledata(self, ctx: commands.Context):
        """Deliberately wipe stale vehicle data for ALL existing players (requirements #15)."""
        database.reset_all_vehicle_data()
        await ctx.send("All players' vehicle data has been reset (vehicle, fuel, condition, vehicles list).")


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
