"""
Manufacturing / Importation Catalog — !manufacture, !import
=============================================================

Admin-only. This is the master item catalog the rest of the goods
economy is meant to draw from eventually — !supply (not built yet)
is how a supplier will actually buy from it and stock a depot; this
cog only owns the catalog itself: defining what an item IS (its
category/subcategory, what stat(s) it affects and by how much, any
companion-item requirement, how many uses one unit is good for) and
its import price.

    !manufacture
        Category -> subcategory dropdowns, then a popup form for
        ONE item: Item Name / Stat effects / Price / Requires item
        (optional) / Uses per unit. Submitting adds it to the
        catalog — or, if an item with that name already exists
        (case-insensitive), OVERWRITES every field on it in place.
        That's intentional: !manufacture doubles as "!manufacture
        again with new numbers" for editing an existing entry, not
        just first-time creation.

    !import
        Lists the full catalog, grouped by category then
        subcategory (same layout as !inv/!menu). Also offers a
        category -> item dropdown to adjust just the price on an
        existing entry without re-typing everything else.

Stat effects are entered as free text like "thirst:10, happiness:5"
— comma-separated stat:percent pairs, matched positionally to
whichever combination of config.PLAYER_STAT_NAMES the admin types.
A stat with no percent (e.g. just "hunger" for a raw ingredient
that needs a future "cook" mechanic before it does anything) is
stored with percent 0 — it's still on record as the item's intended
stat, just inactive until that mechanic exists. Leaving the whole
field blank means the item has no stat effect on its own at all
(e.g. Toothbrush, which only matters as a companion item).
"""

import json

import discord
from discord.ext import commands

import database

from config import PLAYER_STAT_NAMES
from cogs.business_admin import SHOP_CATEGORIES, SUBCATEGORIES, CATEGORY_LABELS


def _is_admin():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)


# ================================================================
# STAT EFFECT PARSING
# ================================================================

def _parse_stat_effects(raw: str) -> tuple[list, "str | None"]:
    """
    Parse "thirst:10, happiness:5" (or "hunger" alone, percent
    defaults to 0) into [{"stat": "thirst", "percent": 10}, ...].

    Returns (effects, error) — error is None on success, or a
    user-facing message on the first bad entry.
    """

    raw = (raw or "").strip()

    if not raw:
        return [], None

    effects = []

    for chunk in raw.split(","):

        chunk = chunk.strip()

        if not chunk:
            continue

        if ":" in chunk:
            stat_name, _, percent_str = chunk.partition(":")
            stat_name = stat_name.strip().lower()
            percent_str = percent_str.strip()

            try:
                percent = float(percent_str) if percent_str else 0.0
            except ValueError:
                return [], f"⛔ `{chunk}` isn't a valid `stat:percent` entry."

        else:
            stat_name = chunk.lower()
            percent = 0.0

        if stat_name not in PLAYER_STAT_NAMES:
            return [], (
                f"⛔ `{stat_name}` isn't a stat. Must be one of: "
                f"{', '.join(PLAYER_STAT_NAMES)}."
            )

        effects.append({"stat": stat_name, "percent": percent})

    return effects, None


def _format_stat_effects(effects: list) -> str:

    if not effects:
        return "_none_"

    return ", ".join(
        f"{e['stat']} +{e['percent']:g}%" if e["percent"] else f"{e['stat']} (inactive)"
        for e in effects
    )


# ================================================================
# !MANUFACTURE — CATEGORY -> SUBCATEGORY -> ITEM FORM (POPUP)
# ================================================================

