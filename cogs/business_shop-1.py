"""
Phase 5 — Shop / Inventory System
==================================

Builds on Phase 4 (cogs/business_admin.py's registration +
cogs/banking.py's !create-business-account) and Phase 1's dynamic
`locations` table — every business is a real map location, so
"location-gated to that business area" below means exactly what it
means for cogs/dealership.py's !cars: the command must be typed in
the business's own channel, AND the player's database location must
actually be that business's code. See _require_business() below,
which is that same check generalized off `database.get_location()`
instead of config.LOCATIONS (businesses are never hand-authored).

Categories (food / drink / gas_oil / merchandise) and which of them
each business_type is licensed to stock live in
cogs/business_admin.py (BUSINESS_TYPE_CATEGORIES / SHOP_CATEGORIES)
— see that file for why the licensing is enforced at the top-level
category, not the spec's finer raw/cooked/snacks-style
sub-categories.

Commands:

    !add <category> <price> <qty> <item name...>
        Owner-only, at their own business. Adds (or restocks/
        reprices, if the item name already exists) a catalog line.
        `category` must be one this business's business_type is
        licensed to sell.

    !menu
        Anyone physically at the business. Business name up top,
        flat list of item / stock / price — no category grouping,
        per spec.

    !buy [quantity]
        Customer-only (not the owner), physically at the business.
        Private two-step dropdown: category (licensed to this
        business, i.e. any category actually stocked) -> item.
        Adds `quantity` (default 1) of the chosen item, at its
        current price, to the OPEN cash register between this
        customer and this business — creating one if none exists
        yet. Does NOT touch stock. Pay with !pay or !transfer
        before the goods are handed over via !sell.

    !sell <@customer>
        Owner-only, at their own business. Fulfills that customer's
        PAID (already-settled-by-!pay/!transfer) register in full —
        deducts stock for every line and hands the goods over.

    !sell <@customer> <quantity> <item name...>
        Owner-only, at their own business. Standalone walk-up cash
        sale with no prior !buy — directly deducts `quantity` of
        `item name` from stock. Does not move any money itself
        (assumes payment already happened outside the register
        system); this only performs the fulfillment/stock side.

    !close-register <@customer>
        Owner-only, at their own business. Manually cancels an
        OPEN (unpaid) register with that customer — these never
        auto-expire, so this is the only way to clear a stale tab
        besides paying it.

    !order <business_code> <quantity> <item name...>
        At the depot (config.LOCATIONS["depot"]). Restocks an
        EXISTING catalog item (added via !add first) at a business
        the caller owns. Per spec, only mall/club owners restock
        this way — mamaput and gasstation owners are not gated to
        this command.
"""

import json
import sqlite3

import discord
from discord.ext import commands

import checks
import database

from cogs import dealership
from cogs.business_admin import BUSINESS_TYPE_CATEGORIES, SHOP_CATEGORIES


DEPOT_CODE = "depot"

# Per spec's final Phase 5 bullet: "!order — at the depot,
# mall/club owners restock." Deliberately excludes mamaput/
# gasstation — narrower than the general "any business_type sells
# in its licensed categories" rule everywhere else in this file.
ORDER_ELIGIBLE_TYPES = ("mall", "club")

# How long a !buy order's transient "@player ordered X from @owner"
# flavor message stays up before self-deleting. The PERMANENT record
# of the sale is the itemized receipt posted on successful payment
# (cogs/banking.py), not this message.
ORDER_MESSAGE_LIFETIME_SECONDS = 15

_CATEGORY_LABELS = {
    "food": "🍲 Food",
    "drink": "🥤 Drinks",
    "gas_oil": "⛽ Gas & Oil",
    "merchandise": "🛍️ Merchandise",
}


# ================================================================
# SHARED LOCATION GATE
# ================================================================
#
# Same shape as cogs/banking.py's _require_sub_location and
# cogs/dealership.py's checks.require_location(DEALERSHIP_CODE) —
# generalized to a dynamically-registered business location instead
# of a static one or a sub-location.
# ================================================================

async def _require_business(ctx: commands.Context) -> "sqlite3.Row | None":

    player = database.get_or_create_player(ctx.author.id)

    if player["traveling"]:
        await ctx.send("⛔ You are currently travelling and cannot do this.")
        return None

    code = player["location"]
    business = database.get_business(code)

    if business is None:
        await ctx.send(
            "⛔ You need to be physically at a registered business to do this."
        )
        return None

    location = database.get_location(code)

    if location is None or ctx.channel.name != location["channel_name"]:
        expected = location["channel_name"] if location else "the business's own channel"
        await ctx.send(f"⛔ This only works in #{expected}.")
        return None

    return business


