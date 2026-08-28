import discord
from discord.ext import commands

import database
from config import REPAIR_COST_PER_POINT, MECHANIC_ROLE, LOCATIONS

# Split of every repair payment: the mechanic keeps 25%, the
# other 75% goes to the auto shop company. The auto shop has no
# tracked balance of its own (same as the taxi/dispatch
# companies) — its cut simply isn't paid out to anyone, exactly
# like TAXI_COMPANY_CUT in config.py.
MECHANIC_CUT = 0.25


class RepairCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="fixcar")
    async def fixcar(self, ctx: commands.Context, member: discord.Member = None):
        """
        Mechanic-only. There is no self-service repair -- a car cannot leave
        the channel it broke down in, so a Mechanic must travel to wherever
        the car currently is and run this command from that same channel.

        The mechanic keeps 25% of whatever is actually paid (full or
        partial repair); the remaining 75% goes to the auto shop and
        isn't paid out to anyone, same as the taxi company's cut.
        """
        mechanic_role = discord.utils.get(ctx.author.roles, name=MECHANIC_ROLE)
        if mechanic_role is None:
            await ctx.send(f"\u26d4 Only someone with the **{MECHANIC_ROLE}** role can repair vehicles.")
            return

        if member is None:
            await ctx.send("Usage: `!fixcar @player`")
            return

        target = database.get_player(member.id)
        if not target or not target["vehicle"]:
            await ctx.send(f"{member.mention} doesn't own a vehicle.")
            return

        vehicle_code = target["vehicle_location"]
        vehicle_loc = LOCATIONS.get(vehicle_code)
        if vehicle_loc is None:
            await ctx.send("That vehicle's location looks corrupted -- contact an admin.")
            return

        # The car is wherever it broke down / currently sits -- the mechanic
        # must be physically there themselves, not just typing in that
        # channel from elsewhere.
        mechanic = database.get_or_create_player(ctx.author.id)
        if mechanic["location"] != vehicle_code:
            await ctx.send(
                f"\u26d4 You need to go to **{vehicle_loc['name']}** (#{vehicle_loc['channel']}) "
                f"to work on this vehicle."
            )
            return

        if ctx.channel.name != vehicle_loc["channel"]:
            await ctx.send(f"\u26d4 This has to be done in #{vehicle_loc['channel']}.")
            return

        needed = 100 - target["vehicle_condition"]
        if needed <= 0:
            await ctx.send(f"{member.mention}'s vehicle is already in perfect condition.")
            return

        cost = round(needed * REPAIR_COST_PER_POINT)
        owner_balance = target["balance"]

        if owner_balance < cost:
            affordable_points = owner_balance / REPAIR_COST_PER_POINT
            spent = owner_balance

            database.update_player(
                member.id,
                balance=0,
                vehicle_condition=target["vehicle_condition"] + affordable_points,
            )

            mechanic_payout = round(spent * MECHANIC_CUT)

            if mechanic_payout > 0:
                database.update_player(
                    ctx.author.id,
                    balance=mechanic["balance"] + mechanic_payout,
                )

            await ctx.send(
                f"\U0001f527 {ctx.author.mention} could only afford a partial repair on {member.mention}'s vehicle: "
                f"+{affordable_points:.0f} condition (\u20a6{spent:,} spent).\n"
                f"\U0001f4b0 {ctx.author.mention}'s cut: \u20a6{mechanic_payout:,}"
            )
            return

        database.update_player(member.id, balance=owner_balance - cost, vehicle_condition=100)

        mechanic_payout = round(cost * MECHANIC_CUT)

        if mechanic_payout > 0:
            database.update_player(
                ctx.author.id,
                balance=mechanic["balance"] + mechanic_payout,
            )

        await ctx.send(
            f"\U0001f527 {ctx.author.mention} fully repaired {member.mention}'s {target['vehicle']} for \u20a6{cost:,}.\n"
            f"\U0001f4b0 {ctx.author.mention}'s cut: \u20a6{mechanic_payout:,}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RepairCog(bot))
