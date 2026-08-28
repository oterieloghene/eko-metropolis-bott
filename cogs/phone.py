"""
The player's Phone — one menu for Bus, BRT Card, Taxi, Flights,
Hotel, Contacts, Mechanic, Dispatch, Map, Emergency, Taxi
Company, and the Bank App.

IMPORTANT — HOW THIS WORKS:

    This cog does NOT duplicate the logic in bus.py, brt_card.py,
    taxi.py, contacts.py, or wallet.py, and does NOT modify those
    files. Every button here builds a normal command Context (same
    as if the player had typed the command themselves in that
    channel) and calls ctx.invoke(...) on the REAL command. That
    means every existing check, fare calculation, role grant, and
    message stays exactly as it already is — the phone is just a
    friendlier front door.

    Flight booking and Hotel booking are the two exceptions: they
    call flight.book_flight_for() / hotel.book_hotel_for() directly,
    because those functions were written from the start to be
    shared between the ! commands and this menu.

PRIVACY — HOW THE PANEL STAYS PRIVATE WITHOUT A SLASH COMMAND:

    Ephemeral responses aren't limited to slash commands — any
    interaction (including a plain button click) can be answered
    ephemerally. But ephemeral only applies to the FIRST response
    to an interaction (interaction.response.send_message(...)),
    never to interaction.response.edit_message(...) on an
    already-public message. So the actual private menu has to be
    a brand new message, which means !phone needs one extra hop:

    1. !phone posts a tiny PUBLIC message — just a
       "Tap to open your phone" button — visible to the channel.
       Only the person who typed !phone can press it (PhoneOpenView
       checks that); it disappears after ~60s if never tapped.
    2. Tapping it IS a fresh interaction, so the bot answers it
       with interaction.response.send_message(..., ephemeral=True)
       — a genuinely private message, with Discord's native
       dismiss control, visible only to that player. The public
       "tap to open" prompt is deleted immediately once that
       happens.
    3. Every screen after that (Bus/Taxi/Back/etc.) just edits
       that already-ephemeral message with edit_message(...) —
       which stays ephemeral, so all the existing submenu code
       below is untouched.

    CAVEAT — commands invoked via ctx.invoke() (see _invoke below)
    still run their own ctx.send() calls against the REAL channel
    the player typed !phone in, because that's the channel the
    Context is built from. That's unchanged from before and is a
    deeper architectural issue (every existing command's ctx.send
    would need to become phone-aware) that's out of scope for this
    file. What IS fixed here: every screen this file owns directly
    — Bank App, BRT recharge-someone-else, transfers, hotel guest
    picking, adding a contact — replies through interaction
    followups, which are ephemeral by construction, so none of the
    NEW phone flows leak into public channels.
"""

import os

import discord
from discord.ext import commands

import database
from cogs import contacts as contacts_cog
from cogs import flight as flight_cog
from cogs import hotel as hotel_cog
from config import DISPATCH_RIDER_ROLE, TAXI_DRIVER_ROLE


PHONE_TIMEOUT_SECONDS = 180

PHONE_IMAGE_PATH = os.path.join(
    os.path.dirname(__file__),
    "assets",
    "phone_home.png"
)

PHONE_COLOR = discord.Color.from_rgb(61, 220, 132)  # Android green


