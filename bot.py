import logging
import os
import asyncio

import discord
from discord.ext import commands

from aiohttp import web, ClientSession, ClientTimeout

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
    "cogs.carpool",
    "cogs.taxi",
    "cogs.travel",
    "cogs.dealership",
    "cogs.economy",
    "cogs.repair",
    "cogs.admin",
    "cogs.driving_school",

    # ------------------------------------------------------------
    # BRT / BUS TRANSPORTATION
    # ------------------------------------------------------------

    "cogs.brt_card",
    "cogs.bus",

    # ------------------------------------------------------------
    # FLIGHTS
    # ------------------------------------------------------------

    "cogs.flight",

    # ------------------------------------------------------------
    # PHONE — must load AFTER bus/brt_card/taxi/flight above,
    # since it looks up their commands by name.
    # ------------------------------------------------------------

    "cogs.phone",
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
# SELF-PING (KEEP-ALIVE FOR RENDER FREE WEB SERVICES)
# ================================================================
#
# Render's free Web Service plan spins the service down after
# ~15 minutes with no inbound HTTP traffic. A Discord bot never
# generates any HTTP traffic on its own, so left alone it will
# eventually get spun down mid-session.
#
# This background loop pings the service's OWN public /health
# URL every few minutes. That counts as real inbound traffic
# (it's a genuine round-trip out to Render's edge and back in),
# which resets Render's idle timer and keeps the service alive
# for free, without needing a paid background worker.
#
# RENDER_EXTERNAL_URL is set automatically by Render for every
# web service (e.g. "https://your-app.onrender.com"). Locally,
# or on any host that doesn't set it, this loop simply does
# nothing.
# ================================================================

SELF_PING_INTERVAL_SECONDS = 10 * 60  # 10 minutes


async def self_ping_loop():

    external_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if not external_url:

        log.info(
            "RENDER_EXTERNAL_URL not set — self-ping "
            "loop disabled (not running on Render, or "
            "not a web service)."
        )

        return

    ping_url = external_url.rstrip("/") + "/health"

    timeout = ClientTimeout(
        total=15
    )

    async with ClientSession(
        timeout=timeout
    ) as session:

        while True:

            await asyncio.sleep(
                SELF_PING_INTERVAL_SECONDS
            )

            try:

                async with session.get(
                    ping_url
                ) as response:

                    log.info(
                        f"Self-ping {ping_url} -> "
                        f"{response.status}"
                    )

            except Exception as error:

                # ------------------------------------------------
                # Never let a failed ping crash the bot — just
                # log it and try again next interval.
                # ------------------------------------------------

                log.warning(
                    f"Self-ping failed: {error}"
                )


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

    log.info(
        "REGISTERED COMMANDS: %s",
        sorted(
            command.name
            for command in bot.commands
        )
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

    # ------------------------------------------------------------
    # Start the self-ping keep-alive loop in the background.
    # ------------------------------------------------------------

    ping_task = asyncio.create_task(
        self_ping_loop()
    )

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
        # Stop the self-ping loop and cleanly stop the HTTP
        # server if Discord shuts down.
        # --------------------------------------------------------

        ping_task.cancel()

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
