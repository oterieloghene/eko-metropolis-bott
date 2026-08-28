"""
Contacts + Texting + Calls — save another player as a contact,
then text or call them like a real phone.

TEXTING — IN-CHANNEL, TWO-STAGE REVEAL:

    Texts are no longer delivered by DM. A text is delivered into
    the RECIPIENT'S CURRENT LOCATION CHANNEL (wherever their
    database location says they are right now — same lookup
    travel.py/taxi.py use via permissions.get_channel_for_code).

    Stage 1 (public): a plain "📩 Incoming text for @recipient"
    message appears in that channel with a single "View" button.
    Only the tagged recipient can press it — anyone else gets a
    "not your text" ephemeral bounce.

    Stage 2 (private): pressing View opens an ephemeral,
    recipient-only "encrypted text" screen showing who it's from
    and the actual content, with Reply + Close buttons. The
    original public message is deleted the moment it's opened —
    so the plaintext is never sitting in the channel for anyone
    else to read. The encrypted screen stays open only until the
    recipient hits Close (or lets it time out).

    Replying uses the exact same two-stage delivery back into the
    original sender's current channel — so a whole conversation
    can happen without either side ever leaving the two-stage
    flow, and without a single DM being sent.

CALLS:

    !call @player  — rings someone in your contacts.
        - A status line appears in YOUR channel:
              "📞 @recipient incoming call..."
        - The recipient gets a DM with Accept / Decline buttons.
        - Accept  -> your status line becomes
              "📞 Call connected with @recipient"
          (stays up until either side runs !endcall).
        - Decline (or no response in time) -> your status line
          becomes "☎️ Missed call" and then deletes itself.

    !endcall — either participant can end an active, connected
        call. Ends the status line and lets the other side know.

AIRTIME METERING:

    players.airtime_balance is its own ledger (like aed_balance/
    mvr_balance), separate from the bank balance. !airtime
    <amount> converts bank cash into airtime credit; !airtime
    with no amount reports the current balance. Every !text/
    !reply charges a flat AIRTIME_TEXT_COST the moment it's
    delivered. Calls are billed to the CALLER, per tick, only
    while CONNECTED — ringing/missed/declined calls stay free —
    and a call that runs out of airtime mid-conversation is ended
    automatically, same as !endcall, with both sides notified.

    Airtime top-up (!airtime <amount>) lives here too, since calls
    and texts are the only things airtime is spent on — the Bank
    App's bill-payment screen just calls this same command instead
    of duplicating recharge logic.

Self-contained: doesn't touch any other cog's internals. phone.py
drives all of this the same way it drives Bus/Taxi/BRT — by
building a ctx and calling ctx.invoke() on the real commands here.
"""

import asyncio

import discord
from discord.ext import commands

import database
import permissions
from config import (
    AIRTIME_CALL_COST_PER_TICK,
    AIRTIME_INSUFFICIENT_MESSAGE,
    AIRTIME_METER_INTERVAL_SECONDS,
    AIRTIME_TEXT_COST,
)


TEXT_TIMEOUT_SECONDS = 300

CALL_RESPONSE_TIMEOUT_SECONDS = 60

MESSAGE_DELETE_DELAY_SECONDS = 8

CALL_STATUS_DELETE_DELAY_SECONDS = 8

TEXT_COLOR = discord.Color.from_rgb(61, 220, 132)


# ================================================================
# SMALL HELPERS
# ================================================================

async def _delete_after_delay(message: discord.Message, delay: float) -> None:

    await asyncio.sleep(delay)

    try:
        await message.delete()

    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def _get_location_channel(
    guild: discord.Guild,
    user_id: int
) -> discord.TextChannel | None:
    """The Discord channel matching wherever this player's
    database location currently says they are."""

    player = database.get_or_create_player(user_id)

    return permissions.get_channel_for_code(guild, player["location"])


# ================================================================
# TEXTING — TWO-STAGE IN-CHANNEL REVEAL
# ================================================================

