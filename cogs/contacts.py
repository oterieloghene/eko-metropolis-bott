"""
Contacts + texting — save another player as a contact, then send
them a text that lands in their Discord DMs, like a real phone.

Self-contained: doesn't touch any other cog. phone.py's Contacts
button drives this the same way it drives Bus/Taxi/BRT — by
building a ctx and calling ctx.invoke() on the real commands here.

WHY DMS, NOT A SHARED "CHANNEL":

    Discord bots can't create a private two-person text thread on
    the fly without a lot of extra channel-management overhead
    (and permissions this bot doesn't currently manage). A DM from
    the bot, formatted to look like a text and tagging who it's
    from, gets the "text a contact" experience without any of
    that — and it works even if the two players have never spoken
    in a shared channel before.

REPLYING:

    The DM the recipient gets includes a "Reply" button, so they
    don't need to already have the sender saved as a contact to
    write back — same as replying to a text from an unsaved
    number on a real phone. That's independent of the persistent
    "last_text_sender" record `!reply` also uses, which survives
    a bot restart (the Reply button doesn't, since it's a normal
    timed view).
"""

import discord
from discord.ext import commands

import database


TEXT_TIMEOUT_SECONDS = 300


# ================================================================
# SHARED SEND LOGIC — used by !text, !reply, and the Reply button
# ================================================================

async def _send_text(
    bot: commands.Bot,
    sender: discord.abc.User,
    recipient_id: int,
    content: str
) -> tuple[bool, str]:
    """
    DMs `recipient_id` a text "from" `sender`. Returns
    (ok, message_for_sender).
    """

    try:
        recipient = await bot.fetch_user(recipient_id)

    except discord.NotFound:
        return False, "That user doesn't exist."

    if recipient.id == sender.id:
        return False, "You can't text yourself."

    embed = discord.Embed(
        description=content,
        color=discord.Color.from_rgb(61, 220, 132)
    )

    embed.set_author(
        name=f"\U0001f4f1 Text from {sender.display_name}",
        icon_url=sender.display_avatar.url
    )

    view = ReplyView(bot, sender.id, recipient_id)

    try:
        await recipient.send(embed=embed, view=view)

    except discord.Forbidden:
        return False, (
            f"Couldn't deliver — {recipient.display_name} has "
            f"DMs closed."
        )

    database.set_last_text_sender(recipient_id, sender.id)

    return True, f"\U0001f4ac Text sent to {recipient.display_name}."


class ReplyModal(discord.ui.Modal, title="Reply"):

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

        ok, note = await _send_text(
            self.bot,
            interaction.user,
            self.sender_id,
            self.message.value
        )

        await interaction.followup.send(note, ephemeral=True)


class ReplyView(discord.ui.View):
    """
    Attached to every text DM. Only the recipient can press it —
    everyone else's tap is ignored (relevant if this DM somehow
    gets forwarded/quoted, and just good practice).
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
    async def reply(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.send_modal(
            ReplyModal(self.bot, self.sender_id, self.recipient_id)
        )

    async def on_timeout(self):

        for child in self.children:
            child.disabled = True


# ================================================================
# COG
# ================================================================

class ContactsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ============================================================
    # !ADDCONTACT
    # ============================================================

    @commands.command(name="addcontact")
    async def addcontact(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if member is None:

            await ctx.send("Usage: `!addcontact @player`")

            return

        if member.id == ctx.author.id:

            await ctx.send("You can't add yourself.")

            return

        database.add_contact(
            ctx.author.id,
            member.id,
            label=member.display_name
        )

        await ctx.send(
            f"\u2705 Added **{member.display_name}** to your contacts."
        )

    # ============================================================
    # !CONTACTS
    # ============================================================

    @commands.command(name="contacts")
    async def contacts(self, ctx: commands.Context):

        rows = database.get_contacts(ctx.author.id)

        if not rows:

            await ctx.send(
                "You have no saved contacts yet. Use "
                "`!addcontact @player`."
            )

            return

        lines = [
            f"\u2022 {row['label'] or ('<@' + row['contact_id'] + '>')}"
            for row in rows
        ]

        await ctx.send(
            "\U0001f4f1 **Your Contacts**\n" + "\n".join(lines)
        )

    # ============================================================
    # !TEXT
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

            await ctx.send("Usage: `!text @player <message>`")

            return

        if not database.is_contact(ctx.author.id, member.id):

            await ctx.send(
                f"\u26d4 {member.display_name} isn't in your "
                f"contacts. Use `!addcontact @{member.name}` first."
            )

            return

        ok, note = await _send_text(
            self.bot, ctx.author, member.id, message
        )

        await ctx.send(note)

    # ============================================================
    # !REPLY — reply to whoever last texted you, without needing
    # them saved as a contact.
    # ============================================================

    @commands.command(name="reply")
    async def reply(
        self,
        ctx: commands.Context,
        *,
        message: str = None
    ):

        if not message:

            await ctx.send("Usage: `!reply <message>`")

            return

        sender_id = database.get_last_text_sender(ctx.author.id)

        if sender_id is None:

            await ctx.send("Nobody has texted you yet.")

            return

        ok, note = await _send_text(
            self.bot, ctx.author, sender_id, message
        )

        await ctx.send(note)


async def setup(bot: commands.Bot):
    await bot.add_cog(ContactsCog(bot))