class _ManufactureItemModal(discord.ui.Modal):

    def __init__(self, category: str, subcategory: str):
        super().__init__(title=f"Manufacture — {subcategory}"[:45])
        self.category = category
        self.subcategory = subcategory

        self.name_input = discord.ui.TextInput(
            label="Item name",
            placeholder="e.g. Coke",
            max_length=100,
        )
        self.stats_input = discord.ui.TextInput(
            label="Stat effects (stat:percent, ...)",
            placeholder="thirst:10, happiness:5 — leave blank if none",
            required=False,
            style=discord.TextStyle.paragraph,
        )
        self.price_input = discord.ui.TextInput(
            label="Price",
            placeholder="500",
        )
        self.requires_input = discord.ui.TextInput(
            label="Requires item (optional)",
            placeholder="e.g. Toothbrush",
            required=False,
        )
        self.uses_input = discord.ui.TextInput(
            label="Uses per unit",
            placeholder="1",
            default="1",
        )

        self.add_item(self.name_input)
        self.add_item(self.stats_input)
        self.add_item(self.price_input)
        self.add_item(self.requires_input)
        self.add_item(self.uses_input)

    async def on_submit(self, interaction: discord.Interaction):

        item_name = self.name_input.value.strip()

        if not item_name:
            await interaction.response.send_message(
                "⛔ An item name is required.", ephemeral=True
            )
            return

        effects, error = _parse_stat_effects(self.stats_input.value)

        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        try:
            price = int(self.price_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "⛔ Price must be a whole number.", ephemeral=True
            )
            return

        if price <= 0:
            await interaction.response.send_message(
                "⛔ Price must be greater than 0.", ephemeral=True
            )
            return

        try:
            uses_per_unit = int(self.uses_input.value.strip() or "1")
        except ValueError:
            await interaction.response.send_message(
                "⛔ Uses per unit must be a whole number.", ephemeral=True
            )
            return

        if uses_per_unit <= 0:
            await interaction.response.send_message(
                "⛔ Uses per unit must be greater than 0.", ephemeral=True
            )
            return

        requires_item = self.requires_input.value.strip() or None

        warning = ""
        if requires_item and database.get_manufactured_good(requires_item) is None:
            warning = (
                f"\n⚠️ Note: **{requires_item}** isn't in the catalog yet — "
                f"the link is saved, but manufacture it too so it actually works."
            )

        row = database.upsert_manufactured_good(
            category=self.category,
            subcategory=self.subcategory,
            item_name=item_name,
            stat_effects=json.dumps(effects),
            requires_item=requires_item,
            uses_per_unit=uses_per_unit,
            price=price,
            created_by=interaction.user.id,
        )

        embed = discord.Embed(
            title="🏭 Manufactured",
            color=discord.Color.green(),
        )
        embed.add_field(name="Item", value=row["item_name"], inline=True)
        embed.add_field(
            name="Category", value=f"{CATEGORY_LABELS.get(self.category, self.category)} / {self.subcategory}",
            inline=True,
        )
        embed.add_field(name="Price", value=f"₦{row['price']:,}", inline=True)
        embed.add_field(name="Stat effects", value=_format_stat_effects(effects), inline=False)
        embed.add_field(name="Requires item", value=requires_item or "_none_", inline=True)
        embed.add_field(name="Uses per unit", value=str(uses_per_unit), inline=True)
        embed.set_footer(text="Added to the permanent importation catalog — see !import.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

        if warning:
            await interaction.followup.send(warning, ephemeral=True)


class _SubcategorySelect(discord.ui.Select):

    def __init__(self, category: str):
        self.category = category

        options = [
            discord.SelectOption(label=sub, value=sub)
            for sub in SUBCATEGORIES.get(category, ())
        ]

        super().__init__(placeholder="Choose a subcategory...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _ManufactureItemModal(self.category, self.values[0])
        )


class _SubcategoryView(discord.ui.View):
    def __init__(self, category):
        super().__init__(timeout=60)
        self.add_item(_SubcategorySelect(category))


class _ManufactureCategorySelect(discord.ui.Select):

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
            content=f"🏭 **{CATEGORY_LABELS.get(self.values[0], self.values[0])}** — pick a subcategory:",
            view=_SubcategoryView(self.values[0]),
        )


class _ManufactureCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(_ManufactureCategorySelect())


# ================================================================
# !IMPORT — CATALOG LIST + PRICE ADJUST
# ================================================================

class _PriceModal(discord.ui.Modal):

    def __init__(self, item_name: str, current_price: int):
        super().__init__(title=f"Adjust price — {item_name}"[:45])
        self.item_name = item_name

        self.price_input = discord.ui.TextInput(
            label=f"New price (currently ₦{current_price:,})",
            placeholder=str(current_price),
        )
        self.add_item(self.price_input)

    async def on_submit(self, interaction: discord.Interaction):

        try:
            price = int(self.price_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "⛔ Price must be a whole number.", ephemeral=True
            )
            return

        if price <= 0:
            await interaction.response.send_message(
                "⛔ Price must be greater than 0.", ephemeral=True
            )
            return

        ok = database.set_manufactured_good_price(self.item_name, price)

        if not ok:
            await interaction.response.send_message(
                "⛔ That item no longer exists in the catalog.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ **{self.item_name}** is now ₦{price:,}.", ephemeral=True
        )


class _ImportItemSelect(discord.ui.Select):

    def __init__(self, rows: list):
        self.rows = rows

        options = [
            discord.SelectOption(
                label=f"{row['item_name']} (₦{row['price']:,})",
                value=row["item_name"],
                description=row["subcategory"],
            )
            for row in rows
        ]

        super().__init__(placeholder="Choose an item to adjust...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):

        row = next((r for r in self.rows if r["item_name"] == self.values[0]), None)

        if row is None:
            await interaction.response.send_message(
                "⛔ That item no longer exists.", ephemeral=True
            )
            return

        await interaction.response.send_modal(_PriceModal(row["item_name"], row["price"]))


class _ImportItemView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=60)
        self.add_item(_ImportItemSelect(rows))