class TextRevealView(discord.ui.View):
    """
    The private, ephemeral "encrypted text" screen. Only the
    recipient ever sees this — Reply lets them write back (through
    the same two-stage flow), Close dismisses it early.
    """

    def __init__(self, bot: commands.Bot, sender_id: int, recipient_id: int):
        super().__init__(timeout=TEXT_TIMEOUT_SECONDS)
        self.bot = bot
        self.sender_id = sender_id
        self.recipient_id = recipient_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.recipient_id:
            await interaction.response.send_message(
                "This isn't your text.", ephemeral=True
            )
            return False

        return True

    @discord.ui.button(label="Reply", style=discord.ButtonStyle.primary, emoji="\u21a9\ufe0f")
    async def reply_button(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.send_modal(
            TextReplyModal(self.bot, self.sender_id, self.recipient_id)
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="\u2716\ufe0f")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):

        try:
            await interaction.response.defer()
            await interaction.delete_original_response()

        except discord.HTTPException:
            pass

        self.stop()


class TextReplyModal(discord.ui.Modal, title="Reply"):

    message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        max_length=500
    )

    def __init__(self, bot: commands.Bot, sender_id: int, replier_id: int):
        super().__init__()
        self.bot = bot
        self.sender_id = sender_id
        self.replier_id = replier_id

    async def on_submit(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True, thinking=False)

        ok, note = await send_text(
            self.bot,
            interaction.guild,
            interaction.user,
            self.sender_id,
            self.message.value
        )

        await interaction.followup.send(note, ephemeral=True)


class TextRevealPromptView(discord.ui.View):
    """
    Stage 1 — the public "📩 Incoming text for @recipient" prompt
    posted into the recipient's current location channel. Only the
    tagged recipient can open it; opening it deletes this message
    and replaces it with the private encrypted screen.
    """

    def __init__(self, bot: commands.Bot, sender_id: int, recipient_id: int, content: str):
        super().__init__(timeout=TEXT_TIMEOUT_SECONDS)
        self.bot = bot
        self.sender_id = sender_id
        self.recipient_id = recipient_id
        self.content = content
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.recipient_id:
            await interaction.response.send_message(
                "\U0001f4f5 This text isn't addressed to you.", ephemeral=True
            )
            return False

        return True

    @discord.ui.button(label="View", style=discord.ButtonStyle.primary, emoji="\U0001f4e9")
    async def view_button(self, interaction: discord.Interaction, button: discord.ui.Button):

        try:
            sender = self.bot.get_user(self.sender_id) or await self.bot.fetch_user(self.sender_id)
            sender_name = sender.display_name
            sender_icon = sender.display_avatar.url

        except discord.NotFound:
            sender_name = "Unknown"
            sender_icon = None

        embed = discord.Embed(description=self.content, color=TEXT_COLOR)
        embed.set_author(
            name=f"\U0001f512 Encrypted text from {sender_name}",
            icon_url=sender_icon
        )

        await interaction.response.send_message(
            embed=embed,
            view=TextRevealView(self.bot, self.sender_id, self.recipient_id),
            ephemeral=True
        )

        # The plaintext prompt disappears the moment it's opened —
        # only the ephemeral encrypted screen remains.
        if self.message is not None:
            try:
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        self.stop()

    async def on_timeout(self):

        if self.message is not None:
            try:
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


