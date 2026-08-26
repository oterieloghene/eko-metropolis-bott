"""
The player's Phone — one menu for Bus, BRT Card, Taxi, and Flights.

IMPORTANT — HOW THIS WORKS:

    This cog does NOT duplicate the logic in bus.py, brt_card.py,
    or taxi.py, and does NOT modify those files. Every button here
    builds a normal command Context (same as if the player had
    typed the command themselves in that channel) and calls
    ctx.invoke(...) on the REAL command. That means every existing
    check, fare calculation, role grant, and message stays exactly
    as it already is — the phone is just a friendlier front door.

    Flight booking is the one exception: it calls
    flight.book_flight_for() directly, because that function was
    written from the start to be shared between !bookflight and
    this menu.

PRIVACY NOTE:

    True "only you can see this" menus require Discord slash
    commands with ephemeral responses, which this bot doesn't have
    set up yet (it's a prefix-command bot). Until that's added,
    the phone panel is a normal channel message — but only the
    player who opened it can press its buttons (everyone else's
    taps are silently ignored), and it deletes itself after 3
    minutes.
"""

import os

import discord
from discord.ext import commands

import database
from cogs import flight as flight_cog


PHONE_TIMEOUT_SECONDS = 180

PHONE_IMAGE_PATH = os.path.join(
    os.path.dirname(__file__),
    "assets",
    "phone_home.png"
)

PHONE_COLOR = discord.Color.from_rgb(61, 220, 132)  # Android green


def _phone_embed(subtitle: str = None) -> discord.Embed:
    """
    The embed styling shared by the home screen and every submenu —
    keeps the same phone graphic up top, just swaps the title.
    """

    embed = discord.Embed(
        title="\U0001f4f1 EkoPhone" + (f" \u2192 {subtitle}" if subtitle else ""),
        color=PHONE_COLOR
    )

    embed.set_image(url="attachment://phone_home.png")

    return embed


# ================================================================
# HELPER — BUILD A USABLE ctx FROM A BUTTON INTERACTION
# ================================================================

class _SilentMessage:
    """
    Stand-in for ctx.message.

    Several existing commands (bus.py, taxi.py) call
    ctx.message.delete() to clean up the player's typed command.
    Since a phone button click has no "typed command message" to
    delete, this swallows that call instead of deleting the phone
    panel itself.
    """

    def __init__(self, real_message: discord.Message):
        self.id = real_message.id
        self.channel = real_message.channel
        self.guild = real_message.guild
        self.author = real_message.author
        self.content = ""

    async def delete(self, *args, **kwargs):
        return None


async def _invoke(
    bot: commands.Bot,
    interaction: discord.Interaction,
    command_name: str,
    *args,
    **kwargs
) -> bool:
    """
    Run an existing prefix command exactly as if the player had
    typed it in interaction.channel, using their real permissions
    and real location.

    ctx.invoke() calls the command's underlying function directly
    (bypassing Discord's message parsing), so any command
    parameter declared keyword-only in its signature — e.g.
    taxi.py's `book(ctx, tier, *, destination)` — MUST be passed
    here as a keyword argument too, or Python raises a TypeError
    and the interaction dies with no visible error to the player
    (this was the "booking a taxi from the phone crashes" bug).
    Plain positional parameters (e.g. bus.py's `bus(ctx, route,
    destination)`) can still go through *args.

    Returns False (and tells the player) if the command can't be
    found — that means bot.py's cog list is out of sync with this
    file, not a player-facing error.
    """

    command = bot.get_command(command_name)

    if command is None:

        await interaction.followup.send(
            f"⚠️ Internal error: `{command_name}` isn't loaded. "
            f"Tell an admin.",
            ephemeral=True
        )

        return False

    ctx = await bot.get_context(interaction.message)
    ctx.author = interaction.user
    ctx.message = _SilentMessage(interaction.message)

    # Lets individual commands (e.g. brt_card.py's buy_card) tell
    # a phone-initiated call apart from the player typing the
    # command themselves, for the handful of checks — like "must
    # be at the Taxi Company" — that should only apply to the
    # latter.
    ctx.from_phone = True

    await ctx.invoke(command, *args, **kwargs)

    return True


# ================================================================
# MODALS — collect typed input the same way the ! commands do
# ================================================================