class _ImportCategorySelect(discord.ui.Select):

    def __init__(self, by_category: dict):
        self.by_category = by_category

        options = [
            discord.SelectOption(
                label=CATEGORY_LABELS.get(category, category.title()),
                value=category,
                description=f"{len(rows)} item(s)",
            )
            for category, rows in by_category.items()
        ]

        super().__init__(placeholder="Choose a category to adjust...", options=options)

    async def callback(self, interaction: discord.Interaction):

        rows = self.by_category.get(self.values[0], [])

        await interaction.response.edit_message(
            content=f"💲 **{CATEGORY_LABELS.get(self.values[0], self.values[0])}** — pick an item:",
            view=_ImportItemView(rows),
        )


class _ImportCategoryView(discord.ui.View):
    def __init__(self, by_category):
        super().__init__(timeout=60)
        self.add_item(_ImportCategorySelect(by_category))


# ================================================================
# !FARM-STUB — placeholder farm/reared produce, ahead of
# !cultivate/!rear actually shipping. Always food_drinks/RAW, never
# shown in !manufacture/!import (source="farm") — exists only so
# !recipe can reference farm-sourced ingredients (e.g. Pepper,
# Tomato) today.
# ================================================================

class _FarmStubModal(discord.ui.Modal):

    def __init__(self):
        super().__init__(title="Farm/Rear stub — RAW produce")

        self.name_input = discord.ui.TextInput(
            label="Item name",
            placeholder="e.g. Pepper",
            max_length=100,
        )
        self.stats_input = discord.ui.TextInput(
            label="Stat effects (stat:percent, ...)",
            placeholder="leave blank if none",
            required=False,
            style=discord.TextStyle.paragraph,
        )
        self.uses_input = discord.ui.TextInput(
            label="Uses per unit",
            placeholder="1",
            default="1",
        )

        self.add_item(self.name_input)
        self.add_item(self.stats_input)
        self.add_item(self.uses_input)

    async def on_submit(self, interaction: discord.Interaction):

        item_name = self.name_input.value.strip()

        if not item_name:
            await interaction.response.send_message(
                "⛔ An item name is required.", ephemeral=True
            )
            return

        effects, error = _parse_stat_effects(self.stats_input.value)

        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        try:
            uses_per_unit = int(self.uses_input.value.strip() or "1")
        except ValueError:
            await interaction.response.send_message(
                "⛔ Uses per unit must be a whole number.", ephemeral=True
            )
            return

        if uses_per_unit <= 0:
            await interaction.response.send_message(
                "⛔ Uses per unit must be greater than 0.", ephemeral=True
            )
            return

        row = database.upsert_manufactured_good(
            category="food_drinks",
            subcategory="RAW",
            item_name=item_name,
            stat_effects=json.dumps(effects),
            requires_item=None,
            uses_per_unit=uses_per_unit,
            price=0,
            created_by=interaction.user.id,
            source="farm",
        )

        embed = discord.Embed(
            title="🌾 Farm/Rear stub added",
            color=discord.Color.green(),
        )
        embed.add_field(name="Item", value=row["item_name"], inline=True)
        embed.add_field(name="Stat effects", value=_format_stat_effects(effects), inline=False)
        embed.set_footer(
            text="Hidden from !manufacture/!import — available as a !recipe ingredient and in inventory."
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ================================================================
# !RECIPE — output COOKED item -> multi-select ingredients
# (raw manufactured + farm/rear stub items)
# ================================================================

class _RecipeIngredientSelect(discord.ui.Select):

    def __init__(self, output_item: str, rows: list):
        self.output_item = output_item
        self.rows = rows

        options = [
            discord.SelectOption(
                label=row["item_name"],
                value=row["item_name"],
                description="Farm/Rear" if row["source"] == "farm" else "Manufactured",
            )
            for row in rows
        ][:25]

        super().__init__(
            placeholder="Choose ingredients (1 qty each)...",
            options=options,
            min_values=1,
            max_values=len(options),
        )

    async def callback(self, interaction: discord.Interaction):

        ok, reason, row = database.create_or_update_recipe(
            output_item=self.output_item,
            ingredients=self.values,
            created_by=interaction.user.id,
        )

        if not ok:
            await interaction.response.send_message(
                f"⛔ That exact ingredient set is already used by the recipe "
                f"for **{row['output_item']}** — recipes can't share an "
                f"ingredient set, or !cook wouldn't know which one to make.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📖 Recipe saved",
            color=discord.Color.green(),
        )
        embed.add_field(name="Cooks", value=self.output_item, inline=False)
        embed.add_field(name="Ingredients", value=", ".join(self.values), inline=False)
        embed.set_footer(text="Players make this with !cook <ingredients> (any order).")

        await interaction.response.send_message(embed=embed, ephemeral=True)


class _RecipeIngredientView(discord.ui.View):
    def __init__(self, output_item, rows):
        super().__init__(timeout=120)
        self.add_item(_RecipeIngredientSelect(output_item, rows))


class _RecipeOutputSelect(discord.ui.Select):

    def __init__(self, cooked_rows: list):
        self.cooked_rows = cooked_rows

        options = [
            discord.SelectOption(label=row["item_name"], value=row["item_name"])
            for row in cooked_rows
        ][:25]

        super().__init__(placeholder="Choose the COOKED item this recipe makes...", options=options)

    async def callback(self, interaction: discord.Interaction):

        raw_rows = [
            r for r in database.get_manufactured_goods()
            if r["category"] == "food_drinks" and r["subcategory"] == "RAW"
        ] + list(database.get_farm_goods())

        # de-dupe by item_name in case of any overlap
        seen = set()
        ingredient_rows = []
        for r in raw_rows:
            key = r["item_name"].lower()
            if key not in seen:
                seen.add(key)
                ingredient_rows.append(r)

        if not ingredient_rows:
            await interaction.response.send_message(
                "⛔ No RAW ingredients exist yet — manufacture some RAW items "
                "or add farm/rear stubs with `!farm-stub` first.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content=f"📖 **{self.values[0]}** — pick its ingredients:",
            view=_RecipeIngredientView(self.values[0], ingredient_rows),
        )


class _RecipeOutputView(discord.ui.View):
    def __init__(self, cooked_rows):
        super().__init__(timeout=60)
        self.add_item(_RecipeOutputSelect(cooked_rows))


class _FarmStubButton(discord.ui.Button):

    def __init__(self):
        super().__init__(label="Add farm/rear stub", style=discord.ButtonStyle.green, emoji="🌾")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_FarmStubModal())


