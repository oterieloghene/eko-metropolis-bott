"""
Shopping — Downtown Dubai / Paradise Resort.

All three commands only work inside the player's CURRENT area
thread, enforced by checks.require_area("shop") (same idea as
hotel.py's !eat only working inside an active hotel thread).

    !mall     -> test-phase feature, currently on hold. Its item
                 lists (config.MALL_ITEMS) are emptied out — !mall
                 just tells the player the mall is closed until
                 this gets folded into the real shop/inventory
                 system properly.
    !fastfood -> random-flavor restaurant order, deducts local
                 currency, no inventory.
    !spa      -> same idea, spa service.

!fastfood and !spa post a plain list the player can order from via
a dropdown — nothing from them is kept afterwards; they're pure
"spend money, get a flavor message" actions.

The player's actual personal inventory (what !mall used to add to,
back when it had items) now lives entirely in cogs/give.py's
!inv/!give, filled by cogs/business_shop.py's !buy/!sell.
"""

import discord
from discord.ext import commands

import checks
import database

from config import (
    AREAS,
    COUNTRY_CURRENCY,
    CURRENCY_SYMBOL,
    MALL_ITEMS,
    FASTFOOD_MENU,
    SPA_SERVICES,
)


# ================================================================
# HELPERS
# ================================================================

def _currency_for_area(area_code: str) -> str:
    return COUNTRY_CURRENCY[AREAS[area_code]["country"]]


def _balance_field(currency: str) -> str:
    return "aed_balance" if currency == "aed" else "mvr_balance"


def _fmt_money(amount: int, currency: str) -> str:
    return f"{amount:,} {CURRENCY_SYMBOL[currency]}"


async def _charge(user_id: int, currency: str, amount: int) -> tuple[bool, int]:
    """Attempt to deduct `amount` of `currency` from the player's
    wallet. Returns (success, current_balance_after_or_before)."""

    player = database.get_or_create_player(user_id)
    field = _balance_field(currency)
    balance = player[field]

    if balance < amount:
        return False, balance

    database.update_player(user_id, **{field: balance - amount})
    return True, balance - amount


# ================================================================
# DROPDOWNS
# ================================================================

class _MallSelect(discord.ui.Select):

    def __init__(self, area_code: str, currency: str):
        self.area_code = area_code
        self.currency = currency

        options = [
            discord.SelectOption(
                label=item["name"],
                value=item["name"],
                description=f"{item['price']:,} {CURRENCY_SYMBOL[currency]}",
            )
            for item in MALL_ITEMS[area_code]
        ]

        super().__init__(placeholder="Choose an item to buy...", options=options)

    async def callback(self, interaction: discord.Interaction):
        item = next(i for i in MALL_ITEMS[self.area_code] if i["name"] == self.values[0])

        ok, balance = await _charge(interaction.user.id, self.currency, item["price"])

        if not ok:
            await interaction.response.send_message(
                f"\u26d4 You need {_fmt_money(item['price'], self.currency)} for the "
                f"**{item['name']}**. You have {_fmt_money(balance, self.currency)}.",
                ephemeral=True,
            )
            return

        database.add_inventory_item(
            interaction.user.id, "food_drinks", "SNACKS", item["name"], 1
        )

        await interaction.response.send_message(
            f"\U0001f6cd\ufe0f Bought **{item['name']}** for "
            f"{_fmt_money(item['price'], self.currency)}. It's in your inventory now.",
            ephemeral=True,
        )


class _MallView(discord.ui.View):
    def __init__(self, area_code: str, currency: str):
        super().__init__(timeout=60)
        self.add_item(_MallSelect(area_code, currency))


class _OrderSelect(discord.ui.Select):
    """Shared dropdown for !fastfood and !spa — spend currency,
    get a flavor confirmation, nothing kept afterwards."""

    def __init__(self, area_code: str, currency: str, menu: list[dict], verb: str, emoji: str):
        self.currency = currency
        self.menu = menu
        self.verb = verb
        self.emoji = emoji

        options = [
            discord.SelectOption(
                label=entry["name"],
                value=entry["name"],
                description=f"{entry['price']:,} {CURRENCY_SYMBOL[currency]}",
            )
            for entry in menu
        ]

        super().__init__(placeholder=f"Choose what to {verb}...", options=options)

    async def callback(self, interaction: discord.Interaction):
        entry = next(e for e in self.menu if e["name"] == self.values[0])

        ok, balance = await _charge(interaction.user.id, self.currency, entry["price"])

        if not ok:
            await interaction.response.send_message(
                f"\u26d4 You need {_fmt_money(entry['price'], self.currency)} for "
                f"**{entry['name']}**. You have {_fmt_money(balance, self.currency)}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"{self.emoji} You {self.verb} **{entry['name']}** for "
            f"{_fmt_money(entry['price'], self.currency)}. Enjoy!",
            ephemeral=True,
        )


class _OrderView(discord.ui.View):
    def __init__(self, area_code: str, currency: str, menu: list[dict], verb: str, emoji: str):
        super().__init__(timeout=60)
        self.add_item(_OrderSelect(area_code, currency, menu, verb, emoji))


# ================================================================
# COG
# ================================================================

class ShopsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="mall")
    @checks.require_area("shop")
    async def mall(self, ctx: commands.Context):
        area_code = ctx.area["area_code"]

        if not MALL_ITEMS.get(area_code):
            await ctx.send("\U0001f6aa The mall is currently closed.")
            return

        currency = _currency_for_area(area_code)

        await ctx.send(
            f"\U0001f6cd\ufe0f **Mall** \u2014 prices in {CURRENCY_SYMBOL[currency]}",
            view=_MallView(area_code, currency),
        )

    @commands.command(name="fastfood")
    @checks.require_area("shop")
    async def fastfood(self, ctx: commands.Context):
        area_code = ctx.area["area_code"]
        currency = _currency_for_area(area_code)

        await ctx.send(
            f"\U0001f37d\ufe0f **Order food** \u2014 prices in {CURRENCY_SYMBOL[currency]}",
            view=_OrderView(area_code, currency, FASTFOOD_MENU[area_code], "order", "\U0001f37d\ufe0f"),
        )

    @commands.command(name="spa")
    @checks.require_area("shop")
    async def spa(self, ctx: commands.Context):
        area_code = ctx.area["area_code"]
        currency = _currency_for_area(area_code)

        await ctx.send(
            f"\U0001f9d6 **Spa menu** \u2014 prices in {CURRENCY_SYMBOL[currency]}",
            view=_OrderView(area_code, currency, SPA_SERVICES[area_code], "book", "\U0001f9d6"),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ShopsCog(bot))
