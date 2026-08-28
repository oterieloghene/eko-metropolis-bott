"""
!give — hand an item from your own inventory to another player
standing in the same location as you. Mainly exists so dispatch
delivery testing has something to hand off before the real
Dispatch job (bicycle/motorcycle courier work) is built.

Also owns the starter item pool: !registerplayers calls
grant_starter_items() so every newly registered player starts
with a few random items already in their inventory to test with.

    !give @player
        Must be typed in the channel matching YOUR current
        location, and the target must also currently be at that
        same location. Opens a dropdown of your own held items
        (up to 25 most recent) — picking one transfers it
        straight to them.

Items live in the same `inventory` table !mall purchases use
(database.get_inventory / add_inventory_item), priced in Naira
(currency="ngn") since price_paid is always 0 for these — free
starter/test props, not a purchase.
"""

import asyncio
import random

import discord
from discord.ext import commands

import database
from config import LOCATIONS, CURRENCY_SYMBOL


# ================================================================
# STARTER ITEM POOL
# ================================================================

STARTER_ITEM_POOL = [
    "Parcel",
    "Food Package",
    "Gift Box",
    "Keys",
]

# How many starter items each newly registered player gets, drawn
# at random (with repeats allowed) from the 4-item pool above.
STARTER_ITEM_COUNT = 3

# Items are free (price_paid=0) — this is just the currency label
# stored alongside them so !inventory can format the row.
STARTER_ITEM_CURRENCY = "ngn"
STARTER_ITEM_AREA_CODE = "starter"


def grant_starter_items(user_id: int) -> list[str]:
    """
    Give a player STARTER_ITEM_COUNT random items from the pool.
    Called by !registerplayers. Returns the list of item names
    granted, so the caller can summarize what was handed out.
    """

    granted = random.choices(STARTER_ITEM_POOL, k=STARTER_ITEM_COUNT)

    for item_name in granted:

        database.add_inventory_item(
            user_id,
            area_code=STARTER_ITEM_AREA_CODE,
            item_name=item_name,
            price_paid=0,
            currency=STARTER_ITEM_CURRENCY,
        )

    return granted


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
# GIVE DROPDOWN
# ================================================================

class GiveItemSelect(discord.ui.Select):
    """
    Lists the giver's own inventory (most recent 25 — a Select
    can't hold more). Picking one transfers that exact row to
    the recipient chosen when !give was typed.
    """

    def __init__(
        self,
        giver_id: int,
        recipient_id: int,
        items: list,
    ):

        options = [
            discord.SelectOption(
                label=row["item_name"][:100],
                value=str(row["item_id"]),
                description=(
                    f"{row['price_paid']:,} "
                    f"{CURRENCY_SYMBOL.get(row['currency'], row['currency'].upper())}"
                    if row["price_paid"]
                    else "Free item"
                ),
            )
            for row in items[:25]
        ]

        super().__init__(
            placeholder="Choose an item to give...",
            min_values=1,
            max_values=1,
            options=options,
        )

        self.giver_id = giver_id
        self.recipient_id = recipient_id

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.giver_id:

            await interaction.response.send_message(
                "⛔ This isn't your `!give` menu.",
                ephemeral=True
            )

            return

        item_id = int(self.values[0])

        moved = database.transfer_inventory_item(
            item_id,
            self.giver_id,
            self.recipient_id,
        )

        if not moved:

            await interaction.response.edit_message(
                content="⛔ That item isn't available anymore — "
                        "maybe it was already given away.",
                view=None
            )

            return

        for child in self.view.children:
            child.disabled = True

        await interaction.response.edit_message(
            content=f"🎁 Given to <@{self.recipient_id}>!",
            view=self.view
        )

        msg = await interaction.original_response()

        asyncio.create_task(
            _delete_after_delay(msg, GIVE_MESSAGE_DELETE_DELAY_SECONDS)
        )


class GiveItemView(discord.ui.View):

    def __init__(self, giver_id: int, recipient_id: int, items: list):
        super().__init__(timeout=60)
        self.add_item(GiveItemSelect(giver_id, recipient_id, items))

    async def on_timeout(self):

        for child in self.children:
            child.disabled = True


# ================================================================
# COG
# ================================================================

class GiveCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

        items = database.get_inventory(ctx.author.id)

        if not items:

            await _send_and_delete(ctx, "⛔ Your inventory is empty.")
            return

        view = GiveItemView(ctx.author.id, member.id, items)

        await _send_and_delete(
            ctx,
            f"🎁 Choose an item to give to {member.mention}:",
            view=view
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveCog(bot))
