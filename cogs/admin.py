import discord
from discord.ext import commands

import database
import permissions

from cogs.give import grant_starter_items

from config import (
    LOCATIONS,
    VEHICLES,
    STARTING_BALANCE,
    STARTING_LOCATION,
    TAXI_DRIVER_ROLE,
    DISPATCH_RIDER_ROLE,
    BRT_CARD_ROLE,
)


def _is_admin():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.guild_permissions.administrator

    return commands.check(predicate)


class AdminCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ========================================================
    # !REGISTERPLAYERS
    # ========================================================

    @commands.command(name="registerplayers")
    @_is_admin()
    async def registerplayers(
        self,
        ctx: commands.Context
    ):

        registered = 0
        existing = 0
        bank_accounts_opened = 0

        for member in ctx.guild.members:

            if member.bot:
                continue

            player = database.get_player(member.id)

            if player is not None:
                existing += 1

                # Existing players may pre-date the Bank App —
                # backfill an account for them too so nobody
                # gets left without one.
                if database.create_bank_account(member.id):
                    bank_accounts_opened += 1

                continue

            database.get_or_create_player(member.id)

            # Every registered player needs a bank account to
            # use the Bank App on their phone. The account
            # shares the player's existing Naira balance, so no
            # separate top-up is needed here.
            if database.create_bank_account(member.id):
                bank_accounts_opened += 1

            # Stock the player's inventory with a few starter
            # props (drawn from the fixed 4-item pool) so !give
            # and dispatch-delivery testing has something to
            # hand off right away.
            grant_starter_items(member.id)

            # Give the newly registered player writing access
            # ONLY to their starting location.
            try:
                for code in LOCATIONS:
                    await permissions.set_write_access(
                        ctx.guild,
                        member,
                        code,
                        allowed=False
                    )

                await permissions.set_write_access(
                    ctx.guild,
                    member,
                    STARTING_LOCATION,
                    allowed=True
                )

            except Exception as error:
                print(
                    f"[REGISTER PERMISSION ERROR] "
                    f"{member.id}: {error}"
                )

            registered += 1

        await ctx.send(
            f"✅ **Player registration completed.**\n\n"
            f"👤 New players registered: **{registered}**\n"
            f"📋 Existing players already registered: **{existing}**\n"
            f"🏦 Bank accounts opened: **{bank_accounts_opened}**\n"
            f"🎁 Starter items granted to each new player: **3**\n"
            f"📍 Starting location: "
            f"**{LOCATIONS[STARTING_LOCATION]['name']}**\n"
            f"💰 Starting balance: **₦{STARTING_BALANCE:,}**"
        )

    # ========================================================
    # !SETLOCATION
    #
    # Usage:
    # !setlocation @Player dealership
    # ========================================================

    @commands.command(name="setlocation")
    @_is_admin()
    async def setlocation(
        self,
        ctx: commands.Context,
        member: discord.Member,
        code: str
    ):

        code = code.strip().lower()

        if code not in LOCATIONS:

            await ctx.send(
                f"⛔ `{code}` is not a valid location code.\n\n"
                f"Example:\n"
                f"`!setlocation @Player dealership`"
            )

            return

        player = database.get_or_create_player(
            member.id
        )

        old_code = str(
            player["location"]
        ).strip().lower()

        database.update_player(
            member.id,
            location=code,
            traveling=0
        )

        try:

            await permissions.move_write_access(
                ctx.guild,
                member,
                old_code=old_code,
                new_code=code
            )

        except Exception as error:

            await ctx.send(
                f"⚠️ Database location was updated, "
                f"but Discord permission sync failed:\n"
                f"`{error}`"
            )

            return

        old_name = (
            LOCATIONS[old_code]["name"]
            if old_code in LOCATIONS
            else old_code
        )

        await ctx.send(
            f"✅ Moved {member.mention} from "
            f"**{old_name}** to "
            f"**{LOCATIONS[code]['name']}**."
        )

    # ========================================================
    # !RESETALLLOCATIONS
    #
    # Usage:
    # !resetalllocations dealership
    #
    # Moves every EXISTING database player to the
    # specified location.
    #
    # Does NOT delete player records.
    # Does NOT change balance.
    # Does NOT remove vehicles.
    # ========================================================

    @commands.command(name="resetalllocations")
    @_is_admin()
    async def resetalllocations(
        self,
        ctx: commands.Context,
        code: str = STARTING_LOCATION
    ):

        code = code.strip().lower()

        if code not in LOCATIONS:

            await ctx.send(
                f"⛔ `{code}` is not a valid location code."
            )

            return

        players = database.all_players()

        reset_count = 0
        permission_count = 0
        failed = []

        for player in players:

            try:

                user_id = int(
                    player["user_id"]
                )

                member = ctx.guild.get_member(
                    user_id
                )

                old_code = str(
                    player["location"]
                ).strip().lower()

                # Update database.
                database.update_player(
                    user_id,
                    location=code,
                    traveling=0
                )

                reset_count += 1

                # Synchronize Discord permissions.
                if member is not None:

                    await permissions.move_write_access(
                        ctx.guild,
                        member,
                        old_code=old_code,
                        new_code=code
                    )

                    permission_count += 1

            except Exception as error:

                failed.append(
                    f"{player['user_id']}: {error}"
                )

        result = (
            f"✅ **Location reset completed.**\n\n"
            f"📍 New location: "
            f"**{LOCATIONS[code]['name']}**\n"
            f"👤 Players reset: **{reset_count}**\n"
            f"🔐 Permissions synced: **{permission_count}**"
        )

        if failed:

            result += (
                f"\n\n⚠️ **Failed: {len(failed)}**\n"
            )

            result += "\n".join(
                f"• `{item}`"
                for item in failed[:20]
            )

        await ctx.send(result)

    # ========================================================
    # !RESETVEHICLEDATA
    #
    # Clears vehicle data from the DATABASE and removes
    # vehicle ownership roles from Discord.
    #
    # Does NOT change:
    # - Balance
    # - Location
    # - Traveling status
    # ========================================================

    @commands.command(name="resetvehicledata")
    @_is_admin()
    async def resetvehicledata(
        self,
        ctx: commands.Context
    ):

        status = await ctx.send(
            "🚗 **Resetting vehicle data...**\n"
            "Please wait."
        )

        removed_roles = 0
        role_failures = 0

        # ----------------------------------------------------
        # Remove all configured vehicle roles from every
        # member in the server.
        # ----------------------------------------------------

        vehicle_role_names = {
            cfg.get("role")
            for cfg in VEHICLES.values()
            if cfg.get("role")
        }

        vehicle_roles = []

        for role_name in vehicle_role_names:

            role = discord.utils.get(
                ctx.guild.roles,
                name=role_name
            )

            if role is not None:
                vehicle_roles.append(role)

        for member in ctx.guild.members:

            if member.bot:
                continue

            for role in vehicle_roles:

                if role not in member.roles:
                    continue

                try:

                    await member.remove_roles(
                        role,
                        reason="Vehicle data reset"
                    )

                    removed_roles += 1

                except discord.Forbidden:

                    role_failures += 1

                except discord.HTTPException:

                    role_failures += 1

        # ----------------------------------------------------
        # Clear vehicle information from database.
        # ----------------------------------------------------

        database.reset_all_vehicle_data()

        result = (
            "✅ **VEHICLE DATA RESET COMPLETED**\n\n"
            "🚗 Vehicle: **None**\n"
            "⛽ Fuel: **0**\n"
            "🔧 Condition: **100**\n"
            "📋 Vehicle list: **Empty**\n"
            f"🎭 Vehicle roles removed: **{removed_roles}**"
        )

        if role_failures:

            result += (
                f"\n⚠️ Role removal failures: "
                f"**{role_failures}**"
            )

        await status.edit(
            content=result
        )

    # ========================================================
    # !RESETDATABASE
    #
    # COMPLETE PLAYER DATABASE RESET
    #
    # This command:
    #
    # 1. Removes ALL vehicle ownership roles from Discord.
    # 2. Deletes ALL player records from SQLite.
    # 3. Leaves the players themselves in Discord.
    #
    # Afterward:
    #
    # !registerplayers
    #
    # creates everybody again with:
    #
    # Balance       = STARTING_BALANCE
    # Location      = STARTING_LOCATION
    # Vehicle       = None
    # Vehicle list  = []
    # Fuel          = 0
    # Condition     = 100
    # Traveling     = 0
    #
    # Usage:
    #
    # !resetdatabase
    # ========================================================

    @commands.command(name="resetdatabase")
    @_is_admin()
    async def resetdatabase(
        self,
        ctx: commands.Context
    ):

        status = await ctx.send(
            "⚠️ **Resetting the entire player database...**\n"
            "Removing old vehicle roles, bank accounts, and "
            "player records.\n"
            "Please wait."
        )

        try:

            # =================================================
            # STEP 1 — REMOVE OLD VEHICLE + JOB ROLES
            #
            # Vehicle-ownership roles (from VEHICLES) plus every
            # role a player only ever holds because a command
            # granted it to them (Taxi Driver, Dispatch Rider,
            # BRT Card) — none of these should survive a reset,
            # since the tables backing them (taxi_drivers,
            # dispatch_riders, brt_cards) are being wiped in Step
            # 2 right along with them. Staff-assigned roles like
            # Mechanic and Officer are left untouched — those
            # aren't granted or removed by the bot.
            # =================================================

            removed_roles = 0
            role_failures = 0

            vehicle_role_names = {
                cfg.get("role")
                for cfg in VEHICLES.values()
                if cfg.get("role")
            }

            job_role_names = {
                TAXI_DRIVER_ROLE,
                DISPATCH_RIDER_ROLE,
                BRT_CARD_ROLE,
            }

            vehicle_roles = []

            for role_name in vehicle_role_names | job_role_names:

                role = discord.utils.get(
                    ctx.guild.roles,
                    name=role_name
                )

                if role is not None:
                    vehicle_roles.append(role)

            for member in ctx.guild.members:

                if member.bot:
                    continue

                for role in vehicle_roles:

                    if role not in member.roles:
                        continue

                    try:

                        await member.remove_roles(
                            role,
                            reason="Complete database reset"
                        )

                        removed_roles += 1

                    except discord.Forbidden:

                        role_failures += 1

                    except discord.HTTPException:

                        role_failures += 1

            # =================================================
            # STEP 2 — DELETE ALL PLAYER RECORDS
            # =================================================

            deleted_players = database.reset_database()

            # =================================================
            # STEP 3 — RESULT
            #
            # There are no players in the database anymore,
            # so there is intentionally nobody to synchronize.
            #
            # !REGISTERPLAYERS must be run afterward.
            # =================================================

            result = (
                "✅ **DATABASE RESET COMPLETED**\n\n"
                f"👤 Player records deleted: "
                f"**{deleted_players}**\n"
                f"🎭 Vehicle + job roles removed: "
                f"**{removed_roles}**\n"
                f"🚕 Taxi/dispatch registrations cleared: "
                f"**All**\n"
                f"🏦 Bank accounts closed: **All**\n"
                f"💰 New-player balance: "
                f"**₦{STARTING_BALANCE:,}**\n"
                f"📍 Starting location: "
                f"**{LOCATIONS[STARTING_LOCATION]['name']}**\n"
                f"🚗 Vehicle: **None**\n"
                f"⛽ Fuel: **0**\n"
                f"🔧 Vehicle condition: **100**\n"
                f"📋 Vehicle list: **Empty**\n"
                f"🚦 Traveling: **No**\n\n"
                "➡️ Run `!registerplayers` to create "
                "fresh player records again."
            )

            if role_failures:

                result += (
                    f"\n\n⚠️ Vehicle role removal failures: "
                    f"**{role_failures}**"
                )

            await status.edit(
                content=result
            )

        except Exception as error:

            await status.edit(
                content=(
                    "❌ **Database reset failed.**\n\n"
                    f"`{error}`"
                )
            )

    # ========================================================
    # !LOCKDOWNCHANNELS
    # ========================================================

    @commands.command(name="lockdownchannels")
    @_is_admin()
    async def lockdownchannels(
        self,
        ctx: commands.Context
    ):

        status_message = await ctx.send(
            "🔒 **Locking down channels...**\n"
            "This may take a minute."
        )

        guild = ctx.guild

        locked = 0
        failed = []

        # ----------------------------------------------------
        # LOCK LOCATION CHANNELS FOR @EVERYONE
        # ----------------------------------------------------

        for code in LOCATIONS:

            channel = permissions.get_channel_for_code(
                guild,
                code
            )

            if channel is None:

                failed.append(
                    f"{code}: channel not found"
                )

                continue

            try:

                overwrite = channel.overwrites_for(
                    guild.default_role
                )

                overwrite.send_messages = False

                await channel.set_permissions(
                    guild.default_role,
                    overwrite=overwrite
                )

                locked += 1

            except discord.Forbidden:

                failed.append(
                    f"{code}: bot lacks permission"
                )

            except discord.HTTPException as error:

                failed.append(
                    f"{code}: Discord error {error}"
                )

            except Exception as error:

                failed.append(
                    f"{code}: {error}"
                )

        # ----------------------------------------------------
        # RESTORE CURRENT LOCATION ACCESS
        # ----------------------------------------------------

        players = database.all_players()

        synced = 0

        for player in players:

            try:

                member = guild.get_member(
                    int(player["user_id"])
                )

                if member is None:
                    continue

                current_location = str(
                    player["location"]
                ).strip().lower()

                if current_location not in LOCATIONS:
                    continue

                # Remove location-specific access everywhere.
                for code in LOCATIONS:

                    await permissions.set_write_access(
                        guild,
                        member,
                        code,
                        allowed=False
                    )

                # Grant access only to current location.
                await permissions.set_write_access(
                    guild,
                    member,
                    current_location,
                    allowed=True
                )

                synced += 1

            except Exception as error:

                print(
                    f"[LOCKDOWN SYNC ERROR] "
                    f"{player['user_id']}: {error}"
                )

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        result = (
            "✅ **Lockdown completed.**\n\n"
            f"🔒 Channels locked: **{locked}**\n"
            f"👤 Players synced: **{synced}**"
        )

        if failed:

            result += (
                f"\n\n⚠️ **Channels that failed: "
                f"{len(failed)}**\n"
            )

            result += "\n".join(
                f"• `{item}`"
                for item in failed[:20]
            )

        await status_message.edit(
            content=result
        )


# ============================================================
# COG SETUP
# ============================================================

async def setup(
    bot: commands.Bot
):
    await bot.add_cog(
        AdminCog(bot)
    )
