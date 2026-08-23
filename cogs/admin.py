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

    # ========================================================
    # !SETLOCATION
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
                f"`{code}` is not a valid location code."
            )

            return

        player = database.get_or_create_player(
            member.id
        )

        old_code = player["location"]

        database.update_player(
            member.id,
            location=code,
            traveling=0
        )

        await permissions.move_write_access(
            ctx.guild,
            member,
            old_code=old_code,
            new_code=code
        )

        await ctx.send(
            f"Moved {member.mention} from "
            f"{LOCATIONS[old_code]['name']} to "
            f"{LOCATIONS[code]['name']}."
        )

    # ========================================================
    # !RESETALLLOCATIONS
    # ========================================================

    @commands.command(name="resetalllocations")
    @_is_admin()
    async def resetalllocations(
        self,
        ctx: commands.Context,
        code: str = "dealership"
    ):

        code = code.strip().lower()

        if code not in LOCATIONS:

            await ctx.send(
                f"`{code}` is not a valid location code."
            )

            return

        count = database.reset_all_locations(
            code
        )

        await ctx.send(
            f"✅ Reset **{count}** existing player(s) to "
            f"**{LOCATIONS[code]['name']}**."
        )

    # ========================================================
    # !RESETVEHICLEDATA
    # ========================================================

    @commands.command(name="resetvehicledata")
    @_is_admin()
    async def resetvehicledata(
        self,
        ctx: commands.Context
    ):

        database.reset_all_vehicle_data()

        await ctx.send(
            "✅ All players' vehicle data have been reset "
            "(vehicle, fuel, condition, vehicles list)."
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
            "🔒 Locking down channels — "
            "this may take a minute..."
        )

        guild = ctx.guild

        locked = 0
        failed = []

        # ====================================================
        # LOCK ALL LOCATION CHANNELS FOR @EVERYONE
        # ====================================================

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

        # ====================================================
        # RESTORE ACCESS FOR EXISTING PLAYERS
        # ====================================================

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

                # ------------------------------------------------
                # Give this player write access ONLY to their
                # current location.
                # ------------------------------------------------

                for code in LOCATIONS:

                    allowed = (
                        code == current_location
                    )

                    try:

                        await permissions.set_write_access(
                            guild,
                            member,
                            code,
                            allowed
                        )

                    except (
                        discord.Forbidden,
                        discord.HTTPException
                    ) as error:

                        print(
                            f"[LOCKDOWN ACCESS ERROR] "
                            f"{member.id} / {code}: {error}"
                        )

                synced += 1

            except Exception as error:

                print(
                    f"[LOCKDOWN SYNC ERROR] "
                    f"{player.get('user_id')}: {error}"
                )

        # ====================================================
        # RESULT
        # ====================================================

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

async def setup(bot: commands.Bot):

    await bot.add_cog(
        AdminCog(bot)
    )