async def send_text(
    bot: commands.Bot,
    guild: discord.Guild | None,
    sender: discord.abc.User,
    recipient_id: int,
    content: str
) -> tuple[bool, str]:
    """
    Deliver a text into `recipient_id`'s CURRENT LOCATION CHANNEL
    as a two-stage reveal. Returns (ok, message_for_sender).
    """

    if recipient_id == sender.id:
        return False, "You can't text yourself."

    if guild is None:
        return False, "Texting only works from inside the server."

    recipient_member = guild.get_member(recipient_id)

    if recipient_member is None:
        return False, "That player isn't on this server."

    # ------------------------------------------------------------
    # AIRTIME — flat fee per text, charged from the sender's
    # airtime_balance (never the bank balance). Checked before
    # anything is posted so a blocked text never touches the
    # recipient's channel.
    # ------------------------------------------------------------

    sender_player = database.get_or_create_player(sender.id)

    if sender_player["airtime_balance"] < AIRTIME_TEXT_COST:
        return False, AIRTIME_INSUFFICIENT_MESSAGE

    channel = _get_location_channel(guild, recipient_id)

    if channel is None:
        return False, (
            f"Couldn't find {recipient_member.display_name}'s "
            "current location channel."
        )

    prompt_view = TextRevealPromptView(bot, sender.id, recipient_id, content)

    try:
        msg = await channel.send(
            f"\U0001f4e9 Incoming text for {recipient_member.mention}",
            view=prompt_view
        )

    except (discord.Forbidden, discord.HTTPException):
        return False, (
            f"Couldn't deliver — I can't post in "
            f"{recipient_member.display_name}'s channel."
        )

    prompt_view.message = msg

    database.set_last_text_sender(recipient_id, sender.id)

    # Only charged once delivery actually succeeded.
    database.update_player(
        sender.id,
        airtime_balance=sender_player["airtime_balance"] - AIRTIME_TEXT_COST
    )

    return True, (
        f"\U0001f4ac Text delivered to {recipient_member.display_name}. "
        f"(\u2212\u20a6{AIRTIME_TEXT_COST:,} airtime)"
    )


async def send_transaction_alert(
    bot: commands.Bot,
    guild: discord.Guild | None,
    sender_id: int,
    recipient_id: int,
    content: str
) -> None:
    """
    Post a bank-transfer notification into recipient_id's current
    location channel, using the exact same two-stage encrypted-text
    reveal UI as a normal !text (see send_text above) — a public
    "incoming alert" prompt that only the recipient can open, which
    then shows the real content in a private encrypted screen.

    Unlike send_text this is system-generated (triggered by
    TransferModal in phone.py after a successful bank_transfer()),
    so there's no airtime cost and no sender-side reply message —
    it's fire-and-forget. The transfer itself is already committed
    in the database by the time this runs, so a failure here (DM's
    closed, channel missing, etc.) is silently swallowed rather
    than surfaced as an error — the money has already moved either
    way, this is just the notification.
    """

    if guild is None:
        return

    recipient_member = guild.get_member(recipient_id)

    if recipient_member is None:
        return

    channel = _get_location_channel(guild, recipient_id)

    if channel is None:
        return

    prompt_view = TextRevealPromptView(bot, sender_id, recipient_id, content)

    try:
        msg = await channel.send(
            f"\U0001f4b3 Incoming transaction alert for "
            f"{recipient_member.mention}",
            view=prompt_view
        )

    except (discord.Forbidden, discord.HTTPException):
        return

    prompt_view.message = msg

    database.set_last_text_sender(recipient_id, sender_id)


# ================================================================
# CALLS
# ================================================================

def _call_key(user_a: int, user_b: int) -> frozenset:
    return frozenset((user_a, user_b))