class _FarmStubView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(_FarmStubButton())


# ================================================================
# COG
# ================================================================

class ManufacturingCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="manufacture")
    @_is_admin()
    async def manufacture(self, ctx: commands.Context):
        await ctx.send(
            "🏭 Manufacture an item — pick a category:",
            view=_ManufactureCategoryView(),
        )

    @commands.command(name="import")
    @_is_admin()
    async def import_(self, ctx: commands.Context):

        rows = database.get_manufactured_goods()

        if not rows:
            await ctx.send("📭 Nothing has been manufactured yet — see `!manufacture`.")
            return

        by_category: dict[str, list] = {}
        for row in rows:
            by_category.setdefault(row["category"], []).append(row)

        embed = discord.Embed(
            title="📋 Importation Catalog",
            color=discord.Color.blurple(),
        )

        for category, cat_rows in by_category.items():

            by_subcategory: dict[str, list] = {}
            for row in cat_rows:
                by_subcategory.setdefault(row["subcategory"], []).append(row)

            lines = []
            for subcategory, sub_rows in by_subcategory.items():
                lines.append(f"**{subcategory}**")
                for row in sub_rows:
                    lines.append(f"{row['item_name']} — ₦{row['price']:,}")

            embed.add_field(
                name=CATEGORY_LABELS.get(category, category.title()),
                value="\n".join(lines),
                inline=False,
            )

        await ctx.send(embed=embed)

        await ctx.send(
            "💲 Want to adjust a price? Pick a category:",
            view=_ImportCategoryView(by_category),
        )

    @commands.command(name="farm-stub")
    @_is_admin()
    async def farm_stub(self, ctx: commands.Context):
        """Placeholder farm/reared RAW produce, ahead of !cultivate/
        !rear shipping — hidden from !manufacture/!import, usable
        as a !recipe ingredient."""
        await ctx.send(
            "🌾 Stub a farm/rear RAW item into the catalog:",
            view=_FarmStubView(),
        )

    @commands.command(name="recipe")
    @_is_admin()
    async def recipe(self, ctx: commands.Context):

        cooked_rows = [
            r for r in database.get_manufactured_goods()
            if r["category"] == "food_drinks" and r["subcategory"] == "COOKED"
        ]

        if not cooked_rows:
            await ctx.send(
                "⛔ No COOKED items exist yet — `!manufacture` one first, "
                "then `!recipe` it."
            )
            return

        await ctx.send(
            "📖 Define a recipe — pick the COOKED item it makes:",
            view=_RecipeOutputView(cooked_rows),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ManufacturingCog(bot))
