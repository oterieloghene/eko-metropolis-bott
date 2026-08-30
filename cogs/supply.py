"""
Supplier / Depot — !assign-supplier, !supply, !list-mall,
!list-drink, !pending-orders, !approve-order, !reject-order
=============================================================

The other half of the depot cart flow started in
cogs/business_shop.py's !order/!review-order. This cog owns the
Supplier side: who can stock the depot, what's actually there, and
approving/rejecting a submitted cart.

    !assign-supplier @player / !remove-supplier @player
        Admin-only. Supplier is admin-assigned, never self-service —
        grants (or revokes) both the `players.is_supplier` flag AND
        the "Supplier" Discord role, which config.LOCATIONS["depot"]
        already restricts physical depot access to.

    !supply
        Supplier-only, must be physically AT the depot (same
        channel+database-location check as everything else
        location-gated). Category -> subcategory -> item dropdown
        pulled from the manufactured catalog (database.
        get_manufactured_goods(), so farm-stub/system entries never
        show up here — nothing to "supply" about produce that isn't
        manufactured), then a popup for quantity. Adds that qty to
        the shared depot_stock pool and debits Treasury the
        catalog price x qty.

    !list-mall / !list-drink
        Anyone, anywhere. Read-only depot stock listings — no manual
        adding. !list-mall groups food_drinks SNACKS + RAW and
        merchandise HYGIENE + GIFTS; !list-drink groups food_drinks
        ALCOHOL + SOFT DRINKS + WATER.

    !pending-orders
        Supplier-only. Every business cart currently awaiting
        approval.

    !approve-order <business_code> / !reject-order <business_code>
        Supplier-only. Approve debits the business account, credits
        Treasury, decrements depot stock, and posts a receipt to the
        #treasury channel (best-effort) plus the business's own
        receipt channel. Reject just clears the cart and notifies
        the owner — nothing moves.
"""

import json

import discord
from discord.ext import commands

import checks
import database

from cogs.business_admin import CATEGORY_LABELS, SUBCATEGORIES, SHOP_CATEGORIES, _get_or_create_role


SUPPLIER_ROLE = "Supplier"
DEPOT_CODE = "depot"
TREASURY_LOG_CHANNEL = "treasury"

# Subcategories shown under each depot list, keyed (category, subcategory).
MALL_GROUPS = (
    ("food_drinks", "SNACKS"),
    ("food_drinks", "RAW"),
    ("merchandise", "HYGIENE"),
    ("merchandise", "GIFTS"),
)

DRINK_GROUPS = (
    ("food_drinks", "ALCOHOL"),
    ("food_drinks", "SOFT DRINKS"),
    ("food_drinks", "WATER"),
)


def _is_admin():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)


def _is_supplier():
    async def predicate(ctx: commands.Context) -> bool:
        if not database.is_player_supplier(ctx.author.id):
            await ctx.send("⛔ Only a Supplier can do that.")
            return False
        return True
    return commands.check(predicate)


# ================================================================
# DEPOT LIST RENDERING (shared by !list-mall / !list-drink)
# ================================================================

def _render_depot_list(title: str, groups: tuple) -> discord.Embed:

    stock_by_name = {row["item_name"].lower(): row for row in database.get_depot_stock()}

    embed = discord.Embed(title=title, color=discord.Color.blurple())

    for category, subcategory in groups:

        rows = [
            row for row in database.get_manufactured_goods()
            if row["category"] == category and row["subcategory"] == subcategory
        ]

        lines = []
        for row in rows:
            stock = stock_by_name.get(row["item_name"].lower())
            qty = stock["qty"] if stock else 0
            if qty > 0:
                lines.append(f"{row['item_name']} x{qty} — ₦{row['price']:,}")

        embed.add_field(
            name=f"{CATEGORY_LABELS.get(category, category.title())} / {subcategory}",
            value="\n".join(lines) if lines else "Empty",
            inline=False,
        )

    return embed


# ================================================================
# !SUPPLY — category -> subcategory -> item -> qty popup
# ================================================================

class _SupplyQtyModal(discord.ui.Modal):

    def __init__(self, row):
        super().__init__(title=f"Supply — {row['item_name']}"[:45])
        self.row = row

        self.qty_input = discord.ui.TextInput(
            label=f"Quantity (₦{row['price']:,} each)",
            placeholder="10",
        )
        self.add_item(self.qty_input)

    async def on_submit(self, interaction: discord.Interaction):

        try:
            qty = int(self.qty_input.value.strip())
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

        cost = self.row["price"] * qty

        ok, reason = database.adjust_institution_balance("treasury", -cost)

        if not ok:
            await interaction.response.send_message(
                "⛔ Treasury can't cover that — insufficient funds."
                if reason == "insufficient_funds"
                else f"⛔ Treasury debit failed ({reason}).",
                ephemeral=True,
            )
            return

        database.add_depot_stock(
            self.row["category"], self.row["subcategory"], self.row["item_name"], qty
        )

        embed = discord.Embed(title="🚚 Depot supplied", color=discord.Color.green())
        embed.add_field(name="Item", value=self.row["item_name"], inline=True)
        embed.add_field(name="Qty added", value=str(qty), inline=True)
        embed.add_field(name="Treasury debited", value=f"₦{cost:,}", inline=True)

        await interaction.response.send_message(embed=embed)