def _phone_embed(subtitle: str = None, description: str = None) -> discord.Embed:
    """
    The embed styling shared by the home screen and every submenu —
    keeps the same phone graphic up top, just swaps the title (and,
    optionally, adds a body line for screens that need one).
    """

    embed = discord.Embed(
        title="\U0001f4f1 EkoPhone" + (f" \u2192 {subtitle}" if subtitle else ""),
        description=description,
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

    # Lets individual commands (e.g. brt_card.py's get_card) tell
    # a phone-initiated call apart from the player typing the
    # command themselves, for the handful of checks — like "must
    # be at the Taxi Company" — that should only apply to the
    # latter.
    ctx.from_phone = True

    # --------------------------------------------------------
    # KEEP PHONE REPLIES PRIVATE
    #
    # Every command below still just calls ctx.send(...) (often
    # via its own cog's _send_and_delete helper) exactly like it
    # would for a typed command — that's the whole point of
    # reusing these commands unmodified. But left alone, ctx.send
    # here would post to the real public channel behind this
    # interaction and only clean itself up a few seconds later,
    # which briefly leaks things like "contact added", a wallet
    # balance, or a BRT top-up to everyone else in the channel.
    #
    # Since the interaction was already deferred ephemerally
    # before _invoke was ever called, redirect ctx.send to an
    # ephemeral interaction.followup.send instead — visible only
    # to this player, from the first word, with no public flash.
    # Auto-delete helpers that stash the returned message and
    # call .delete()/.edit() on it later keep working unchanged,
    # since a followup message supports both.
    # --------------------------------------------------------

    async def _ephemeral_send(*send_args, **send_kwargs):
        send_kwargs["ephemeral"] = True
        return await interaction.followup.send(*send_args, **send_kwargs)

    ctx.send = _ephemeral_send

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

        await interaction.response.defer(ephemeral=True, thinking=False)

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

        await interaction.response.defer(ephemeral=True, thinking=False)

        await _invoke(
            self.bot,
            interaction,
            "book",
            self.tier.value,
            destination=self.destination.value
        )


class DispatchOrderModal(discord.ui.Modal, title="Order a Delivery"):

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

        await interaction.response.defer(ephemeral=True, thinking=False)

        await _invoke(
            self.bot,
            interaction,
            "orderdelivery",
            self.tier.value,
            destination=self.destination.value
        )


def _parse_amount(raw: str) -> int | None:
    """Shared int-parsing for every '₦ amount' modal below."""

    try:
        return int(str(raw).strip())
    except ValueError:
        return None


class RechargeModal(discord.ui.Modal, title="Recharge My BRT Card"):

    amount = discord.ui.TextInput(
        label="Amount (\u20a6)",
        placeholder="5000"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        amount = _parse_amount(self.amount.value)

        if amount is None:
            await interaction.followup.send(
                "\u26d4 Amount must be a whole number.", ephemeral=True
            )
            return

        await _invoke(self.bot, interaction, "brtcard recharge", amount)


class RechargeOtherModal(discord.ui.Modal, title="Recharge Their BRT Card"):

    amount = discord.ui.TextInput(
        label="Amount (\u20a6)",
        placeholder="5000"
    )

    def __init__(self, bot: commands.Bot, target: discord.Member):
        super().__init__()
        self.bot = bot
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        amount = _parse_amount(self.amount.value)

        if amount is None:
            await interaction.followup.send(
                "\u26d4 Amount must be a whole number.", ephemeral=True
            )
            return

        await _invoke(
            self.bot, interaction, "brtcard recharge", amount, self.target
        )


class AirtimeModal(discord.ui.Modal, title="Airtime Top-Up"):

    amount = discord.ui.TextInput(
        label="Amount (\u20a6)",
        placeholder="1000"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        amount = _parse_amount(self.amount.value)

        if amount is None:
            await interaction.followup.send(
                "\u26d4 Amount must be a whole number.", ephemeral=True
            )
            return

        await _invoke(self.bot, interaction, "airtime", amount)


class TransferModal(discord.ui.Modal, title="Bank Transfer"):

    amount = discord.ui.TextInput(
        label="Amount (\u20a6)",
        placeholder="10000"
    )

    def __init__(self, bot: commands.Bot, target: discord.Member):
        super().__init__()
        self.bot = bot
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        amount = _parse_amount(self.amount.value)

        if amount is None:
            await interaction.followup.send(
                "\u26d4 Amount must be a whole number.", ephemeral=True
            )
            return

        ok, reason = database.bank_transfer(
            interaction.user.id, self.target.id, amount
        )

        if ok:
            player = database.get_or_create_player(interaction.user.id)
            await interaction.followup.send(
                f"\u2705 Sent \u20a6{amount:,} to {self.target.display_name}.\n"
                f"\U0001f3e6 New Bank Balance: \u20a6{player['balance']:,}",
                ephemeral=True
            )

            await contacts_cog.send_transaction_alert(
                self.bot,
                interaction.guild,
                interaction.user.id,
                self.target.id,
                f"\U0001f3e6 You received \u20a6{amount:,} from "
                f"{interaction.user.display_name}.",
            )

            return

        reasons = {
            "no_sender_account": "\u26d4 You don't have a bank account.",
            "no_recipient_account": (
                f"\u26d4 {self.target.display_name} doesn't have a "
                "bank account."
            ),
            "invalid_amount": "\u26d4 Enter a positive amount.",
            "insufficient_funds": "\u26d4 You don't have enough money.",
        }

        await interaction.followup.send(
            reasons.get(reason, f"\u26d4 Transfer failed ({reason})."),
            ephemeral=True
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

        await interaction.response.defer(ephemeral=True, thinking=False)

        minutes = _parse_amount(self.stay_minutes.value)

        if minutes is None:
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


class ExchangeModal(discord.ui.Modal, title="Exchange Currency"):

    currency = discord.ui.TextInput(
        label="Currency: aed or mvr",
        placeholder="aed"
    )

    amount = discord.ui.TextInput(
        label="Amount to exchange (\u20a6)",
        placeholder="500000"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        amount = _parse_amount(self.amount.value)

        if amount is None:
            await interaction.followup.send(
                "\u26d4 Amount must be a whole number.", ephemeral=True
            )
            return

        await _invoke(
            self.bot,
            interaction,
            "exchange",
            str(self.currency.value).strip(),
            amount,
        )


# ================================================================
# SUBMENU VIEWS
# ================================================================

class _BaseView(discord.ui.View):
    """
    Base for every phone submenu (BusView, TaxiView, etc).

    No owner check here anymore: these views only ever exist
    inside an ephemeral message (see PhoneOpenView below), which
    Discord already shows to nobody but the player it was sent
    to. Nobody else can ever see these buttons to tap them, so a
    "this isn't your phone" guard has nothing left to guard.
    """

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(timeout=PHONE_TIMEOUT_SECONDS)
        self.bot = bot
        self.owner_id = owner_id

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

    @discord.ui.button(label="Get BRT Card", style=discord.ButtonStyle.primary, emoji="\U0001f4b3")
    async def get(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "brtcard get")

    @discord.ui.button(label="Balance", style=discord.ButtonStyle.secondary, emoji="\U0001f4b0")
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "brtcard balance")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


class TaxiView(_BaseView):

    @discord.ui.button(label="Book a Ride", style=discord.ButtonStyle.primary, emoji="\U0001f695")
    async def book(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TaxiBookModal(self.bot))

    @discord.ui.button(label="Cancel Ride", style=discord.ButtonStyle.danger, emoji="\u274c")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "cancelride")

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


# ================================================================
# HOTEL — tier + optional guest, no more typed guest ID.
# ================================================================

class HotelGuestSelect(discord.ui.UserSelect):
    """Real Discord user picker for the luxury-room guest, instead
    of typing (or copying) a raw user ID into a text box."""

    def __init__(self, bot: commands.Bot):
        super().__init__(
            placeholder="Choose a guest (optional)...",
            min_values=1,
            max_values=1
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        guest = self.values[0] if self.values else None

        if guest is not None and not isinstance(guest, discord.Member):
            guest = interaction.guild.get_member(guest.id)

        message = await hotel_cog.book_hotel_for(
            self.bot,
            interaction.guild,
            interaction.channel,
            interaction.user.id,
            "luxury",
            guest,
        )

        await interaction.followup.send(message, ephemeral=True)


class HotelGuestSelectView(_BaseView):

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(bot, owner_id)
        self.add_item(HotelGuestSelect(bot))

    @discord.ui.button(label="No Guest", style=discord.ButtonStyle.secondary, row=1)
    async def no_guest(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.defer(ephemeral=True, thinking=False)

        message = await hotel_cog.book_hotel_for(
            self.bot,
            interaction.guild,
            interaction.channel,
            interaction.user.id,
            "luxury",
            None,
        )

        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Hotel"), view=HotelView(self.bot, self.owner_id)
        )


class HotelView(_BaseView):

    @discord.ui.button(label="Book Standard", style=discord.ButtonStyle.primary, emoji="\U0001f3e8")
    async def standard(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.defer(ephemeral=True, thinking=False)

        message = await hotel_cog.book_hotel_for(
            self.bot,
            interaction.guild,
            interaction.channel,
            interaction.user.id,
            "standard",
            None,
        )

        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Book Luxury", style=discord.ButtonStyle.primary, emoji="\U0001f3ec")
    async def luxury(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Hotel \u2192 Luxury Guest"),
            view=HotelGuestSelectView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, emoji="\U0001f4cb")
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        message = None

        room = database.get_hotel_room(interaction.user.id)
        as_guest = False
        if room is None:
            room = database.get_hotel_room_as_guest(interaction.user.id)
            as_guest = True

        if room is None:
            message = "You don't have an active hotel room."
        else:
            icon = "\U0001f3e8" if room["tier"] == "standard" else "\U0001f3ec"
            label = f"{icon} {room['tier'].title()} Room {room['room_number']}"
            role = "guest in" if as_guest else "booked"
            message = f"You are {role} **{label}**."

        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


# ================================================================
# BANK APP — replaces the old Wallet menu.
#
# Gate: a player needs a bank account (database.has_bank_account)
# before any of this is usable. For now, accounts are opened by
# an admin (!registerplayers creates one automatically for every
# registered player) — see cogs/admin.py.
# ================================================================

class NoBankAccountView(_BaseView):

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


class WalletSubView(_BaseView):
    """Currency exchange, nested one level under Banking now."""

    @discord.ui.button(label="Balances & Rates", style=discord.ButtonStyle.primary, emoji="\U0001f4b1")
    async def balances(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "wallet")

    @discord.ui.button(label="Exchange", style=discord.ButtonStyle.success, emoji="\U0001f4b8")
    async def exchange(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ExchangeModal(self.bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App \u2192 Banking"),
            view=BankingView(self.bot, self.owner_id)
        )


class TransferUserSelect(discord.ui.UserSelect):

    def __init__(self, bot: commands.Bot):
        super().__init__(
            placeholder="Choose who to send money to...",
            min_values=1,
            max_values=1
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):

        target = self.values[0]

        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)

        if target is None:
            await interaction.response.send_message(
                "That player isn't in this server anymore.", ephemeral=True
            )
            return

        if target.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't transfer to yourself.", ephemeral=True
            )
            return

        await interaction.response.send_modal(TransferModal(self.bot, target))


class TransferSelectView(_BaseView):

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(bot, owner_id)
        self.add_item(TransferUserSelect(bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App \u2192 Banking"),
            view=BankingView(self.bot, self.owner_id)
        )


class BankingView(_BaseView):

    @discord.ui.button(label="Check Balance", style=discord.ButtonStyle.primary, emoji="\U0001f3e6")
    async def check_balance(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.defer(ephemeral=True, thinking=False)

        player = database.get_or_create_player(interaction.user.id)

        await interaction.followup.send(
            f"\U0001f3e6 **Bank Balance:** \u20a6{player['balance']:,}",
            ephemeral=True
        )

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.success, emoji="\U0001f4b8")
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App \u2192 Transfer"),
            view=TransferSelectView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Wallet", style=discord.ButtonStyle.secondary, emoji="\U0001f4b1")
    async def wallet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App \u2192 Wallet"),
            view=WalletSubView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App"), view=BankHomeView(self.bot, self.owner_id)
        )


class BRTRechargeUserSelect(discord.ui.UserSelect):

    def __init__(self, bot: commands.Bot):
        super().__init__(
            placeholder="Choose whose card to recharge...",
            min_values=1,
            max_values=1
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):

        target = self.values[0]

        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)

        if target is None:
            await interaction.response.send_message(
                "That player isn't in this server anymore.", ephemeral=True
            )
            return

        await interaction.response.send_modal(RechargeOtherModal(self.bot, target))


class BRTRechargeOtherView(_BaseView):

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(bot, owner_id)
        self.add_item(BRTRechargeUserSelect(bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App \u2192 Bill Payment"),
            view=BillPaymentView(self.bot, self.owner_id)
        )


class BillPaymentView(_BaseView):

    @discord.ui.button(label="Airtime Top-Up", style=discord.ButtonStyle.primary, emoji="\U0001f4f6")
    async def airtime(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AirtimeModal(self.bot))

    @discord.ui.button(label="Recharge My BRT Card", style=discord.ButtonStyle.success, emoji="\U0001f68c")
    async def recharge_self(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RechargeModal(self.bot))

    @discord.ui.button(label="Recharge Someone's BRT Card", style=discord.ButtonStyle.secondary, emoji="\U0001f68c", row=1)
    async def recharge_other(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App \u2192 BRT Recharge"),
            view=BRTRechargeOtherView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App"), view=BankHomeView(self.bot, self.owner_id)
        )


class BankHomeView(_BaseView):

    @discord.ui.button(label="Bill Payment", style=discord.ButtonStyle.primary, emoji="\U0001f9fe")
    async def bill_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App \u2192 Bill Payment"),
            view=BillPaymentView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Banking", style=discord.ButtonStyle.success, emoji="\U0001f3e6")
    async def banking(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App \u2192 Banking"),
            view=BankingView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


class ComingSoonView(_BaseView):

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey)
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


class DispatchView(_BaseView):

    @discord.ui.button(label="Order a Delivery", style=discord.ButtonStyle.primary, emoji="\U0001f4e6")
    async def order(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DispatchOrderModal(self.bot))

    @discord.ui.button(label="Cancel Order", style=discord.ButtonStyle.danger, emoji="\u274c")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "cancelorder")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


# ================================================================
# EMERGENCY — !emergency police / !emergency hospital (see
# cogs/emergency.py). Two buttons, no modal needed — neither
# command takes typed input beyond which service.
# ================================================================

class EmergencyView(_BaseView):

    @discord.ui.button(label="Call Police", style=discord.ButtonStyle.primary, emoji="\U0001f6a8")
    async def police(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "emergency", "police")

    @discord.ui.button(label="Call Ambulance", style=discord.ButtonStyle.danger, emoji="\U0001f691")
    async def ambulance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "emergency", "hospital")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


# ================================================================
# TAXI COMPANY — driver/rider-side controls, split out of
# TaxiView/DispatchView (which stay customer-only: Book/Order +
# Cancel). Gated at the door: MainMenuView's "Taxi Company"
# button checks TAXI_DRIVER_ROLE/DISPATCH_RIDER_ROLE BEFORE ever
# showing this view, so nobody who doesn't work for the company
# sees driver-only controls in the first place (see
# MainMenuView.taxi_company below).
# ================================================================

class TaxiCompanyView(_BaseView):

    # ---- Taxi (row 0) ----

    @discord.ui.button(label="Taxi: Go Online", style=discord.ButtonStyle.success, emoji="\U0001f7e2", row=0)
    async def taxi_online(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "taxistart")

    @discord.ui.button(label="Taxi: Go Offline", style=discord.ButtonStyle.secondary, emoji="\U0001f534", row=0)
    async def taxi_offline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "taxistop")

    @discord.ui.button(label="Taxi: Accept", style=discord.ButtonStyle.primary, emoji="\u2705", row=0)
    async def taxi_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "taxiaccept")

    @discord.ui.button(label="Taxi: Decline", style=discord.ButtonStyle.danger, emoji="\u274c", row=0)
    async def taxi_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "taxidecline")

    @discord.ui.button(label="Taxi: Begin Ride", style=discord.ButtonStyle.primary, emoji="\U0001f695", row=0)
    async def taxi_pickup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "taxipickup")

    # ---- Dispatch (row 1) ----

    @discord.ui.button(label="Dispatch: Go Online", style=discord.ButtonStyle.success, emoji="\U0001f7e2", row=1)
    async def dispatch_online(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "dispatchstart")

    @discord.ui.button(label="Dispatch: Go Offline", style=discord.ButtonStyle.secondary, emoji="\U0001f534", row=1)
    async def dispatch_offline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "dispatchstop")

    @discord.ui.button(label="Dispatch: Accept", style=discord.ButtonStyle.primary, emoji="\u2705", row=1)
    async def dispatch_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "dispatchaccept")

    @discord.ui.button(label="Dispatch: Decline", style=discord.ButtonStyle.danger, emoji="\u274c", row=1)
    async def dispatch_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "dispatchdecline")

    @discord.ui.button(label="Dispatch: Begin Ride", style=discord.ButtonStyle.primary, emoji="\U0001f4e6", row=1)
    async def dispatch_pickup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "dispatchpickup")

    # ---- Nav (row 2) ----

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id)
        )


