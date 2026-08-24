import logging
import os
import asyncio

import discord
from discord.ext import commands

from aiohttp import web

import database
import permissions
from checks import WrongChannel, NotAtLocation, CurrentlyTraveling
from config import STARTING_LOCATION


# ================================================================
# LOGGING
# ================================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ekobot")


# ================================================================
# ENVIRONMENT
# ================================================================

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")


# ================================================================
# DISCORD INTENTS
# ================================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


# ================================================================
# BOT
# ================================================================

bot = commands.Bot(
    command_prefix=COMMAND_PREFIX,
    intents=intents,
    help_command=commands.DefaultHelpCommand()
)


# ================================================================
# COGS
# ================================================================

COGS = [
    "cogs.location",
    "cogs.travel",
    "cogs.dealership",
    "cogs.economy",
    "cogs.repair",
    "cogs.admin",
]


# ================================================================
# RENDER HEALTH SERVER
# ================================================================

async def health_check(request):
    return web.Response(
        text="Eko Metropolis Bot is online."
    )


async def start_web_server():
    """
    Small HTTP server required because the bot is running
    as a Render Web Service.

    The Discord bot itself does NOT use this server.
    It only keeps Render's health/port check satisfied.
    """

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health_check
    )

    app.router.add_get(
        "/health",
        health_check
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    log.info(
        f"Render health server listening on port {port}"
    )

    return runner


# ================================================================
# BOT READY
# ================================================================

@bot.event
async def on_ready():

    database.init_db()

    # ------------------------------------------------------------
    # Ensure the bot can send messages in every registered
    # location channel.
    # ------------------------------------------------------------

    for guild in bot.guilds:

        await permissions.ensure_bot_channel_permissions(
            guild
        )

    log.info(
        f"Logged in as {bot.user} "
        f"(id={bot.user.id})"
    )


# ================================================================
# MEMBER JOIN
# ================================================================

@bot.event
async def on_member_join(
    member: discord.Member
):

    """
    New players start at the Vehicle Dealership.
    """

    database.get_or_create_player(
        member.id
    )

    await permissions.set_write_access(
        member.guild,
        member,
        STARTING_LOCATION,
        allowed=True
    )


# ================================================================
# COMMAND ERROR HANDLER
# ================================================================

@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError
):

    # ------------------------------------------------------------
    # Unknown commands
    # ------------------------------------------------------------

    if isinstance(
        error,
        commands.CommandNotFound
    ):

        return

    # ------------------------------------------------------------
    # Wrong channel
    # ------------------------------------------------------------

    if isinstance(
        error,
        WrongChannel
    ):

        await ctx.send(
            f"⛔ {error}"
        )

        return

    # ------------------------------------------------------------
    # Not at location
    # ------------------------------------------------------------

    if isinstance(
        error,
        NotAtLocation
    ):

        await ctx.send(
            f"⛔ {error}"
        )

        return

    # ------------------------------------------------------------
    # Currently travelling
    # ------------------------------------------------------------

    if isinstance(
        error,
        CurrentlyTraveling
    ):

        await ctx.send(
            f"⛔ {error}"
        )

        return

    # ------------------------------------------------------------
    # Generic check failure
    # ------------------------------------------------------------

    if isinstance(
        error,
        commands.CheckFailure
    ):

        await ctx.send(
            "⛔ You can't use that command right now."
        )

        return

    # ------------------------------------------------------------
    # Missing argument
    # ------------------------------------------------------------

    if isinstance(
        error,
        commands.MissingRequiredArgument
    ):

        await ctx.send(
            f"Missing argument: "
            f"`{error.param.name}`. "
            f"Check `!help {ctx.command}`."
        )

        return

    # ------------------------------------------------------------
    # Unknown/unhandled error
    # ------------------------------------------------------------

    log.exception(
        "Unhandled command error",
        exc_info=error
    )

    try:

        await ctx.send(
            "Something went wrong running that command."
        )

    except discord.HTTPException:

        pass


# ================================================================
# MAIN
# ================================================================

async def main():

    # ------------------------------------------------------------
    # Start Render HTTP health server.
    # ------------------------------------------------------------

    web_runner = await start_web_server()

    try:

        # --------------------------------------------------------
        # Load all cogs.
        # --------------------------------------------------------

        async with bot:

            for cog in COGS:

                log.info(
                    f"Loading extension: {cog}"
                )

                await bot.load_extension(
                    cog
                )

            # ----------------------------------------------------
            # Start Discord bot.
            # ----------------------------------------------------

            await bot.start(
                TOKEN
            )

    finally:

        # --------------------------------------------------------
        # Cleanly stop HTTP server if Discord shuts down.
        # --------------------------------------------------------

        await web_runner.cleanup()


# ================================================================
# ENTRY POINT
# ================================================================

if __name__ == "__main__":

    if not TOKEN:

        raise SystemExit(
            "DISCORD_TOKEN environment variable "
            "is not set."
        )

    asyncio.run(
        main()
    )