class _SupplyItemSelect(discord.ui.Select):

    def __init__(self, rows: list):
        self.rows = rows

        options = [
            discord.SelectOption(
                label=f"{row['item_name']} (₦{row['price']:,})",
                value=row["item_name"],
                description=row["subcategory"],
            )
            for row in rows
        ][:25]

        super().__init__(placeholder="Choose an item to supply...", options=options)

    async def callback(self, interaction: discord.Interaction):

        row = next((r for r in self.rows if r["item_name"] == self.values[0]), None)

        if row is None:
            await interaction.response.send_message(
                "⛔ That item no longer exists in the catalog.", ephemeral=True
            )
            return

        await interaction.response.send_modal(_SupplyQtyModal(row))


class _SupplyItemView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=60)
        self.add_item(_SupplyItemSelect(rows))


class _SupplySubcategorySelect(discord.ui.Select):

    def __init__(self, category: str):
        self.category = category

        options = [
            discord.SelectOption(label=sub, value=sub)
            for sub in SUBCATEGORIES.get(category, ())
        ]

        super().__init__(placeholder="Choose a subcategory...", options=options)

    async def callback(self, interaction: discord.Interaction):

        rows = [
            row for row in database.get_manufactured_goods()
            if row["category"] == self.category and row["subcategory"] == self.values[0]
        ]

        if not rows:
            await interaction.response.send_message(
                "⛔ Nothing manufactured in that subcategory yet.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content=f"🚚 **{self.values[0]}** — pick an item to supply:",
            view=_SupplyItemView(rows),
        )


class _SupplySubcategoryView(discord.ui.View):
    def __init__(self, category):
        super().__init__(timeout=60)
        self.add_item(_SupplySubcategorySelect(category))


class _SupplyCategorySelect(discord.ui.Select):

    def __init__(self):
        options = [
            discord.SelectOption(
                label=CATEGORY_LABELS.get(category, category.title()),
                value=category,
            )
            for category in SHOP_CATEGORIES
        ]
        super().__init__(placeholder="Choose a category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=f"🚚 **{CATEGORY_LABELS.get(self.values[0], self.values[0])}** — pick a subcategory:",
            view=_SupplySubcategoryView(self.values[0]),
        )


class _SupplyCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(_SupplyCategorySelect())


# ================================================================
# COG
# ================================================================

class SupplyCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------
    # !ASSIGN-SUPPLIER / !REMOVE-SUPPLIER
    # ------------------------------------------------------------

    @commands.command(name="assign-supplier")
    @_is_admin()
    async def assign_supplier(self, ctx: commands.Context, member: discord.Member):

        database.set_player_supplier(member.id, True)

        role = await _get_or_create_role(ctx.guild, SUPPLIER_ROLE)

        try:
            await member.add_roles(role, reason="Eko Bot: assigned as Supplier")
        except discord.Forbidden:
            await ctx.send(
                f"⚠️ Flagged {member.mention} as a Supplier, but couldn't grant the "
                f"`{SUPPLIER_ROLE}` role — check my role position/permissions."
            )
            return

        await ctx.send(f"✅ {member.mention} is now a Supplier.")

    @commands.command(name="remove-supplier")
    @_is_admin()
    async def remove_supplier(self, ctx: commands.Context, member: discord.Member):

        database.set_player_supplier(member.id, False)

        role = discord.utils.get(ctx.guild.roles, name=SUPPLIER_ROLE)

        if role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason="Eko Bot: removed as Supplier")
            except discord.Forbidden:
                pass

        await ctx.send(f"✅ {member.mention} is no longer a Supplier.")

    # ------------------------------------------------------------
    # !SUPPLY
    # ------------------------------------------------------------

    @commands.command(name="supply")
    @_is_supplier()
    @checks.require_location(DEPOT_CODE)
    async def supply(self, ctx: commands.Context):

        rows = database.get_manufactured_goods()

        if not rows:
            await ctx.send("⛔ Nothing has been manufactured yet — see `!manufacture`.")
            return

        await ctx.send(
            "🚚 Supply the depot — pick a category:",
            view=_SupplyCategoryView(),
        )

    # ------------------------------------------------------------
    # !LIST-MALL / !LIST-DRINK
    # ------------------------------------------------------------

    @commands.command(name="list-mall")
    async def list_mall(self, ctx: commands.Context):
        await ctx.send(embed=_render_depot_list("🏬 Depot — Mall Stock", MALL_GROUPS))

    @commands.command(name="list-drink")
    async def list_drink(self, ctx: commands.Context):
        await ctx.send(embed=_render_depot_list("🥤 Depot — Drink Stock", DRINK_GROUPS))

    # ------------------------------------------------------------
    # !PENDING-ORDERS
    # ------------------------------------------------------------

    @commands.command(name="pending-orders")
    @_is_supplier()
    async def pending_orders(self, ctx: commands.Context):

        rows = database.get_pending_depot_orders()

        if not rows:
            await ctx.send("📭 No orders awaiting approval.")
            return

        embed = discord.Embed(title="📦 Pending Depot Orders", color=discord.Color.gold())

        for row in rows:

            business = database.get_business(row["business_code"])
            lines = json.loads(row["items"])

            embed.add_field(
                name=f"{business['name'] if business else row['business_code']} ({row['business_code']})",
                value=(
                    "\n".join(f"{l['qty']} x {l['item_name']}" for l in lines)
                    + f"\n**Total: ₦{row['total']:,}**"
                ),
                inline=False,
            )

        embed.set_footer(text="!approve-order <code> or !reject-order <code>")

        await ctx.send(embed=embed)

    # ------------------------------------------------------------
    # !APPROVE-ORDER / !REJECT-ORDER
    # ------------------------------------------------------------

    async def _post_receipt(self, ctx, business, row, action: str):

        lines = json.loads(row["items"])
        item_summary = "\n".join(f"{l['qty']} x {l['item_name']} (₦{l['price']:,} each)" for l in lines)

        embed = discord.Embed(
            title=f"🧾 Depot Order {action.title()} — {business['name']}",
            color=discord.Color.green() if action == "approved" else discord.Color.red(),
        )
        embed.add_field(name="Items", value=item_summary, inline=False)
        embed.add_field(name="Total", value=f"₦{row['total']:,}", inline=True)
        embed.add_field(name="By", value=ctx.author.mention, inline=True)

        # Best-effort: post to #treasury if it exists, and to the
        # business's own receipt channel if it has an open account.
        treasury_channel = discord.utils.get(ctx.guild.text_channels, name=TREASURY_LOG_CHANNEL)
        if treasury_channel is not None:
            try:
                await treasury_channel.send(embed=embed)
            except discord.Forbidden:
                pass

        account = database.get_business_account(business["code"])
        if account is not None:
            biz_channel = discord.utils.get(ctx.guild.text_channels, name=account["receipt_channel_name"])
            if biz_channel is not None and biz_channel.id != getattr(treasury_channel, "id", None):
                try:
                    await biz_channel.send(embed=embed)
                except discord.Forbidden:
                    pass

    @commands.command(name="approve-order")
    @_is_supplier()
    async def approve_order(self, ctx: commands.Context, business_code: str):

        business_code = business_code.lower().strip()
        business = database.get_business(business_code)

        if business is None:
            await ctx.send(f"⛔ No registered business with the code `{business_code}`.")
            return

        ok, reason, row = database.approve_depot_order(business_code, ctx.author.id)

        if not ok:
            messages = {
                "no_pending_order": f"⛔ **{business['name']}** has no order awaiting approval.",
                "insufficient_stock": "⛔ Depot stock has changed since submission — not enough left to fulfill this order.",
                "no_business_account": f"⛔ **{business['name']}** doesn't have an open business account.",
                "insufficient_funds": f"⛔ **{business['name']}**'s account can't cover the total.",
            }
            await ctx.send(messages.get(reason, f"⛔ Approval failed ({reason})."))
            return

        await ctx.send(f"✅ Approved **{business['name']}**'s order — ₦{row['total']:,} debited, Treasury credited.")

        await self._post_receipt(ctx, business, row, "approved")

    @commands.command(name="reject-order")
    @_is_supplier()
    async def reject_order(self, ctx: commands.Context, business_code: str):

        business_code = business_code.lower().strip()
        business = database.get_business(business_code)

        if business is None:
            await ctx.send(f"⛔ No registered business with the code `{business_code}`.")
            return

        ok, reason, row = database.reject_depot_order(business_code)

        if not ok:
            await ctx.send(f"⛔ **{business['name']}** has no order awaiting approval.")
            return

        await ctx.send(f"🗑️ Rejected and cleared **{business['name']}**'s order.")

        owner = ctx.guild.get_member(int(business["owner_id"]))
        if owner is not None:
            try:
                await owner.send(
                    f"⛔ Your depot order for **{business['name']}** was rejected by "
                    f"{ctx.author.mention} — the cart has been cleared. Build a new "
                    f"one with `!order`."
                )
            except discord.Forbidden:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(SupplyCog(bot))
