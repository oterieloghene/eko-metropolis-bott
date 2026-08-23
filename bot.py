import logging
import os

import discord
from discord.ext import commands

import database
import permissions
from checks import WrongChannel, NotAtLocation, CurrentlyTraveling
from config import STARTING_LOCATION

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ekobot")

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=commands.DefaultHelpCommand())

COGS = [
    "cogs.location",
    "cogs.travel",
    "cogs.dealership",
    "cogs.economy",
    "cogs.repair",
    "cogs.admin",
]


@bot.event
async def on_ready():
    database.init_db()
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_member_join(member: discord.Member):
    """New players start at the Vehicle Dealership (requirements #20)."""
    database.get_or_create_player(member.id)
    await permissions.set_write_access(member.guild, member, STARTING_LOCATION, allowed=True)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, WrongChannel):
        await ctx.send(f"⛔ {error}")
        return
    if isinstance(error, NotAtLocation):
        await ctx.send(f"⛔ {error}")
        return
    if isinstance(error, CurrentlyTraveling):
        await ctx.send(f"⛔ {error}")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("⛔ You can't use that command right now.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Check `!help {ctx.command}`.")
        return

    log.exception("Unhandled command error", exc_info=error)
    await ctx.send("Something went wrong running that command.")


async def main():
    async with bot:
        for cog in COGS:
            await bot.load_extension(cog)
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN environment variable is not set.")
    import asyncio

    asyncio.run(main())
