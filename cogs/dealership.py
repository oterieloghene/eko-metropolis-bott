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
            # Company vehicles (Bicycle/Motorcycle for dispatch,
            # same idea as the taxi cars) aren't sold here — they
            # have no price/quantity and are handed out free via
            # !becomedispatchrider instead. Skip them in the
            # dealership catalog.
            if cfg.get("price") is None:
                continue
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

        if cfg.get("price") is None:
            await ctx.send(f"`{match}` isn't sold here — see `!cars`.")
            return

        player = database.get_player(ctx.author.id)

        # Personal vehicles bought at the dealership are tracked
        # separately from commercial vehicles (taxi/dispatch/
        # police); a player can own several personal cars too.
        owned = database.get_vehicles(ctx.author.id)
        already_owned = next(
            (v for v in owned if v.get("name") == match and v.get("type") == "personal"),
            None,
        )
        if already_owned:
            await ctx.send(f"You already own a **{match}**. You can't buy a second one of the same model.")
            return

        if cfg["quantity"] <= 0:
            await ctx.send(f"{match} is currently out of stock.")
            return

        if player["balance"] < cfg["price"]:
            await ctx.send(f"You need ₦{cfg['price']:,} but only have ₦{player['balance']:,}.")
            return

        database.update_player(
            ctx.author.id,
            balance=player["balance"] - cfg["price"],
            location=DEALERSHIP_CODE,  # purchase keeps the player at the dealership
        )

        database.add_vehicle(
            ctx.author.id,
            name=match,
            vehicle_type="personal",
            location=DEALERSHIP_CODE,
            condition=cfg["condition"],
            fuel=cfg["fuel_capacity"],
            select=True,  # newly bought vehicle becomes the active one
        )

        role_name = cfg.get("role")
        if role_name:
            role = discord.utils.get(ctx.guild.roles, name=role_name)
            if role:
                try:
                    await ctx.author.add_roles(role, reason="Vehicle purchase")
                except discord.Forbidden:
                    pass

        await ctx.send(f"🎉 {ctx.author.mention} bought a **{match}** for ₦{cfg['price']:,}! It's now your active vehicle.")

    @commands.command(name="vehicle", aliases=["vehicles"])
    async def vehicle(self, ctx: commands.Context):
        """List every vehicle the player owns (personal, taxi, dispatch, police)."""
        owned = database.get_vehicles(ctx.author.id)

        if not owned:
            await ctx.send("You don't own a vehicle yet.")
            return

        embed = discord.Embed(title="🚗 Your Vehicles", color=discord.Color.dark_blue())

        for v in owned:
            loc = LOCATIONS.get(v.get("location"))
            loc_name = loc["name"] if loc else "Unknown"
            active = " ✅ (active)" if v.get("selected") else ""

            embed.add_field(
                name=f"{v.get('name', 'Vehicle')}{active}",
                value=(
                    f"Type: {v.get('type', 'personal')}\n"
                    f"Location: {loc_name}\n"
                    f"Fuel: {v.get('fuel', 0):.1f}\n"
                    f"Condition: {v.get('condition', 100):.0f}"
                ),
                inline=True,
            )

        embed.set_footer(text="Use !usevehicle <name> to switch your active vehicle.")
        await ctx.send(embed=embed)

    @commands.command(name="usevehicle")
    async def usevehicle(self, ctx: commands.Context, *, name: str = None):
        """Select which owned vehicle is used for driving."""
        if not name:
            await ctx.send("Usage: `!usevehicle <name>` — see `!vehicle` for your owned vehicles.")
            return

        selected = database.select_vehicle(ctx.author.id, name.strip())

        if not selected:
            await ctx.send(f"You don't own a vehicle matching `{name}`. Check `!vehicle`.")
            return

        await ctx.send(f"🔑 **{selected['name']}** is now your active vehicle.")


async def setup(bot: commands.Bot):
    await bot.add_cog(DealershipCog(bot))
