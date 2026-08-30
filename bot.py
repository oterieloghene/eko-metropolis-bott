import logging
import os
import asyncio

import discord
from discord.ext import commands

from aiohttp import web, ClientSession, ClientTimeout

import database
import permissions
import routing
from checks import (
    WrongChannel,
    NotAtLocation,
    CurrentlyTraveling,
    NotInArea,
    WrongAreaKind,
    Unconscious,
)
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
# GLOBAL CHECK -- UNCONSCIOUS PLAYERS CAN'T DO ANYTHING
# ================================================================
#
# Applies to every command from every cog, including the phone
# (cogs/phone.py's commands are regular bot commands too, so this
# blocks phone use for free). See config.py's COLLAPSE /
# UNCONSCIOUS block and cogs/walk.py for how a player ends up
# marked unconscious in the first place.
#
# Deliberately checks the DATABASE flag (players.unconscious),
# not the Discord role -- the role can lag a beat behind a fresh
# collapse/resuscitation, but the database write always happens
# first in cogs/walk.py and cogs/ambulance.py.
# ================================================================

@bot.check
async def block_unconscious_players(ctx: commands.Context) -> bool:

    if not isinstance(ctx.author, discord.Member):
        return True

    if database.is_unconscious(ctx.author.id):
        raise Unconscious()

    return True


# ================================================================
# COGS
# ================================================================

COGS = [
    "cogs.weather",
    "cogs.daynight",
    "cogs.map",
    "cogs.carpool",
    "cogs.taxi",

    # cogs.dispatch imports build_multi_leg_route from
    # cogs.carpool directly (reuses its nearest-stop-first
    # router for multi-order delivery runs), so it must load
    # after cogs.carpool. travel.py will import cogs.dispatch
    # directly (same pattern as cogs.taxi) once the !drive hook
    # is wired in.
    "cogs.dispatch",

    "cogs.travel",
    "cogs.dealership",
    "cogs.economy",
    "cogs.repair",
    "cogs.admin",

    # Location/sub-location registration (!location-registration,
    # !create-sub-location, !remove-location, !remove-sub-location).
    # Admin-only. Loads after cogs.admin just for grouping — no
    # actual dependency between the two.
    "cogs.location_admin",

    # Phase 2 banking (!create-account, !create-current-account,
    # !with, !transfer, !cash-bal, !pay, !view-balances, !cb-with,
    # !adjust). Loads after cogs.location_admin since its
    # sub-location gates (front-desk/atm/cbe-chairman) depend on
    # those rooms already being registrable, though there's no
    # hard import dependency.
    "cogs.banking",

    # Phase 4 business registration (!business-registration) +
    # Phase 5 shop/inventory system (!add, !menu, !buy, !sell,
    # !close-register, !order). business_shop.py imports
    # BUSINESS_TYPE_CATEGORIES/SHOP_CATEGORIES from business_admin.py
    # at module level, so business_admin must load first; both load
    # after cogs.banking since a business needs !create-business-account
    # before its shop commands are useful (not a hard import
    # dependency, just load-order sanity).
    "cogs.business_admin",
    "cogs.business_shop",

    # !manufacture / !import — the admin-only goods catalog
    # (cogs/business_shop.py's !sell eventually draws real stock
    # from here once !supply exists). Imports SHOP_CATEGORIES/
    # SUBCATEGORIES/CATEGORY_LABELS from business_admin.py at
    # module level, so business_admin must load first.
    "cogs.manufacturing",

    "cogs.driving_school",

    # ------------------------------------------------------------
    # PLAYER STATS -- !stats + background passive decay. No
    # cross-cog dependencies (walking's distance-based drain, once
    # it exists in cogs/walk.py, will call database.adjust_stats()
    # directly rather than reaching into this cog).
    # ------------------------------------------------------------

    "cogs.stats",

    # ------------------------------------------------------------
    # WALKING -- !walk / !trek + collapse/unconscious + resuscitate.
    # Reuses routing.find_route() (same graph as driving/buses).
    # No cross-cog dependencies.
    # ------------------------------------------------------------

    "cogs.walk",

    # ------------------------------------------------------------
    # AMBULANCE -- fleet system (mirrors cogs.police) + patient
    # transport to the hospital. No cross-cog dependencies, but
    # loads after cogs.walk since it treats/clears the same
    # unconscious state cogs.walk sets.
    # ------------------------------------------------------------

    "cogs.ambulance",

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
    # HOTELS — own file. flight.py imports cogs.hotel directly to
    # trigger room cleanup on return/forfeiture (same pattern as
    # travel.py -> cogs.mechanic below); phone.py also imports it
    # for the Hotel menu button.
    # ------------------------------------------------------------

    "cogs.hotel",

    # ------------------------------------------------------------
    # OVERSEAS AREAS (Downtown Dubai, Dubai Desert, Dubai Marina,
    # Paradise Resort, Blue Lagoon, Ocean Excursion) + their shops,
    # events, and the AED/MVR wallet. flight.py imports cogs.areas
    # directly (same pattern as cogs.hotel above) to post the
    # arrival "Where do you want to go?" menu and to clean up area
    # thread membership on return/forfeiture.
    # ------------------------------------------------------------

    "cogs.areas",
    "cogs.shops",
    "cogs.events",
    "cogs.wallet",

    # ------------------------------------------------------------
    # MECHANIC DISPATCH / CONTACTS + TEXTING
    #
    # Own files, own commands — travel.py imports cogs.mechanic
    # directly (same pattern as cogs.taxi) so a mechanic can
    # !drive to a job, but neither of these touches taxi.py,
    # repair.py, or anything else above.
    # ------------------------------------------------------------

    "cogs.mechanic",
    "cogs.contacts",

    # ------------------------------------------------------------
    # GIVE — !give @player (same-location item hand-off) + !inv
    # (personal inventory viewer). No cross-cog dependencies, so it
    # can load anywhere.
    # ------------------------------------------------------------

    "cogs.give",

    # ------------------------------------------------------------
    # POLICE — patrol car fleet (!purchasepd/!assignpd/
    # !retrievepd/!pdfleet) + !patrol. No cross-cog dependencies
    # (self-contained movement, doesn't call into cogs.travel).
    # ------------------------------------------------------------

    "cogs.police",

    # ------------------------------------------------------------
    # EMERGENCY — !emergency police / !emergency hospital. Posts
    # a role-ping alert into the existing police/hospital
    # LOCATIONS channels. No cross-cog dependencies.
    # ------------------------------------------------------------

    "cogs.emergency",

    # ------------------------------------------------------------
    # PHONE — must load AFTER bus/brt_card/taxi/flight/mechanic/
    # contacts/emergency above, since it looks up their commands
    # by name.
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
    # Merge every dynamically registered location (businesses,
    # !location-registration) into the road graph. Must happen
    # after init_db() creates the `locations` table, and before
    # players can !walk/!drive/!taxi/!dispatch to them.
    # ------------------------------------------------------------

    routing.sync_dynamic_locations()

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
    # Unconscious
    # ------------------------------------------------------------

    if isinstance(
        error,
        Unconscious
    ):

        await ctx.send(
            f"\U0001F4A4 {error}"
        )

        return

    # ------------------------------------------------------------
    # Not in an area thread / wrong kind of area
    # ------------------------------------------------------------

    if isinstance(
        error,
        (NotInArea, WrongAreaKind)
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