class BusTicketModal(discord.ui.Modal, title="Board a Bus"):

    route = discord.ui.TextInput(
        label="Route (B1, B2, or B3)",
        placeholder="B1",
        max_length=4
    )

    destination = discord.ui.TextInput(
        label="Destination",
        placeholder="e.g. lekki-phase-1"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(
            ephemeral=True,
            thinking=False
        )

        await _invoke(
            self.bot,
            interaction,
            "bus",
            self.route.value,
            self.destination.value
        )


class TaxiBookModal(discord.ui.Modal, title="Book a Taxi"):

    tier = discord.ui.TextInput(
        label="Tier (standard or premium)",
        placeholder="standard",
        max_length=10
    )

    destination = discord.ui.TextInput(
        label="Destination",
        placeholder="e.g. eko-market"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(
            ephemeral=True,
            thinking=False
        )

        await _invoke(
            self.bot,
            interaction,
            "book",
            self.tier.value,
            destination=self.destination.value
        )


class RechargeModal(discord.ui.Modal, title="Recharge BRT Card"):

    amount = discord.ui.TextInput(
        label="Amount (\u20a6)",
        placeholder="5000"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(
            ephemeral=True,
            thinking=False
        )

        try:
            amount = int(str(self.amount.value).strip())

        except ValueError:

            await interaction.followup.send(
                "\u26d4 Amount must be a whole number.",
                ephemeral=True
            )

            return

        await _invoke(
            self.bot,
            interaction,
            "brtcard recharge",
            amount
        )


class FlightStayModal(discord.ui.Modal, title="How long is the trip?"):

    stay_minutes = discord.ui.TextInput(
        label="Vacation length in minutes (2-30, test run)",
        placeholder="10"
    )

    def __init__(self, bot: commands.Bot, destination: str):
        super().__init__()
        self.bot = bot
        self.destination = destination

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(
            ephemeral=True,
            thinking=False
        )

        try:
            minutes = int(str(self.stay_minutes.value).strip())

        except ValueError:

            await interaction.followup.send(
                "\u26d4 Stay length must be a whole number of minutes.",
                ephemeral=True
            )

            return

        ok, message = flight_cog.book_flight_for(
            interaction.user.id,
            self.destination,
            minutes
        )

        await interaction.followup.send(message, ephemeral=True)


# ================================================================
# SUBMENU VIEWS
# ================================================================

class _BaseView(discord.ui.View):

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(timeout=PHONE_TIMEOUT_SECONDS)
        self.bot = bot
        self.owner_id = owner_id

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ) -> bool:

        if interaction.user.id != self.owner_id:

            await interaction.response.send_message(
                "\U0001f4f5 This isn't your phone.",
                ephemeral=True
            )

            return False

        return True

    async def on_timeout(self):

        for child in self.children:
            child.disabled = True


class BusView(_BaseView):

    @discord.ui.button(label="Board a Bus", style=discord.ButtonStyle.primary, emoji="\U0001f3ab")
    async def board(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BusTicketModal(self.bot))

    @discord.ui.button(label="Fleet Status", style=discord.ButtonStyle.secondary, emoji="\U0001f68f")
    async def fleet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "busfleet")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


class BRTCardView(_BaseView):

    @discord.ui.button(label="Buy Card", style=discord.ButtonStyle.primary, emoji="\U0001f4b3")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "brtcard buy")

    @discord.ui.button(label="Balance", style=discord.ButtonStyle.secondary, emoji="\U0001f4b0")
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "brtcard balance")

    @discord.ui.button(label="Recharge", style=discord.ButtonStyle.success, emoji="\U0001f50b")
    async def recharge(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RechargeModal(self.bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


class TaxiView(_BaseView):

    @discord.ui.button(label="Book a Ride", style=discord.ButtonStyle.primary, emoji="\U0001f695")
    async def book(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TaxiBookModal(self.bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


class FlightView(_BaseView):

    @discord.ui.button(label="Book Dubai", style=discord.ButtonStyle.primary)
    async def dubai(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FlightStayModal(self.bot, "dubai"))

    @discord.ui.button(label="Book Maldives", style=discord.ButtonStyle.primary)
    async def maldives(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FlightStayModal(self.bot, "maldives"))

    @discord.ui.button(label="Check In", style=discord.ButtonStyle.success, emoji="\U0001f6eb")
    async def checkin(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.defer(ephemeral=True, thinking=False)

        # Mirror what !checkin's @checks.require_location already
        # enforces, since ctx.invoke() below would skip that
        # decorator. Only the database-location half is checked
        # here (the phone isn't tied to one physical channel);
        # the command itself still re-validates everything else.
        player = database.get_or_create_player(interaction.user.id)

        if player["location"] != flight_cog.FLIGHT_AGENCY_LOCATION:

            await interaction.followup.send(
                "\u26d4 You need to be at the Travel Agency to check in.",
                ephemeral=True
            )

            return

        await _invoke(self.bot, interaction, "checkin")

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, emoji="\U0001f4cb")
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "flightstatus")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


class MechanicView(_BaseView):

    @discord.ui.button(label="Request Mechanic", style=discord.ButtonStyle.primary, emoji="\U0001f527")
    async def request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "bookmechanic")

    @discord.ui.button(label="Cancel Request", style=discord.ButtonStyle.danger, emoji="\u274c")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "cancelmechanic")

    @discord.ui.button(label="Go Online", style=discord.ButtonStyle.success, emoji="\U0001f7e2")
    async def online(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only does anything for players with the Mechanic role —
        # !mechanicstart itself tells anyone else why it declined.
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "mechanicstart")

    @discord.ui.button(label="Go Offline", style=discord.ButtonStyle.secondary, emoji="\U0001f534")
    async def offline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "mechanicstop")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


