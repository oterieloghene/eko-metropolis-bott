"""
BRT CARD COMMANDS.

Handles:

    !brtcard get                       — Get a BRT Card (free)
    !brtcard balance                   — Check your BRT balance
    !brtcard recharge <amount>         — Recharge YOUR card
    !brtcard recharge <amount> @player — Recharge SOMEONE ELSE's card

The BRT Card section is intentionally kept to just those three
actions — getting a card, checking its balance, and recharging
(your own or another player's). Nothing else lives here anymore.

BRT cards are gotten at the Taxi Company IF the player types the
command themselves. Getting one through the phone (cogs/phone.py)
skips that location check on purpose — the phone is meant to work
from anywhere, same as recharge already did. See `_is_taxi_channel`
below for how that distinction is made.

A player receives the:

    BRT Card

Discord role after getting a card, and — if they hold the
Lagosians role — a one-time ₦5,000 starter bonus straight onto
the new card. That bonus is a first-card-only, Lagosians-only
perk (database.claim_brt_starter_bonus enforces the "only once"
half; the Lagosians check happens here).

The BRT card balance is stored separately from the player's
normal bank balance — recharging moves money from a player's
bank balance (players.balance) onto the BRT card.
"""

import asyncio

import discord
from discord.ext import commands

import database


# ============================================================
# CONFIGURATION
# ============================================================

BRT_CARD_ROLE = "BRT Card"

# Role that unlocks the one-time ₦5,000 first-card bonus. Kept as
# a local constant (rather than in config.py) since this is the
# only place it's checked.
LAGOSIANS_ROLE = "Lagosians"

BRT_CARD_PRICE = 0

BRT_STARTER_BONUS_AMOUNT = 5000

MESSAGE_DELETE_DELAY = 10


# ============================================================
# COG
# ============================================================

class BRTCardCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
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
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _reply(self, ctx: commands.Context, content: str) -> discord.Message:
        """Send + auto-delete, so BRT chatter doesn't clutter the channel."""

        message = await ctx.send(content)
        asyncio.create_task(self._delete_later(message))
        return message

    # ========================================================
    # HELPER — BRT ROLE
    # ========================================================

    def _get_brt_role(self, guild: discord.Guild) -> discord.Role | None:
        return discord.utils.get(guild.roles, name=BRT_CARD_ROLE)

    # ========================================================
    # HELPER — LAGOSIANS ROLE CHECK
    # ========================================================

    def _is_lagosian(self, member: discord.Member) -> bool:
        return any(role.name == LAGOSIANS_ROLE for role in member.roles)

    # ========================================================
    # HELPER — TAXI COMPANY CHECK
    #
    # Skipped when the command was run through the phone menu
    # (cogs/phone.py sets ctx.from_phone = True before invoking)
    # — the phone doesn't require being physically anywhere.
    # A player typing `!brtcard get` themselves still needs to
    # be in #taxi-company.
    # ========================================================

    def _is_taxi_channel(self, ctx: commands.Context) -> bool:

        if getattr(ctx, "from_phone", False):
            return True

        return ctx.channel.name == "taxi-company"

    # ========================================================
    # !BRTCARD
    # ========================================================

    @commands.group(name="brtcard", invoke_without_command=True)
    async def brtcard(self, ctx: commands.Context):

        await self._reply(
            ctx,
            "🚌 **BRT Card**\n\n"
            "`!brtcard get` — Get a BRT Card (free)\n"
            "`!brtcard balance` — Check your BRT balance\n"
            "`!brtcard recharge <amount>` — Recharge your card\n"
            "`!brtcard recharge <amount> @player` — Recharge "
            "someone else's card"
        )

    # ========================================================
    # !BRTCARD GET  (was "buy" — BRT Cards are free)
    # ========================================================

    @brtcard.command(name="get", aliases=["buy", "purchase"])
    async def get_card(self, ctx: commands.Context):

        # ----------------------------------------------------
        # MUST BE AT TAXI COMPANY — unless gotten via the phone
        # ----------------------------------------------------

        if not self._is_taxi_channel(ctx):
            await self._reply(
                ctx,
                "⛔ You can only get a BRT Card at the **Taxi Company**."
            )
            return

        database.get_or_create_player(ctx.author.id)

        # ----------------------------------------------------
        # CHECK EXISTING CARD
        # ----------------------------------------------------

        if database.has_brt_card(ctx.author.id):
            await self._reply(
                ctx,
                f"⛔ {ctx.author.mention}, you already have a BRT Card."
            )
            return

        # ----------------------------------------------------
        # CARD PRICE — kept for safety, but BRT_CARD_PRICE is 0
        # ----------------------------------------------------

        if BRT_CARD_PRICE > 0:

            player = database.get_player(ctx.author.id)

            if player["balance"] < BRT_CARD_PRICE:
                await self._reply(
                    ctx,
                    f"⛔ {ctx.author.mention}, you need "
                    f"₦{BRT_CARD_PRICE:,} to get a BRT Card."
                )
                return

            database.update_player(
                ctx.author.id,
                balance=player["balance"] - BRT_CARD_PRICE
            )

        # ----------------------------------------------------
        # CREATE CARD
        # ----------------------------------------------------

        database.create_brt_card(ctx.author.id)

        # ----------------------------------------------------
        # GIVE ROLE
        # ----------------------------------------------------

        role = self._get_brt_role(ctx.guild)

        if role:

            try:
                await ctx.author.add_roles(role, reason="BRT Card issued")

            except discord.Forbidden:
                await self._reply(
                    ctx,
                    "⚠️ BRT Card was created, but I could not give you "
                    "the `BRT Card` role. Please contact an admin."
                )
                return

        # ----------------------------------------------------
        # ONE-TIME ₦5,000 STARTER BONUS — LAGOSIANS ONLY
        # ----------------------------------------------------

        bonus_line = ""

        if self._is_lagosian(ctx.author):

            granted = database.claim_brt_starter_bonus(
                ctx.author.id,
                amount=BRT_STARTER_BONUS_AMOUNT
            )

            if granted:
                bonus_line = (
                    f"\n🎁 Lagosians first-card bonus: "
                    f"₦{BRT_STARTER_BONUS_AMOUNT:,} added.\n"
                )

        new_balance = database.get_brt_balance(ctx.author.id)

        # ----------------------------------------------------
        # CONFIRMATION
        # ----------------------------------------------------

        await self._reply(
            ctx,
            f"🚌 **BRT Card Activated**\n\n"
            f"{ctx.author.mention}, your BRT Card has "
            "been successfully issued.\n"
            f"{bonus_line}\n"
            f"💳 **BRT Balance:** ₦{new_balance:,}\n"
            f"🎫 **Role:** {BRT_CARD_ROLE}\n\n"
            "You can now recharge your card and use it "
            "for BRT transportation."
        )

    # ========================================================
    # !BRTCARD BALANCE
    # ========================================================

    @brtcard.command(name="balance")
    async def balance(self, ctx: commands.Context):

        if not database.has_brt_card(ctx.author.id):
            await self._reply(
                ctx,
                f"⛔ {ctx.author.mention}, you do not have a BRT Card."
            )
            return

        balance = database.get_brt_balance(ctx.author.id)

        await self._reply(
            ctx,
            f"💳 {ctx.author.mention}\n**BRT Card Balance:** ₦{balance:,}"
        )

    # ========================================================
    # !BRTCARD RECHARGE <amount> [@player]
    #
    # No @player -> recharges YOUR OWN card.
    # With @player -> recharges SOMEONE ELSE's card, paid for out
    # of your own bank balance.
    # ========================================================

    @brtcard.command(name="recharge")
    async def recharge(
        self,
        ctx: commands.Context,
        amount: int,
        target: discord.Member = None
    ):

        recipient = target or ctx.author
        recharging_other = target is not None and target.id != ctx.author.id

        # ----------------------------------------------------
        # CHECK CARD (on the account being recharged)
        # ----------------------------------------------------

        if not database.has_brt_card(recipient.id):

            if recharging_other:
                await self._reply(
                    ctx,
                    f"⛔ {recipient.display_name} doesn't have a "
                    "BRT Card yet."
                )
            else:
                await self._reply(
                    ctx,
                    f"⛔ {ctx.author.mention}, you need a BRT Card "
                    "before you can recharge."
                )

            return

        # ----------------------------------------------------
        # CHECK AMOUNT
        # ----------------------------------------------------

        if amount <= 0:
            await self._reply(ctx, "⛔ Recharge amount must be greater than ₦0.")
            return

        # ----------------------------------------------------
        # CHECK PAYER'S BANK BALANCE — always the command author,
        # even when recharging someone else's card.
        # ----------------------------------------------------

        payer = database.get_player(ctx.author.id)

        if payer is None:
            await self._reply(ctx, "⛔ Player account not found.")
            return

        if payer["balance"] < amount:
            await self._reply(
                ctx,
                f"⛔ {ctx.author.mention}, you do not have enough money.\n\n"
                f"🏦 Bank Balance: ₦{payer['balance']:,}\n"
                f"💳 Recharge Amount: ₦{amount:,}"
            )
            return

        # ----------------------------------------------------
        # DEDUCT FROM PAYER'S BANK BALANCE
        # ----------------------------------------------------

        database.update_player(
            ctx.author.id,
            balance=payer["balance"] - amount
        )

        # ----------------------------------------------------
        # ADD TO RECIPIENT'S BRT CARD
        # ----------------------------------------------------

        database.add_brt_balance(recipient.id, amount)

        new_recipient_balance = database.get_brt_balance(recipient.id)
        new_payer_balance = payer["balance"] - amount

        # ----------------------------------------------------
        # CONFIRMATION
        # ----------------------------------------------------

        if recharging_other:
            await self._reply(
                ctx,
                f"✅ {ctx.author.mention}, you recharged "
                f"{recipient.display_name}'s BRT Card.\n\n"
                f"💳 **Recharge:** ₦{amount:,}\n"
                f"💳 **Their BRT Balance:** ₦{new_recipient_balance:,}\n"
                f"🏦 **Your Bank Balance:** ₦{new_payer_balance:,}"
            )
        else:
            await self._reply(
                ctx,
                f"✅ {ctx.author.mention}, your BRT Card has been "
                "recharged.\n\n"
                f"💳 **Recharge:** ₦{amount:,}\n"
                f"💳 **BRT Balance:** ₦{new_recipient_balance:,}\n"
                f"🏦 **Bank Balance:** ₦{new_payer_balance:,}"
            )


# ============================================================
# EXTENSION SETUP
# ============================================================

async def setup(bot: commands.Bot):
    await bot.add_cog(BRTCardCog(bot))