def _licensed_categories(business_type: str) -> tuple:
    return BUSINESS_TYPE_CATEGORIES.get(business_type, ())


# ================================================================
# !BUY — CATEGORY -> ITEM DROPDOWNS
# ================================================================

class _ItemSelect(discord.ui.Select):

    def __init__(self, business: "sqlite3.Row", items: list, qty: int):
        self.business = business
        self.items = items
        self.qty = qty

        options = [
            discord.SelectOption(
                label=f"{row['item_name']} (₦{row['price']:,})",
                value=row["item_name"],
                description=f"In stock: {row['stock']}",
            )
            for row in items
        ]

        super().__init__(placeholder="Choose an item...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):

        item = next((i for i in self.items if i["item_name"] == self.values[0]), None)

        if item is None:
            await interaction.response.send_message(
                "⛔ That item no longer exists.", ephemeral=True
            )
            return

        if item["stock"] < self.qty:
            await interaction.response.send_message(
                f"⛔ Only {item['stock']} of **{item['item_name']}** left in stock.",
                ephemeral=True,
            )
            return

        register = database.add_to_register(
            self.business["code"],
            interaction.user.id,
            item["item_name"],
            item["price"],
            self.qty,
        )

        await interaction.response.send_message(
            f"🧾 Added **{self.qty} x {item['item_name']}** (₦{item['price']:,} each) "
            f"to your tab at **{self.business['name']}**.\n"
            f"Running total: ₦{register['total']:,}. Pay with `!pay` or `!transfer` "
            f"for the exact total to settle it.",
            ephemeral=True,
        )

        owner = interaction.guild.get_member(int(self.business["owner_id"]))
        owner_mention = owner.mention if owner else f"the {self.business['name']} owner"

        flavor = await interaction.channel.send(
            f"🛒 {interaction.user.mention} ordered {self.qty} x "
            f"**{item['item_name']}** from {owner_mention}."
        )

        try:
            await flavor.delete(delay=ORDER_MESSAGE_LIFETIME_SECONDS)
        except discord.HTTPException:
            pass


class _ItemView(discord.ui.View):
    def __init__(self, business, items, qty):
        super().__init__(timeout=60)
        self.add_item(_ItemSelect(business, items, qty))


class _CategorySelect(discord.ui.Select):

    def __init__(self, business: "sqlite3.Row", by_category: dict, qty: int):
        self.business = business
        self.by_category = by_category
        self.qty = qty

        options = [
            discord.SelectOption(
                label=_CATEGORY_LABELS.get(category, category.title()),
                value=category,
                description=f"{len(rows)} item(s) available",
            )
            for category, rows in by_category.items()
        ]

        super().__init__(placeholder="Choose a category...", options=options)

    async def callback(self, interaction: discord.Interaction):

        rows = self.by_category.get(self.values[0], [])

        if not rows:
            await interaction.response.send_message(
                "⛔ Nothing in stock in that category right now.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content=f"🛍️ **{self.business['name']}** — pick an item:",
            view=_ItemView(self.business, rows, self.qty),
        )


class _CategoryView(discord.ui.View):
    def __init__(self, business, by_category, qty):
        super().__init__(timeout=60)
        self.add_item(_CategorySelect(business, by_category, qty))


# ================================================================
# COG
# ================================================================

class BusinessShopCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ============================================================
    # !ADD
    # ============================================================

    @commands.command(name="add")
    async def add(
        self,
        ctx: commands.Context,
        category: str,
        price: int,
        qty: int,
        *,
        item_name: str
    ):

        business = await _require_business(ctx)

        if business is None:
            return

        if str(ctx.author.id) != business["owner_id"]:
            await ctx.send(f"⛔ Only **{business['name']}**'s owner can add stock.")
            return

        category = category.lower().strip()

        if category not in SHOP_CATEGORIES:
            await ctx.send(
                f"⛔ Invalid category `{category}`. Must be one of: "
                f"{', '.join(SHOP_CATEGORIES)}."
            )
            return

        licensed = _licensed_categories(business["business_type"])

        if category not in licensed:
            await ctx.send(
                f"⛔ **{business['name']}** ({business['business_type']}) isn't "
                f"licensed to sell **{category}**. Licensed categories: "
                f"{', '.join(licensed) or 'none'}."
            )
            return

        if price <= 0:
            await ctx.send("⛔ Price must be greater than 0.")
            return

        if qty <= 0:
            await ctx.send("⛔ Quantity must be greater than 0.")
            return

        item_name = item_name.strip()

        if not item_name:
            await ctx.send("⛔ An item name is required.")
            return

        row = database.add_business_item(
            business_code=business["code"],
            category=category,
            item_name=item_name,
            price=price,
            qty=qty,
            created_by=ctx.author.id,
        )

        embed = discord.Embed(
            title="📦 Stock Added",
            color=discord.Color.green()
        )
        embed.add_field(name="Business", value=business["name"], inline=False)
        embed.add_field(name="Item", value=row["item_name"], inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Price", value=f"₦{row['price']:,}", inline=True)
        embed.add_field(name="Total Stock", value=str(row["stock"]), inline=True)

        await ctx.send(embed=embed)

    # ============================================================
    # !MENU
    # ============================================================

    @commands.command(name="menu")
    async def menu(self, ctx: commands.Context):

        business = await _require_business(ctx)

        if business is None:
            return

        items = database.get_business_items(business["code"])

        embed = discord.Embed(
            title=f"🧾 {business['name']}",
            color=discord.Color.blurple()
        )

        if not items:
            embed.description = "_Nothing on the menu yet._"
        else:
            embed.description = "\n".join(
                f"**{row['item_name']}** — ₦{row['price']:,} "
                f"(stock: {row['stock']})"
                for row in items
            )

        await ctx.send(embed=embed)

    # ============================================================
    # !BUY
    # ============================================================
    #
    # discord.py only allows one command named "buy" bot-wide, and
    # cogs/dealership.py's original !buy (vehicle purchase) already
    # claimed it — so this is now the single !buy command, and it
    # dispatches to the dealership's own flow when typed from the
    # dealership's channel (same channel-based routing every other
    # location-gated command in this bot already uses). See
    # cogs/dealership.py's buy_vehicle() for that branch; the
    # dealership's own `<vehicle name>` argument is free text,
    # while the shop-item branch below expects an optional integer
    # quantity — both are accepted as one raw string and parsed
    # per-branch below.
    # ============================================================

    @commands.command(name="buy")
    async def buy(self, ctx: commands.Context, *, arg: str = None):

        if ctx.channel.name == dealership.DEALERSHIP_CHANNEL_NAME:
            await dealership.buy_vehicle(ctx, arg)
            return

        qty = 1

        if arg is not None:
            try:
                qty = int(arg.strip())
            except ValueError:
                await ctx.send(
                    "⛔ Usage: `!buy [quantity]` — see `!menu` for what's "
                    "available here."
                )
                return

        business = await _require_business(ctx)

        if business is None:
            return

        if str(ctx.author.id) == business["owner_id"]:
            await ctx.send(
                "⛔ You can't buy from your own shop. Use `!sell` for a walk-up sale."
            )
            return

        if qty <= 0:
            await ctx.send("⛔ Quantity must be greater than 0.")
            return

        items = database.get_business_items(business["code"])
        items = [row for row in items if row["stock"] > 0]

        if not items:
            await ctx.send(f"⛔ **{business['name']}** has nothing in stock right now.")
            return

        by_category: dict[str, list] = {}
        for row in items:
            by_category.setdefault(row["category"], []).append(row)

        await ctx.send(
            f"🛍️ **{business['name']}** — pick a category:",
            view=_CategoryView(business, by_category, qty),
        )

    # ============================================================
    # !CLOSE-REGISTER
    # ============================================================

    @commands.command(name="close-register")
    async def close_register(self, ctx: commands.Context, customer: discord.Member):

        business = await _require_business(ctx)

        if business is None:
            return

        if str(ctx.author.id) != business["owner_id"]:
            await ctx.send(f"⛔ Only **{business['name']}**'s owner can close a tab.")
            return

        register = database.get_open_register(business["code"], customer.id)

        if register is None:
            await ctx.send(f"⛔ {customer.mention} doesn't have an open tab here.")
            return

        database.cancel_register(register["register_id"])

        await ctx.send(
            f"🚫 Cancelled {customer.mention}'s open tab of ₦{register['total']:,} "
            f"at **{business['name']}**."
        )

    # ============================================================
    # !SELL
    # ============================================================

    @commands.command(name="sell")
    async def sell(
        self,
        ctx: commands.Context,
        customer: discord.Member,
        qty: int = None,
        *,
        item_name: str = None
    ):

        business = await _require_business(ctx)

        if business is None:
            return

        if str(ctx.author.id) != business["owner_id"]:
            await ctx.send(f"⛔ Only **{business['name']}**'s owner can sell here.")
            return

        # --------------------------------------------------------
        # Standalone walk-up sale — quantity + item name given
        # explicitly, no register involved at all.
        # --------------------------------------------------------

        if qty is not None or item_name is not None:

            if qty is None or item_name is None:
                await ctx.send(
                    "⛔ Usage: `!sell @customer` (fulfill their paid tab) or "
                    "`!sell @customer <quantity> <item name>` (walk-up sale)."
                )
                return

            if qty <= 0:
                await ctx.send("⛔ Quantity must be greater than 0.")
                return

            item_name = item_name.strip()
            item = database.get_business_item(business["code"], item_name)

            if item is None:
                await ctx.send(f"⛔ **{item_name}** isn't on the menu here.")
                return

            ok, reason = database.adjust_business_item_stock(
                business["code"], item["item_name"], -qty
            )

            if not ok:
                if reason == "insufficient_stock":
                    await ctx.send(
                        f"⛔ Only {item['stock']} of **{item['item_name']}** left."
                    )
                else:
                    await ctx.send(f"⛔ Sale failed ({reason}).")
                return

            await ctx.send(
                f"✅ Walk-up sale: {customer.mention} takes **{qty} x "
                f"{item['item_name']}** from **{business['name']}**. Goods handed over."
            )
            return

        # --------------------------------------------------------
        # Fulfill an already-PAID register in full.
        # --------------------------------------------------------

        register = database.get_paid_register(business["code"], customer.id)

        if register is None:
            await ctx.send(
                f"⛔ {customer.mention} has no paid tab waiting to be fulfilled "
                f"here. If they haven't paid yet, they need to `!pay` or "
                f"`!transfer` the exact total first."
            )
            return

        lines = json.loads(register["items"])

        for line in lines:

            ok, reason = database.adjust_business_item_stock(
                business["code"], line["item_name"], -line["qty"]
            )

            if not ok:
                await ctx.send(
                    f"⛔ Can't fulfill — **{line['item_name']}** doesn't have "
                    f"{line['qty']} in stock anymore ({reason}). Sort out stock "
                    f"and try again; the paid tab is untouched."
                )
                return

        database.fulfill_register(register["register_id"])

        summary = "\n".join(
            f"• {line['qty']} x {line['item_name']}" for line in lines
        )

        embed = discord.Embed(
            title=f"✅ Order Fulfilled — {business['name']}",
            color=discord.Color.green()
        )
        embed.add_field(name="Customer", value=customer.mention, inline=False)
        embed.add_field(name="Items", value=summary, inline=False)
        embed.add_field(name="Total Paid", value=f"₦{register['total']:,}", inline=False)

        await ctx.send(embed=embed)

    # ============================================================
    # !ORDER — restocking at the depot
    # ============================================================

    @commands.command(name="order")
    @checks.require_location(DEPOT_CODE)
    async def order(
        self,
        ctx: commands.Context,
        business_code: str,
        qty: int,
        *,
        item_name: str
    ):

        business_code = business_code.lower().strip()
        business = database.get_business(business_code)

        if business is None:
            await ctx.send(f"⛔ No registered business with the code `{business_code}`.")
            return

        if str(ctx.author.id) != business["owner_id"]:
            await ctx.send(f"⛔ You don't own **{business['name']}**.")
            return

        if business["business_type"] not in ORDER_ELIGIBLE_TYPES:
            await ctx.send(
                f"⛔ Only {', '.join(ORDER_ELIGIBLE_TYPES)} owners restock at the "
                f"depot. **{business['name']}** is a {business['business_type']}."
            )
            return

        if qty <= 0:
            await ctx.send("⛔ Quantity must be greater than 0.")
            return

        item_name = item_name.strip()

        ok, reason = database.adjust_business_item_stock(business_code, item_name, qty)

        if not ok:
            await ctx.send(
                f"⛔ **{item_name}** isn't on **{business['name']}**'s menu yet — "
                f"add it first with `!add` before restocking it here."
                if reason == "no_such_item"
                else f"⛔ Restock failed ({reason})."
            )
            return

        item = database.get_business_item(business_code, item_name)

        embed = discord.Embed(
            title="🚚 Restocked",
            color=discord.Color.green()
        )
        embed.add_field(name="Business", value=business["name"], inline=False)
        embed.add_field(name="Item", value=item["item_name"], inline=True)
        embed.add_field(name="Added", value=str(qty), inline=True)
        embed.add_field(name="New Total Stock", value=str(item["stock"]), inline=True)

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(BusinessShopCog(bot))