# ================================================================
# CONTACTS
# ================================================================

def _resolve_member(guild: discord.Guild, raw: str) -> discord.Member | None:
    """
    Parses a typed mention (<@id> / <@!id>) or a raw numeric ID
    into a real discord.Member — modals only return text, but
    ctx.invoke() needs the real object, not a string.
    """

    raw = raw.strip()

    for prefix, suffix in (("<@!", ">"), ("<@", ">")):
        if raw.startswith(prefix) and raw.endswith(suffix):
            raw = raw[len(prefix):-len(suffix)]
            break

    if not raw.isdigit():
        return None

    return guild.get_member(int(raw))


class AddContactModal(discord.ui.Modal, title="Add Contact"):

    user_tag = discord.ui.TextInput(
        label="@mention or user ID",
        placeholder="@username"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        member = _resolve_member(interaction.guild, self.user_tag.value)

        if member is None:

            await interaction.followup.send(
                "\u26d4 Couldn't find that player. Try @mentioning "
                "them or pasting their user ID.",
                ephemeral=True
            )

            return

        await _invoke(self.bot, interaction, "addcontact", member)


class MessageContactModal(discord.ui.Modal, title="Send a Text"):

    message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        max_length=500
    )

    def __init__(self, bot: commands.Bot, member: discord.Member):
        super().__init__()
        self.bot = bot
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        await _invoke(
            self.bot,
            interaction,
            "text",
            self.member,
            message=self.message.value
        )


class ContactSelect(discord.ui.Select):

    def __init__(self, bot: commands.Bot, owner_id: int, rows):

        options = [
            discord.SelectOption(
                label=(row["label"] or f"User {row['contact_id']}")[:100],
                value=row["contact_id"]
            )
            for row in rows[:25]
        ]

        super().__init__(
            placeholder="Message a contact...",
            options=options,
            min_values=1,
            max_values=1
        )

        self.bot = bot
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction):

        member = interaction.guild.get_member(int(self.values[0]))

        if member is None:

            await interaction.response.send_message(
                "That player isn't in this server anymore.",
                ephemeral=True
            )

            return

        await interaction.response.send_modal(
            MessageContactModal(self.bot, member)
        )


class ContactsView(_BaseView):

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(bot, owner_id)

        rows = database.get_contacts(owner_id)

        if rows:
            self.add_item(ContactSelect(bot, owner_id, rows))

    @discord.ui.button(label="Add Contact", style=discord.ButtonStyle.primary, emoji="\u2795", row=1)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddContactModal(self.bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


# ================================================================
# MAIN MENU
# ================================================================

class MainMenuView(_BaseView):

    @discord.ui.button(label="Bus", style=discord.ButtonStyle.primary, emoji="\U0001f68c")
    async def bus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bus"), view=BusView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="BRT Card", style=discord.ButtonStyle.primary, emoji="\U0001f4b3")
    async def brt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("BRT Card"), view=BRTCardView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Taxi", style=discord.ButtonStyle.primary, emoji="\U0001f695")
    async def taxi(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Taxi"), view=TaxiView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Flights", style=discord.ButtonStyle.primary, emoji="\u2708\ufe0f")
    async def flights(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Flights"), view=FlightView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Mechanic", style=discord.ButtonStyle.primary, emoji="\U0001f527")
    async def mechanic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Mechanic"), view=MechanicView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Contacts", style=discord.ButtonStyle.primary, emoji="\U0001f4d6")
    async def contacts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Contacts"), view=ContactsView(self.bot, self.owner_id)
        )


# ================================================================
# COG
# ================================================================

class PhoneCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="phone")
    async def phone(self, ctx: commands.Context):

        database.get_or_create_player(ctx.author.id)

        # Sending an embed image + file attachment needs "Attach
        # Files" and "Embed Links" permissions in THIS channel.
        # Not every channel has those configured (only the ones
        # in LOCATIONS get them automatically — see
        # permissions.ensure_bot_channel_permissions), so without
        # this try/except !phone would raise discord.Forbidden
        # and die with "Something went wrong running that
        # command" in any channel missing those permissions. Fall
        # back to a text-only menu (still fully usable, just no
        # picture) instead of crashing.
        try:

            await ctx.send(
                embed=_phone_embed(),
                file=discord.File(
                    PHONE_IMAGE_PATH, filename="phone_home.png"
                ),
                view=MainMenuView(self.bot, ctx.author.id)
            )

        except discord.Forbidden:

            try:

                await ctx.send(
                    "\U0001f4f1 **EkoPhone** "
                    "(image unavailable — I'm missing "
                    "**Attach Files**/**Embed Links** permission "
                    "in this channel)",
                    view=MainMenuView(self.bot, ctx.author.id)
                )

            except discord.HTTPException:
                pass

        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(PhoneCog(bot))