class CallResponseView(discord.ui.View):
    """
    DM'd to the recipient of a !call. Accept/Decline updates the
    status line back in the caller's channel and (on timeout or
    decline) cleans itself up as a missed call.
    """

    def __init__(
        self,
        bot: commands.Bot,
        active_calls: dict,
        caller_id: int,
        recipient_id: int,
        guild_id: int,
        status_channel_id: int,
        status_message_id: int,
        cog: "ContactsCog | None" = None
    ):
        super().__init__(timeout=CALL_RESPONSE_TIMEOUT_SECONDS)
        self.bot = bot
        self.active_calls = active_calls
        self.caller_id = caller_id
        self.recipient_id = recipient_id
        self.guild_id = guild_id
        self.status_channel_id = status_channel_id
        self.status_message_id = status_message_id
        self.cog = cog
        self.dm_message: discord.Message | None = None
        self._resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.recipient_id

    async def _get_status_message(self) -> discord.Message | None:

        guild = self.bot.get_guild(self.guild_id)

        if guild is None:
            return None

        channel = guild.get_channel(self.status_channel_id)

        if channel is None:
            return None

        try:
            return await channel.fetch_message(self.status_message_id)

        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="\u2705")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):

        self._resolved = True

        key = _call_key(self.caller_id, self.recipient_id)

        guild = self.bot.get_guild(self.guild_id)
        status_channel = (
            guild.get_channel(self.status_channel_id)
            if guild is not None
            else None
        )

        # ----------------------------------------------------------
        # PRIVATE CALL LINE — a temporary private thread (for text/
        # coordination) plus a temporary private voice channel that
        # only the two participants (and the bot) can see or join.
        # Both are deleted the moment the call ends, however it ends
        # (see ContactsCog._end_call). Best-effort: if either fails
        # to create (missing perms, boost-level requirements for
        # private threads, etc.) the call still "connects" in the
        # status-line sense, it just won't have a real voice line.
        # ----------------------------------------------------------

        thread = None
        voice_channel = None

        if guild is not None and status_channel is not None:

            try:
                thread = await status_channel.create_thread(
                    name=f"call-{self.caller_id}-{self.recipient_id}",
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                    auto_archive_duration=60,
                    reason="Private call line",
                )

                await thread.add_user(discord.Object(id=self.caller_id))
                await thread.add_user(discord.Object(id=self.recipient_id))

            except discord.HTTPException:
                thread = None

            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=False, connect=False
                    ),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True, connect=True, manage_channels=True
                    ),
                }

                caller_member = guild.get_member(self.caller_id)
                recipient_member = guild.get_member(self.recipient_id)

                if caller_member is not None:
                    overwrites[caller_member] = discord.PermissionOverwrite(
                        view_channel=True, connect=True, speak=True
                    )

                if recipient_member is not None:
                    overwrites[recipient_member] = discord.PermissionOverwrite(
                        view_channel=True, connect=True, speak=True
                    )

                voice_channel = await guild.create_voice_channel(
                    name=f"call-{self.caller_id}-{self.recipient_id}",
                    category=status_channel.category,
                    overwrites=overwrites,
                    reason="Private call line",
                )

            except discord.HTTPException:
                voice_channel = None

            if thread is not None and voice_channel is not None:

                try:
                    await thread.send(
                        f"\U0001f4de <@{self.caller_id}> <@{self.recipient_id}> "
                        f"— join your private call here: "
                        f"{voice_channel.mention}\n"
                        f"This thread and channel are deleted "
                        f"automatically once the call ends."
                    )
                except discord.HTTPException:
                    pass

        self.active_calls[key] = {
            "caller_id": self.caller_id,
            "recipient_id": self.recipient_id,
            "guild_id": self.guild_id,
            "channel_id": self.status_channel_id,
            "message_id": self.status_message_id,
            "thread_id": thread.id if thread is not None else None,
            "voice_channel_id": (
                voice_channel.id if voice_channel is not None else None
            ),
        }

        status_msg = await self._get_status_message()

        if status_msg is not None:

            link_note = (
                f" — {thread.mention}" if thread is not None else ""
            )

            try:
                await status_msg.edit(
                    content=f"\U0001f4de Call connected with "
                            f"<@{self.recipient_id}>{link_note}"
                )
            except discord.HTTPException:
                pass

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="\u2705 Call accepted. Say hi!", view=self
        )

        # Metering only starts once the call is actually
        # connected — ringing/missed/declined calls stay free.
        if self.cog is not None:
            self.cog.start_metering(key)

        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="\u260e\ufe0f")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):

        self._resolved = True

        status_msg = await self._get_status_message()

        if status_msg is not None:
            try:
                await status_msg.edit(content="\u260e\ufe0f Missed call")
                asyncio.create_task(
                    _delete_after_delay(status_msg, CALL_STATUS_DELETE_DELAY_SECONDS)
                )
            except discord.HTTPException:
                pass

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="\u260e\ufe0f You declined the call.", view=self
        )

        self.stop()

    async def on_timeout(self):

        if self._resolved:
            return

        status_msg = await self._get_status_message()

        if status_msg is not None:
            try:
                await status_msg.edit(content="\u260e\ufe0f Missed call")
                asyncio.create_task(
                    _delete_after_delay(status_msg, CALL_STATUS_DELETE_DELAY_SECONDS)
                )
            except discord.HTTPException:
                pass

        if self.dm_message is not None:
            try:
                await self.dm_message.edit(content="\u260e\ufe0f Missed call.", view=None)
            except discord.HTTPException:
                pass


