"""
Personal Inventory — !give and !inv
====================================

Owns the `inventory` table (database.add_inventory_item /
get_inventory / transfer_inventory_item) that
cogs/business_shop.py's !sell fills up when a paid !buy tab (or a
walk-up sale) is handed over. This cog is the two commands a
player uses to look at and move around what they're actually
holding — !buy/!sell (business_shop.py) is how it gets INTO an
inventory in the first place; nothing here creates items out of
nothing.

Categories are the shared set from cogs/business_admin.py
(SHOP_CATEGORIES / CATEGORY_LABELS) — the same ones a business's
!menu/!buy use — so an item never changes category between a
shop's shelf and a player's inventory.

    !inv
        Usable anywhere. Ephemeral "pick a category to view"
        dropdown; picking one shows that category's items (name +
        qty) as a private follow-up message. Categories the player
        holds nothing in still show up, listed as "Empty" — same
        as every other category.

    !give @player
        Must be typed in the channel matching YOUR current
        location, and the target must also currently be at that
        same location. Ephemeral category -> item dropdown built
        from your OWN inventory (categories you hold nothing in
        aren't shown — nothing to give from them). Picking an item
        opens a popup asking how many to give; submitting moves
        that quantity to the recipient's inventory.
"""

import asyncio

import discord
from discord.ext import commands

import database
from config import LOCATIONS
from cogs.business_admin import SHOP_CATEGORIES, CATEGORY_LABELS


# ================================================================
# AUTO-DELETE HELPERS (keep the channel from clustering up)
# ================================================================

GIVE_MESSAGE_DELETE_DELAY_SECONDS = 20


async def _delete_after_delay(msg: discord.Message, delay: float) -> None:

    await asyncio.sleep(delay)

    try:
        await msg.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _send_and_delete(ctx: commands.Context, content: str = None, **kwargs) -> discord.Message:

    msg = await ctx.send(content, **kwargs)

    asyncio.create_task(
        _delete_after_delay(msg, GIVE_MESSAGE_DELETE_DELAY_SECONDS)
    )

    try:
        await ctx.message.delete()

    except (discord.Forbidden, discord.NotFound):
        pass

    return msg


# ================================================================
# !INV — CATEGORY -> CONTENTS (read-only, ephemeral)
# ================================================================

def _format_category_contents(category: str, rows: list) -> discord.Embed:

    embed = discord.Embed(
        title=CATEGORY_LABELS.get(category, category.title()),
        color=discord.Color.blurple(),
    )

    if not rows:
        embed.description = "Empty"
    else:
        embed.description = "\n".join(
            f"{row['item_name']} x{row['qty']}" for row in rows
        )

    return embed


class _InvCategorySelect(discord.ui.Select):

    def __init__(self, user_id: int, by_category: dict):
        self.user_id = user_id
        self.by_category = by_category

        options = [
            discord.SelectOption(
                label=CATEGORY_LABELS.get(category, category.title()),
                value=category,
            )
            for category in SHOP_CATEGORIES
        ]

        super().__init__(placeholder="Pick a category to view", options=options)

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This isn't your `!inv` menu.", ephemeral=True
            )
            return

        rows = self.by_category.get(self.values[0], [])

        await interaction.response.send_message(
            embed=_format_category_contents(self.values[0], rows),
            ephemeral=True,
        )


class _InvCategoryView(discord.ui.View):
    def __init__(self, user_id, by_category):
        super().__init__(timeout=60)
        self.add_item(_InvCategorySelect(user_id, by_category))


# ================================================================
# !GIVE — CATEGORY -> ITEM -> QUANTITY (POPUP)
# ================================================================

