"""
BRT CARD COMMANDS.

Handles:

    !brtcard buy
    !brtcard balance
    !brtcard recharge <amount>

BRT cards are purchased at the Taxi Company IF you type the
command yourself. Buying through the phone (cogs/phone.py) skips
that location check on purpose — the phone is meant to work from
anywhere, same as recharge already did. See `_is_taxi_channel`
below for how that distinction is made.

A player receives the:

    BRT Card

Discord role after purchasing a card.

The BRT card balance is stored separately from
the player's normal bank balance.
"""

import asyncio

import discord
from discord.ext import commands

import database


# ============================================================
# CONFIGURATION
# ============================================================

BRT_CARD_ROLE = "BRT Card"

BRT_CARD_PRICE = 0

MESSAGE_DELETE_DELAY = 10


# ============================================================
# COG
# ============================================================

class BRTCardCog(commands.Cog):

    def __init__(
        self,
        bot: commands.Bot
    ):
        self.bot = bot


    # ========================================================
    # HELPER — DELETE MESSAGE
    # ========================================================

    async def _delete_later(
        self,
        message: discord.Message,
        delay: int = MESSAGE_DELETE_DELAY
    ):
        await asyncio.sleep(delay)

        try:
            await message.delete()
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException
        ):
            pass


    # ========================================================
    # HELPER — BRT ROLE
    # ========================================================

    def _get_brt_role(
        self,
        guild: discord.Guild
    ) -> discord.Role | None:

        return discord.utils.get(
            guild.roles,
            name=BRT_CARD_ROLE
        )


    # ========================================================
    # HELPER — CHECK BRT ROLE
    # ========================================================

    def _has_brt_role(
        self,
        member: discord.Member
    ) -> bool:

        return any(
            role.name == BRT_CARD_ROLE
            for role in member.roles
        )


    # ========================================================
    # HELPER — TAXI COMPANY CHECK
    #
    # Skipped when the command was run through the phone menu
    # (cogs/phone.py sets ctx.from_phone = True before invoking)
    # — the phone doesn't require being physically anywhere.
    # A player typing `!brtcard buy` themselves still needs to
    # be in #taxi-company.
    # ========================================================

    def _is_taxi_channel(
        self,
        ctx: commands.Context
    ) -> bool:

        if getattr(ctx, "from_phone", False):
            return True

        return (
            ctx.channel.name
            == "taxi-company"
        )


    # ========================================================
    # !BRTCARD
    # ========================================================

    @commands.group(
        name="brtcard",
        invoke_without_command=True
    )
    async def brtcard(
        self,
        ctx: commands.Context
    ):

        message = await ctx.send(
            "🚌 **BRT Card**\n\n"
            "`!brtcard buy` — Buy a BRT Card\n"
            "`!brtcard balance` — Check BRT balance\n"
            "`!brtcard recharge <amount>` — Recharge your card"
        )

        asyncio.create_task(
            self._delete_later(
                message
            )
        )


    # ========================================================
    # !BRTCARD BUY
    # ========================================================

    @brtcard.command(
        name="buy"
    )
    async def buy_card(
        self,
        ctx: commands.Context
    ):

        # ----------------------------------------------------
        # MUST BE AT TAXI COMPANY — unless bought via the phone
        # (see _is_taxi_channel above).
        # ----------------------------------------------------

        if not self._is_taxi_channel(ctx):

            message = await ctx.send(
                "⛔ You can only purchase a "
                "BRT Card at the **Taxi Company**."
            )

            asyncio.create_task(
                self._delete_later(
                    message
                )
            )

            return

        # ----------------------------------------------------
        # PLAYER
        # ----------------------------------------------------

        database.get_or_create_player(
            ctx.author.id
        )

        # ----------------------------------------------------
        # CHECK EXISTING CARD
        # ----------------------------------------------------

        if database.has_brt_card(
            ctx.author.id
        ):

            message = await ctx.send(
                f"⛔ {ctx.author.mention}, "
                "you already have a BRT Card."
            )

            asyncio.create_task(
                self._delete_later(
                    message
                )
            )

            return

        # ----------------------------------------------------
        # CARD PRICE
        # ----------------------------------------------------

        if BRT_CARD_PRICE > 0:

            player = database.get_player(
                ctx.author.id
            )

            if player["balance"] < BRT_CARD_PRICE:

                message = await ctx.send(
                    f"⛔ {ctx.author.mention}, "
                    f"you need ₦{BRT_CARD_PRICE:,} "
                    "to purchase a BRT Card."
                )

                asyncio.create_task(
                    self._delete_later(
                        message
                    )
                )

                return

            database.update_player(
                ctx.author.id,
                balance=(
                    player["balance"]
                    - BRT_CARD_PRICE
                )
            )

        # ----------------------------------------------------
        # CREATE CARD
        # ----------------------------------------------------

        database.create_brt_card(
            ctx.author.id
        )

        # ----------------------------------------------------
        # GIVE ROLE
        # ----------------------------------------------------

        role = self._get_brt_role(
            ctx.guild
        )

        if role:

            try:
                await ctx.author.add_roles(
                    role,
                    reason="BRT Card purchased"
                )

            except discord.Forbidden:

                message = await ctx.send(
                    "⚠️ BRT Card was created, "
                    "but I could not give you the "
                    "`BRT Card` role. Please contact an admin."
                )

                asyncio.create_task(
                    self._delete_later(
                        message
                    )
                )

                return

        # ----------------------------------------------------
        # CONFIRMATION
        # ----------------------------------------------------

        message = await ctx.send(
            f"🚌 **BRT Card Activated**\n\n"
            f"{ctx.author.mention}, your BRT Card has "
            "been successfully issued.\n\n"
            f"💳 **BRT Balance:** ₦0\n"
            f"🎫 **Role:** {BRT_CARD_ROLE}\n\n"
            "You can now recharge your card and use it "
            "for BRT transportation."
        )

        asyncio.create_task(
            self._delete_later(
                message
            )
        )


    # ========================================================
    # !BRTCARD BALANCE
    # ========================================================

    @brtcard.command(
        name="balance"
    )
    async def balance(
        self,
        ctx: commands.Context
    ):

        if not database.has_brt_card(
            ctx.author.id
        ):

            message = await ctx.send(
                f"⛔ {ctx.author.mention}, "
                "you do not have a BRT Card."
            )

            asyncio.create_task(
                self._delete_later(
                    message
                )
            )

            return

        balance = database.get_brt_balance(
            ctx.author.id
        )

        message = await ctx.send(
            f"💳 {ctx.author.mention}\n"
            f"**BRT Card Balance:** ₦{balance:,}"
        )

        asyncio.create_task(
            self._delete_later(
                message
            )
        )


    # ========================================================
    # !BRTCARD RECHARGE
    # ========================================================

    @brtcard.command(
        name="recharge"
    )
    async def recharge(
        self,
        ctx: commands.Context,
        amount: int
    ):

        # ----------------------------------------------------
        # CHECK CARD
        # ----------------------------------------------------

        if not database.has_brt_card(
            ctx.author.id
        ):

            message = await ctx.send(
                f"⛔ {ctx.author.mention}, "
                "you need a BRT Card before you can recharge."
            )

            asyncio.create_task(
                self._delete_later(
                    message
                )
            )

            return

        # ----------------------------------------------------
        # CHECK AMOUNT
        # ----------------------------------------------------

        if amount <= 0:

            message = await ctx.send(
                "⛔ Recharge amount must be greater than ₦0."
            )

            asyncio.create_task(
                self._delete_later(
                    message
                )
            )

            return

        # ----------------------------------------------------
        # CHECK NORMAL BALANCE
        # ----------------------------------------------------

        player = database.get_player(
            ctx.author.id
        )

        if player is None:

            message = await ctx.send(
                "⛔ Player account not found."
            )

            asyncio.create_task(
                self._delete_later(
                    message
                )
            )

            return

        if player["balance"] < amount:

            message = await ctx.send(
                f"⛔ {ctx.author.mention}, "
                "you do not have enough money.\n\n"
                f"💰 Bank Balance: ₦{player['balance']:,}\n"
                f"💳 Recharge Amount: ₦{amount:,}"
            )

            asyncio.create_task(
                self._delete_later(
                    message
                )
            )

            return

        # ----------------------------------------------------
        # DEDUCT FROM NORMAL BALANCE
        # ----------------------------------------------------

        database.update_player(
            ctx.author.id,
            balance=(
                player["balance"]
                - amount
            )
        )

        # ----------------------------------------------------
        # ADD TO BRT CARD
        # ----------------------------------------------------

        database.add_brt_balance(
            ctx.author.id,
            amount
        )

        new_balance = database.get_brt_balance(
            ctx.author.id
        )

               # ----------------------------------------------------
        # CONFIRMATION
        # ----------------------------------------------------

        message = await ctx.send(
            f"✅ {ctx.author.mention}, "
            "your BRT Card has been recharged.\n\n"
            f"💳 **Recharge:** ₦{amount:,}\n"
            f"💳 **BRT Balance:** ₦{new_balance:,}\n"
            f"🏦 **Bank Balance:** "
            f"₦{player['balance'] - amount:,}"
        )

        asyncio.create_task(
            self._delete_later(
                message
            )
        )


# ============================================================
# EXTENSION SETUP
# ============================================================

async def setup(
    bot: commands.Bot
):

    await bot.add_cog(
        BRTCardCog(bot)
    )
