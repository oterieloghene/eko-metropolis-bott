"""
Emergency App — !emergency police / !emergency hospital.

Also reachable from the phone (see cogs/phone.py's EmergencyView
— "Call Police" / "Call Ambulance" buttons), which invokes this
same command through the shared _invoke() helper, same as every
other phone screen.

FLOW:

    1. Player runs !emergency police (or hospital). The caller's
       CURRENT DATABASE LOCATION is looked up (never the channel
       they typed it in) and posted, along with a role-mention
       (@Officer or @Medic Staff — see EMERGENCY_SERVICES in
       config.py), into that service's own LOCATIONS channel
       (precint-reception / hospital-lobby — already exist, no
       new channels needed).

    2. The alert carries a single "Accept" button. Only someone
       holding the matching responder role can press it —
       anyone else gets an ephemeral "you don't have that role"
       bounce, same pattern used elsewhere for role-gated
       buttons.

    3. TIME WINDOW: the alert stays open for
       EMERGENCY_RESPONSE_WINDOW_SECONDS. If nobody accepts in
       time, it deletes itself — at that point there's nothing
       left to click, so a late tap simply can't happen.

    4. If someone DOES accept, the alert is resolved immediately
       (edited to show who's responding, button disabled, then
       deleted after a short delay) so a second responder can't
       also accept the same call.

Self-contained: doesn't touch any other cog's internals, and
doesn't write any new database state — this is a notification
system layered on top of the existing player location + role
setup.
"""

import asyncio

import discord
from discord.ext import commands

import database
import permissions
from config import (
    EMERGENCY_MESSAGE_DELETE_DELAY_SECONDS,
    EMERGENCY_RESPONSE_WINDOW_SECONDS,
    EMERGENCY_SERVICES,
    LOCATIONS,
)


EMERGENCY_COLOR = discord.Color.red()


async def _delete_after_delay(message: discord.Message, delay: float) -> None:

    await asyncio.sleep(delay)

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def _location_name(code: str | None) -> str:

    loc = LOCATIONS.get(code) if code else None
    return loc["name"] if loc else "an unknown location"


class EmergencyAlertView(discord.ui.View):
    """
    The Accept button on a posted alert. Handles the response
    window itself — see on_timeout — and guarantees only ONE
    responder can ever successfully accept via the _resolved
    flag checked at the top of accept().
    """

    def __init__(
        self,
        service_key: str,
        responder_role_name: str,
        caller: discord.abc.User,
        caller_location_name: str
    ):
        super().__init__(timeout=EMERGENCY_RESPONSE_WINDOW_SECONDS)
        self.service_key = service_key
        self.responder_role_name = responder_role_name
        self.caller = caller
        self.caller_location_name = caller_location_name
        self.message: discord.Message | None = None
        self._resolved = False

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="\u2705")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):

        member = interaction.user

        has_role = discord.utils.get(
            getattr(member, "roles", []), name=self.responder_role_name
        )

        if not has_role:

            await interaction.response.send_message(
                f"\U0001f6ab You need the **{self.responder_role_name}** "
                f"role to respond to this.",
                ephemeral=True
            )

            return

        # A second responder tapping Accept a moment after the
        # first — the alert is already spoken for.
        if self._resolved:

            await interaction.response.send_message(
                "\u26d4 Someone already responded to this call.",
                ephemeral=True
            )

            return

        self._resolved = True
        self.stop()

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content=(
                f"\u2705 {member.mention} is responding to "
                f"{self.caller.mention}'s emergency at "
                f"**{self.caller_location_name}**."
            ),
            view=self
        )

        if self.message is not None:
            asyncio.create_task(
                _delete_after_delay(
                    self.message, EMERGENCY_MESSAGE_DELETE_DELAY_SECONDS
                )
            )

    async def on_timeout(self):

        if self._resolved:
            return

        # Nobody accepted in the response window — the alert is
        # gone, so there's nothing left for a late responder to
        # click.
        if self.message is not None:

            try:
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class EmergencyCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="emergency")
    async def emergency(self, ctx: commands.Context, service: str = None):
        """!emergency police | !emergency hospital"""

        if service is None:
            await ctx.send(
                "Usage: `!emergency police` or `!emergency hospital`"
            )
            return

        service_key = service.strip().lower()

        # "ambulance" is accepted as a friendlier alias — matches
        # the phone's "Call Ambulance" button label even though
        # the underlying service/location code is "hospital".
        if service_key == "ambulance":
            service_key = "hospital"

        service_info = EMERGENCY_SERVICES.get(service_key)

        if service_info is None:
            await ctx.send(
                "Usage: `!emergency police` or `!emergency hospital`"
            )
            return

        player = database.get_or_create_player(ctx.author.id)
        caller_location_name = _location_name(player["location"])

        channel = permissions.get_channel_for_code(
            ctx.guild, service_info["location"]
        )

        if channel is None:

            await ctx.send(
                f"\u26a0\ufe0f Couldn't find the "
                f"{service_info['label']} channel — tell an admin."
            )

            return

        role = discord.utils.get(ctx.guild.roles, name=service_info["role"])
        role_mention = role.mention if role else f"@{service_info['role']}"

        view = EmergencyAlertView(
            service_key,
            service_info["role"],
            ctx.author,
            caller_location_name
        )

        try:

            alert_msg = await channel.send(
                f"\U0001f6a8 {role_mention} — **{service_info['label']} "
                f"needed!**\n"
                f"{ctx.author.mention} is requesting help at "
                f"**{caller_location_name}**.\n"
                f"Respond within "
                f"{EMERGENCY_RESPONSE_WINDOW_SECONDS} seconds "
                f"or this call goes unanswered.",
                view=view
            )

        except (discord.Forbidden, discord.HTTPException):

            await ctx.send(
                f"\u26a0\ufe0f Couldn't post in the "
                f"{service_info['label']} channel — tell an admin."
            )

            return

        view.message = alert_msg

        await ctx.send(
            f"\U0001f6a8 {service_info['label']} has been alerted to your "
            f"location — **{caller_location_name}**. Stay put if you can."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EmergencyCog(bot))