class _GiveQtyModal(discord.ui.Modal):
    """Popup asking how many of the already-chosen item to give."""

    def __init__(self, giver_id: int, recipient_id: int, recipient_mention: str, row):
        super().__init__(title=f"Give {row['item_name']}"[:45])
        self.giver_id = giver_id
        self.recipient_id = recipient_id
        self.recipient_mention = recipient_mention
        self.item_name = row["item_name"]

        self.qty_input = discord.ui.TextInput(
            label=f"Quantity (you have {row['qty']})",
            placeholder="1",
            default="1",
            max_length=5,
        )
        self.add_item(self.qty_input)

    async def on_submit(self, interaction: discord.Interaction):

        raw = self.qty_input.value.strip()

        try:
            qty = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "⛔ Enter a whole number for quantity.", ephemeral=True
            )
            return

        if qty <= 0:
            await interaction.response.send_message(
                "⛔ Quantity must be greater than 0.", ephemeral=True
            )
            return

        ok, reason = database.transfer_inventory_item(
            self.giver_id, self.recipient_id, self.item_name, qty
        )

        if not ok:
            message = (
                "⛔ You don't have that item anymore."
                if reason == "not_found"
                else "⛔ You don't have that many to give."
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        await interaction.response.send_message(
            f"🎁 Gave **{qty} x {self.item_name}** to {self.recipient_mention}!",
            ephemeral=True,
        )


class _GiveItemSelect(discord.ui.Select):

    def __init__(self, giver_id: int, recipient_id: int, recipient_mention: str, rows: list):
        self.giver_id = giver_id
        self.recipient_id = recipient_id
        self.recipient_mention = recipient_mention
        self.rows = rows

        options = [
            discord.SelectOption(
                label=f"{row['item_name']} (have {row['qty']})",
                value=row["item_name"],
            )
            for row in rows
        ]

        super().__init__(placeholder="Choose an item...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.giver_id:
            await interaction.response.send_message(
                "⛔ This isn't your `!give` menu.", ephemeral=True
            )
            return

        row = next((r for r in self.rows if r["item_name"] == self.values[0]), None)

        if row is None:
            await interaction.response.send_message(
                "⛔ That item no longer exists.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            _GiveQtyModal(self.giver_id, self.recipient_id, self.recipient_mention, row)
        )


class _GiveItemView(discord.ui.View):
    def __init__(self, giver_id, recipient_id, recipient_mention, rows):
        super().__init__(timeout=60)
        self.add_item(_GiveItemSelect(giver_id, recipient_id, recipient_mention, rows))


class _GiveCategorySelect(discord.ui.Select):

    def __init__(self, giver_id: int, recipient_id: int, recipient_mention: str, by_category: dict):
        self.giver_id = giver_id
        self.recipient_id = recipient_id
        self.recipient_mention = recipient_mention
        self.by_category = by_category

        options = [
            discord.SelectOption(
                label=CATEGORY_LABELS.get(category, category.title()),
                value=category,
                description=f"{len(rows)} item(s)",
            )
            for category, rows in by_category.items()
        ]

        super().__init__(placeholder="Choose a category...", options=options)

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.giver_id:
            await interaction.response.send_message(
                "⛔ This isn't your `!give` menu.", ephemeral=True
            )
            return

        rows = self.by_category.get(self.values[0], [])

        if not rows:
            await interaction.response.send_message(
                "⛔ Nothing of yours in that category right now.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content=f"🎁 Choose an item to give to {self.recipient_mention}:",
            view=_GiveItemView(self.giver_id, self.recipient_id, self.recipient_mention, rows),
        )


class _GiveCategoryView(discord.ui.View):
    def __init__(self, giver_id, recipient_id, recipient_mention, by_category):
        super().__init__(timeout=60)
        self.add_item(_GiveCategorySelect(giver_id, recipient_id, recipient_mention, by_category))


# ================================================================
# COG
# ================================================================

class GiveCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="inv")
    async def inv(self, ctx: commands.Context):

        rows = database.get_inventory(ctx.author.id)

        by_category: dict[str, list] = {}
        for row in rows:
            by_category.setdefault(row["category"], []).append(row)

        await ctx.send(
            "📦 Pick a category to view (only you will see the contents):",
            view=_InvCategoryView(ctx.author.id, by_category),
        )

    @commands.command(name="give")
    async def give(self, ctx: commands.Context, member: discord.Member = None):

        if member is None:

            await _send_and_delete(
                ctx,
                "Usage: `!give @player` — must be someone standing "
                "at the same location as you."
            )

            return

        if member.bot:

            await _send_and_delete(ctx, "⛔ You can't give items to a bot.")
            return

        if member.id == ctx.author.id:

            await _send_and_delete(ctx, "⛔ You can't give an item to yourself.")
            return

        giver = database.get_or_create_player(ctx.author.id)
        recipient = database.get_or_create_player(member.id)

        giver_loc = LOCATIONS.get(giver["location"])

        # Must be typed from wherever the giver actually currently
        # is — same "type it where you actually are" rule used
        # throughout the rest of the game.
        if giver_loc is None or ctx.channel.name != giver_loc["channel"]:

            expected = giver_loc["channel"] if giver_loc else "your current location"

            await _send_and_delete(
                ctx,
                f"⛔ You need to be in #{expected} to do this."
            )

            return

        if recipient["location"] != giver["location"]:

            await _send_and_delete(
                ctx,
                f"⛔ {member.mention} isn't at "
                f"**{giver_loc['name']}** with you right now."
            )

            return

        rows = database.get_inventory(ctx.author.id)

        if not rows:

            await _send_and_delete(ctx, "⛔ Your inventory is empty.")
            return

        by_category: dict[str, list] = {}
        for row in rows:
            by_category.setdefault(row["category"], []).append(row)

        await _send_and_delete(
            ctx,
            f"🎁 Choose a category to give from, for {member.mention}:",
            view=_GiveCategoryView(ctx.author.id, member.id, member.mention, by_category),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveCog(bot))