# ================================================================
# CONTACTS
# ================================================================

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


class AddContactUserSelect(discord.ui.UserSelect):
    """
    Real Discord user picker for adding a contact — no more typing
    or pasting anyone's numeric ID.
    """

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(
            placeholder="Choose a player to add...",
            min_values=1,
            max_values=1
        )
        self.bot = bot
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        member = self.values[0]

        if not isinstance(member, discord.Member):
            member = interaction.guild.get_member(member.id)

        if member is None:
            await interaction.followup.send(
                "\u26d4 Couldn't find that player in this server.",
                ephemeral=True
            )
            return

        await _invoke(self.bot, interaction, "addcontact", member)

        # Refresh so the newly added contact shows up in the
        # message dropdown right away.
        await interaction.edit_original_response(
            embed=_phone_embed("Contacts"),
            view=ContactsView(self.bot, self.owner_id)
        )


class AddContactSelectView(_BaseView):

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(bot, owner_id)
        self.add_item(AddContactUserSelect(bot, owner_id))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.grey, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Contacts"), view=ContactsView(self.bot, self.owner_id)
        )


class ContactsView(_BaseView):

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(bot, owner_id)

        rows = database.get_contacts(owner_id)

        if rows:
            self.add_item(ContactSelect(bot, owner_id, rows))

    @discord.ui.button(label="Add Contact", style=discord.ButtonStyle.primary, emoji="\u2795", row=1)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Contacts \u2192 Add"),
            view=AddContactSelectView(self.bot, self.owner_id)
        )

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

    @discord.ui.button(label="Hotel", style=discord.ButtonStyle.primary, emoji="\U0001f3e8", row=1)
    async def hotel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Hotel"), view=HotelView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Dispatch", style=discord.ButtonStyle.secondary, emoji="\U0001f4e1", row=1)
    async def dispatch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Dispatch"), view=DispatchView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Bank App", style=discord.ButtonStyle.success, emoji="\U0001f3e6", row=1)
    async def bank_app(self, interaction: discord.Interaction, button: discord.ui.Button):

        if not database.has_bank_account(interaction.user.id):

            await interaction.response.edit_message(
                embed=_phone_embed(
                    "Bank App",
                    "\u26d4 You don't have a bank account yet. Bank "
                    "accounts are opened by an admin."
                ),
                view=NoBankAccountView(self.bot, self.owner_id)
            )

            return

        await interaction.response.edit_message(
            embed=_phone_embed("Bank App"), view=BankHomeView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Map", style=discord.ButtonStyle.primary, emoji="\U0001f5fa\ufe0f", row=2)
    async def map_(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "map")

    @discord.ui.button(label="Emergency", style=discord.ButtonStyle.danger, emoji="\U0001f6a8", row=2)
    async def emergency(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Emergency"), view=EmergencyView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Taxi Company", style=discord.ButtonStyle.secondary, emoji="\U0001f697", row=2)
    async def taxi_company(self, interaction: discord.Interaction, button: discord.ui.Button):

        # Gate at the door — only someone who actually works for
        # the company (either role) ever sees the driver-only
        # controls in TaxiCompanyView.
        member_roles = getattr(interaction.user, "roles", [])

        is_taxi_driver = discord.utils.get(member_roles, name=TAXI_DRIVER_ROLE)
        is_dispatch_rider = discord.utils.get(member_roles, name=DISPATCH_RIDER_ROLE)

        if not is_taxi_driver and not is_dispatch_rider:

            await interaction.response.edit_message(
                embed=_phone_embed(
                    "Taxi Company",
                    "\U0001f6ab You don't work for the company."
                ),
                view=ComingSoonView(self.bot, self.owner_id)
            )

            return

        await interaction.response.edit_message(
            embed=_phone_embed("Taxi Company"), view=TaxiCompanyView(self.bot, self.owner_id)
        )


# ================================================================
# THE ONE-TAP GATE — the only ever-public part of the phone.
# ================================================================

PHONE_OPEN_TIMEOUT_SECONDS = 60


class PhoneOpenView(discord.ui.View):
    """
    The public "Tap to open your phone" prompt !phone posts.

    Locked to whoever typed !phone (matches the old behavior:
    anyone else tapping it gets "this isn't your phone"). A tap
    from the right person opens their own real, ephemeral phone
    menu and deletes this prompt immediately. If nobody taps it,
    it deletes itself after PHONE_OPEN_TIMEOUT_SECONDS.
    """

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(timeout=PHONE_OPEN_TIMEOUT_SECONDS)
        self.bot = bot
        self.owner_id = owner_id
        self.message: discord.Message = None  # set by the caller after sending

    @discord.ui.button(
        label="Tap to open your phone",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f4f1"
    )
    async def open_phone(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if interaction.user.id != self.owner_id:

            await interaction.response.send_message(
                "\U0001f4f5 This isn't your phone.",
                ephemeral=True
            )

            return

        # This is the actual privacy hop: a fresh interaction
        # answered with its own ephemeral response, not an edit
        # of the public prompt.
        try:

            await interaction.response.send_message(
                embed=_phone_embed(),
                file=discord.File(
                    PHONE_IMAGE_PATH, filename="phone_home.png"
                ),
                view=MainMenuView(self.bot, self.owner_id),
                ephemeral=True
            )

        except discord.HTTPException:

            await interaction.response.send_message(
                "\U0001f4f1 **EkoPhone** (image unavailable)",
                view=MainMenuView(self.bot, self.owner_id),
                ephemeral=True
            )

        # Clean up the public prompt now that the real menu exists.
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

        self.stop()

    async def on_timeout(self):

        if self.message is not None:

            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


# ================================================================
# COG
# ================================================================

class PhoneCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="phone")
    async def phone(self, ctx: commands.Context):

        database.get_or_create_player(ctx.author.id)

        view = PhoneOpenView(self.bot, ctx.author.id)

        try:

            message = await ctx.send(
                "\U0001f4f1 **Tap to open your phone**",
                view=view
            )

            view.message = message

        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(PhoneCog(bot))