# ================================================================
# COG
# ================================================================

class ContactsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Keyed by frozenset({caller_id, recipient_id}) so either
        # side can look their own active call up with just their
        # own id.
        self.active_calls: dict[frozenset, dict] = {}

    # ------------------------------------------------------------
    # send + auto-delete — keeps phone chatter (contact added,
    # confirmations, etc.) from cluttering up public channels.
    # ------------------------------------------------------------

    async def _reply(
        self,
        ctx: commands.Context,
        content: str,
        delay: float = MESSAGE_DELETE_DELAY_SECONDS
    ) -> discord.Message:

        message = await ctx.send(content)
        asyncio.create_task(_delete_after_delay(message, delay))
        return message

    def _find_active_call(self, user_id: int):

        for key, call in self.active_calls.items():

            if user_id in key:
                return key, call

        return None, None

    # ------------------------------------------------------------
    # AIRTIME METERING — one background task per connected call,
    # billing the CALLER's airtime_balance every
    # AIRTIME_METER_INTERVAL_SECONDS for as long as the call is
    # up. Stops on its own once the call leaves self.active_calls
    # (however that happens — !endcall, the other side hanging
    # up, or running out of airtime here).
    # ------------------------------------------------------------

    def start_metering(self, key: frozenset) -> None:
        asyncio.create_task(self._meter_call(key))

    async def _meter_call(self, key: frozenset) -> None:

        while True:

            await asyncio.sleep(AIRTIME_METER_INTERVAL_SECONDS)

            call = self.active_calls.get(key)

            if call is None:
                # Call already ended some other way.
                return

            caller_id = call["caller_id"]
            player = database.get_or_create_player(caller_id)

            if player["airtime_balance"] < AIRTIME_CALL_COST_PER_TICK:

                await self._end_call(
                    key, call,
                    ended_by_id=None,
                    reason="ran out of airtime"
                )

                return

            database.update_player(
                caller_id,
                airtime_balance=player["airtime_balance"] - AIRTIME_CALL_COST_PER_TICK
            )

    # ------------------------------------------------------------
    # SHARED CALL-ENDING LOGIC — used by !endcall (a participant
    # hangs up) and by the metering task above (forced end, no
    # participant involved). ended_by_id is None for the latter.
    # ------------------------------------------------------------

    async def _end_call(
        self,
        key: frozenset,
        call: dict,
        ended_by_id: int | None,
        reason: str | None = None
    ) -> None:

        # Already ended (e.g. the other side's !endcall and this
        # forced timeout raced) — nothing left to do.
        if self.active_calls.get(key) is not call:
            return

        del self.active_calls[key]

        guild = self.bot.get_guild(call["guild_id"])
        channel = guild.get_channel(call["channel_id"]) if guild else None

        # --------------------------------------------------------
        # TEAR DOWN THE PRIVATE CALL LINE — the temporary thread
        # and voice channel created in CallResponseView.accept.
        # Best-effort: a call that never got a real thread/voice
        # channel (creation failed at accept time) has None here
        # and this is just skipped.
        # --------------------------------------------------------

        if guild is not None:

            thread_id = call.get("thread_id")

            if thread_id is not None:

                thread = guild.get_thread(thread_id)

                if thread is not None:

                    try:
                        await thread.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass

            voice_channel_id = call.get("voice_channel_id")

            if voice_channel_id is not None:

                voice_channel = guild.get_channel(voice_channel_id)

                if voice_channel is not None:

                    try:
                        await voice_channel.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass

        end_text = (
            "\u260e\ufe0f Call ended"
            if reason is None
            else f"\u260e\ufe0f Call ended — {reason}"
        )

        if channel is not None:

            try:
                msg = await channel.fetch_message(call["message_id"])
                await msg.edit(content=end_text)
                asyncio.create_task(
                    _delete_after_delay(msg, CALL_STATUS_DELETE_DELAY_SECONDS)
                )

            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        for participant_id in (call["caller_id"], call["recipient_id"]):

            # Whoever explicitly ran !endcall already sees the
            # status line above / gets their own reply — no need
            # to also DM them.
            if participant_id == ended_by_id:
                continue

            try:
                user = self.bot.get_user(participant_id) or await self.bot.fetch_user(participant_id)

                dm_text = (
                    f"\u260e\ufe0f Call ended — {reason}."
                    if reason is not None
                    else "\u260e\ufe0f The other side ended the call."
                )

                note = await user.send(dm_text)
                asyncio.create_task(
                    _delete_after_delay(note, CALL_STATUS_DELETE_DELAY_SECONDS)
                )

            except (discord.Forbidden, discord.NotFound):
                pass

    # ============================================================
    # !ADDCONTACT
    # ============================================================

    @commands.command(name="addcontact")
    async def addcontact(self, ctx: commands.Context, member: discord.Member = None):

        if member is None:
            await self._reply(ctx, "Usage: `!addcontact @player`")
            return

        if member.id == ctx.author.id:
            await self._reply(ctx, "You can't add yourself.")
            return

        database.add_contact(ctx.author.id, member.id, label=member.display_name)

        await self._reply(
            ctx, f"\u2705 Added **{member.display_name}** to your contacts."
        )

    # ============================================================
    # !CONTACTS
    # ============================================================

    @commands.command(name="contacts")
    async def contacts(self, ctx: commands.Context):

        rows = database.get_contacts(ctx.author.id)

        if not rows:
            await self._reply(
                ctx,
                "You have no saved contacts yet. Use `!addcontact @player`."
            )
            return

        lines = [
            f"\u2022 {row['label'] or ('<@' + row['contact_id'] + '>')}"
            for row in rows
        ]

        await self._reply(
            ctx, "\U0001f4f1 **Your Contacts**\n" + "\n".join(lines)
        )

    # ============================================================
    # !TEXT — two-stage, delivered to the recipient's current
    # location channel.
    # ============================================================

    @commands.command(name="text")
    async def text(
        self,
        ctx: commands.Context,
        member: discord.Member = None,
        *,
        message: str = None
    ):

        if member is None or not message:
            await self._reply(ctx, "Usage: `!text @player <message>`")
            return

        if not database.is_contact(ctx.author.id, member.id):
            await self._reply(
                ctx,
                f"\u26d4 {member.display_name} isn't in your contacts. "
                f"Use `!addcontact @{member.name}` first."
            )
            return

        ok, note = await send_text(self.bot, ctx.guild, ctx.author, member.id, message)

        await self._reply(ctx, note)

    # ============================================================
    # !REPLY — reply to whoever last texted you.
    # ============================================================

    @commands.command(name="reply")
    async def reply(self, ctx: commands.Context, *, message: str = None):

        if not message:
            await self._reply(ctx, "Usage: `!reply <message>`")
            return

        sender_id = database.get_last_text_sender(ctx.author.id)

        if sender_id is None:
            await self._reply(ctx, "Nobody has texted you yet.")
            return

        ok, note = await send_text(self.bot, ctx.guild, ctx.author, sender_id, message)

        await self._reply(ctx, note)

    # ============================================================
    # !CALL
    # ============================================================

    @commands.command(name="call")
    async def call(self, ctx: commands.Context, member: discord.Member = None):

        if member is None:
            await self._reply(ctx, "Usage: `!call @player`")
            return

        if member.id == ctx.author.id:
            await self._reply(ctx, "You can't call yourself.")
            return

        if not database.is_contact(ctx.author.id, member.id):
            await self._reply(
                ctx,
                f"\u26d4 {member.display_name} isn't in your contacts. "
                f"Use `!addcontact @{member.name}` first."
            )
            return

        existing_key, _ = self._find_active_call(ctx.author.id)

        if existing_key is not None:
            await self._reply(ctx, "\u26d4 You're already in a call. Use `!endcall` first.")
            return

        # Ringing/missed/declined calls are free — but the caller
        # still needs enough airtime to cover at least the first
        # tick, or metering would end the call before it even
        # gets going the moment it connects.
        caller_player = database.get_or_create_player(ctx.author.id)

        if caller_player["airtime_balance"] < AIRTIME_CALL_COST_PER_TICK:
            await self._reply(ctx, AIRTIME_INSUFFICIENT_MESSAGE)
            return

        status_msg = await ctx.send(f"\U0001f4de {member.mention} incoming call...")

        dm_view = CallResponseView(
            self.bot,
            self.active_calls,
            ctx.author.id,
            member.id,
            ctx.guild.id,
            ctx.channel.id,
            status_msg.id,
            cog=self
        )

        dm_embed = discord.Embed(
            description=f"\U0001f4de Incoming call from **{ctx.author.display_name}**",
            color=TEXT_COLOR
        )

        try:
            dm_msg = await member.send(embed=dm_embed, view=dm_view)
            dm_view.dm_message = dm_msg

        except discord.Forbidden:

            try:
                await status_msg.edit(content="\u260e\ufe0f Missed call")
            except discord.HTTPException:
                pass

            asyncio.create_task(
                _delete_after_delay(status_msg, CALL_STATUS_DELETE_DELAY_SECONDS)
            )

            await self._reply(
                ctx,
                f"\u26d4 Couldn't reach {member.display_name} — their DMs are closed."
            )

    # ============================================================
    # !ENDCALL
    # ============================================================

    @commands.command(name="endcall")
    async def endcall(self, ctx: commands.Context):

        key, call = self._find_active_call(ctx.author.id)

        if call is None:
            await self._reply(ctx, "You're not in a call.")
            return

        await self._end_call(key, call, ended_by_id=ctx.author.id)

        await self._reply(ctx, "\u260e\ufe0f Call ended.")

    # ============================================================
    # !AIRTIME — top-up (or check) the metered airtime_balance.
    #
    # airtime_balance is its own ledger, separate from the bank
    # balance (same pattern as aed_balance/mvr_balance) — every
    # !text/!reply/connected !call spends from it, never from the
    # bank balance directly. `!airtime <amount>` converts that
    # much bank cash into airtime credit; `!airtime` with no
    # amount just reports the current balance.
    # ============================================================

    @commands.command(name="airtime")
    async def airtime(self, ctx: commands.Context, amount: int = None):

        player = database.get_or_create_player(ctx.author.id)

        if amount is None:
            await self._reply(
                ctx,
                f"\U0001f4f6 Airtime balance: \u20a6{player['airtime_balance']:,}\n"
                f"Use `!airtime <amount>` to top up from your bank balance."
            )
            return

        if amount <= 0:
            await self._reply(ctx, "Usage: `!airtime <amount>`")
            return

        if player["balance"] < amount:
            await self._reply(
                ctx,
                f"\u26d4 You need \u20a6{amount:,}. "
                f"You have \u20a6{player['balance']:,}."
            )
            return

        database.update_player(
            ctx.author.id,
            balance=player["balance"] - amount,
            airtime_balance=player["airtime_balance"] + amount
        )

        await self._reply(
            ctx,
            f"\U0001f4f6 Airtime top-up successful — \u20a6{amount:,} "
            f"moved from your bank balance.\n"
            f"Airtime balance: \u20a6{player['airtime_balance'] + amount:,}\n"
            f"Bank balance: \u20a6{player['balance'] - amount:,}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ContactsCog(bot))
