"""
Consumption Commands — !cook, !brush, !bath, !eat, !drink
============================================================

Owns the "actually use what's in your inventory" layer that sits on
top of cogs/manufacturing.py's catalog (item definitions: stat
effects, uses_per_unit) and database.py's `inventory`/`recipes`
tables.

    !cook <ingredient, ingredient, ...>
        NOT a dropdown — typed comma-separated args (order doesn't
        matter, case-insensitive), e.g. "!cook tomato paste, pepper".
        Ingredients can be manufactured RAW items or !farm-stub
        produce; either way they must already be in YOUR inventory.
        An exact-set match (no duplicates, no extras) against a
        !recipe-defined ingredient set produces that recipe's COOKED
        item. Anything else — a typo you don't own, fine — but a
        non-matching combination of things you DO own (duplicates,
        extra ingredients, or just no such recipe) produces a
        "Concoction" instead. Either way the ingredients are
        consumed. Cooking itself only ever produces an item; it
        doesn't touch stats — !eat does that.

    !brush / !bath
        NOT dropdowns — auto-use whatever's already in your
        inventory. !brush needs a Toothbrush AND Toothpaste; !bath
        needs Soap. Each required item is spent via per-unit use
        tracking (a tube of toothpaste good for N brushes before the
        unit itself is used up), and any stat effects defined on
        those items (via !manufacture) apply immediately.

    !eat / !drink
        Dropdowns. !eat lists your COOKED + SNACKS items and lets
        you multi-select several at once (effects stack). !drink
        lists your ALCOHOL/WATER/SOFT DRINKS items, single-select
        only. Both spend a use the same way !brush/!bath do, and
        apply whatever stat effects the item was manufactured with.

An item with no matching manufactured_goods/farm-stub catalog entry
(e.g. something a business owner stocked by hand via !add without
ever running it through !manufacture) is still consumable — it just
has no stat effect and a uses_per_unit of 1, since there's nowhere
to look either up.
"""

import json

import discord
from discord.ext import commands

import database

from config import STAT_DISPLAY


# ================================================================
# SHARED HELPERS
# ================================================================

def _item_definition(item_name: str):
    """The manufactured_goods/farm-stub/system row for this item
    name, or None if it was never run through !manufacture/
    !farm-stub (e.g. hand-stocked by a business owner)."""
    return database.get_manufactured_good(item_name)


def _stat_effects_of(item_name: str) -> list:
    row = _item_definition(item_name)
    if row is None:
        return []
    try:
        return json.loads(row["stat_effects"] or "[]")
    except (TypeError, ValueError):
        return []


def _uses_per_unit_of(item_name: str) -> int:
    row = _item_definition(item_name)
    return int(row["uses_per_unit"]) if row else 1


def _format_totals(totals: dict) -> str:
    if not totals:
        return ""
    parts = []
    for stat, percent in totals.items():
        display = STAT_DISPLAY.get(stat, {"emoji": "", "label": stat.title()})
        sign = "+" if percent >= 0 else ""
        parts.append(f"{display['emoji']} {display['label']} {sign}{percent:g}%")
    return "\n" + " · ".join(parts)


async def _consume_and_apply(user_id: int, item_names: list[str]) -> dict:
    """Spend one use of each item in `item_names` (per-unit use
    tracking) and apply the sum of their stat effects in a single
    adjust_stats call. Returns the per-stat totals applied."""

    totals: dict = {}

    for name in item_names:
        database.consume_inventory_use(user_id, name, _uses_per_unit_of(name))

        for effect in _stat_effects_of(name):
            percent = effect.get("percent") or 0
            if percent:
                totals[effect["stat"]] = totals.get(effect["stat"], 0) + percent

    if totals:
        database.adjust_stats(user_id, **totals)

    return totals


# "Concoction" — the !cook fallback for a non-matching ingredient
# combination. Registered lazily (source="system", so it never shows
# in !manufacture/!import) the first time it's actually needed.
CONCOCTION_ITEM_NAME = "Concoction"


def _ensure_concoction_registered(created_by: int) -> None:
    if database.get_manufactured_good(CONCOCTION_ITEM_NAME) is not None:
        return
    database.upsert_manufactured_good(
        category="food_drinks",
        subcategory="COOKED",
        item_name=CONCOCTION_ITEM_NAME,
        stat_effects=json.dumps([
            {"stat": "hunger", "percent": 5},
            {"stat": "health", "percent": -5},
        ]),
        requires_item=None,
        uses_per_unit=1,
        price=0,
        created_by=created_by,
        source="system",
    )


# ================================================================
# !EAT — dropdown, multi-select (COOKED + SNACKS)
# ================================================================

class _EatSelect(discord.ui.Select):

    def __init__(self, user_id: int, rows: list):
        self.user_id = user_id
        self.rows = rows

        options = [
            discord.SelectOption(
                label=f"{row['item_name']} (have {row['qty']})",
                value=row["item_name"],
                description=row["subcategory"],
            )
            for row in rows
        ][:25]

        super().__init__(
            placeholder="Choose what to eat (pick one or more)...",
            options=options,
            min_values=1,
            max_values=len(options),
        )

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This isn't your `!eat` menu.", ephemeral=True
            )
            return

        totals = await _consume_and_apply(self.user_id, self.values)

        await interaction.response.send_message(
            f"🍽️ You ate **{', '.join(self.values)}**.{_format_totals(totals)}",
            ephemeral=True,
        )


class _EatView(discord.ui.View):
    def __init__(self, user_id, rows):
        super().__init__(timeout=60)
        self.add_item(_EatSelect(user_id, rows))


# ================================================================
# !DRINK — dropdown, single-select (ALCOHOL/WATER/SOFT DRINKS)
# ================================================================

class _DrinkSelect(discord.ui.Select):

    def __init__(self, user_id: int, rows: list):
        self.user_id = user_id
        self.rows = rows

        options = [
            discord.SelectOption(
                label=f"{row['item_name']} (have {row['qty']})",
                value=row["item_name"],
                description=row["subcategory"],
            )
            for row in rows
        ][:25]

        super().__init__(placeholder="Choose what to drink...", options=options)

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This isn't your `!drink` menu.", ephemeral=True
            )
            return

        totals = await _consume_and_apply(self.user_id, [self.values[0]])

        await interaction.response.send_message(
            f"🥤 You drank **{self.values[0]}**.{_format_totals(totals)}",
            ephemeral=True,
        )


class _DrinkView(discord.ui.View):
    def __init__(self, user_id, rows):
        super().__init__(timeout=60)
        self.add_item(_DrinkSelect(user_id, rows))


# ================================================================
# COG
# ================================================================

_EAT_SUBCATEGORIES = ("COOKED", "SNACKS")
_DRINK_SUBCATEGORIES = ("ALCOHOL", "SOFT DRINKS", "WATER")


class ConsumeCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------
    # !COOK
    # ------------------------------------------------------------

    @commands.command(name="cook")
    async def cook(self, ctx: commands.Context, *, ingredients_text: str = None):

        if not ingredients_text or not ingredients_text.strip():
            await ctx.send(
                "Usage: `!cook <ingredient>, <ingredient>, ...` — e.g. "
                "`!cook tomato paste, pepper`. Order doesn't matter."
            )
            return

        typed = [chunk.strip() for chunk in ingredients_text.split(",") if chunk.strip()]

        if not typed:
            await ctx.send("⛔ Name at least one ingredient.")
            return

        missing = []
        for name in typed:
            row = database.get_inventory_item(ctx.author.id, name)
            if row is None or row["qty"] <= 0:
                missing.append(name)

        if missing:
            await ctx.send(f"⛔ You don't have: {', '.join(missing)}.")
            return

        has_duplicates = len({n.lower() for n in typed}) != len(typed)

        recipe = None if has_duplicates else database.get_recipe_by_ingredients(typed)

        # Consume the ingredients (whole units — raw materials, not
        # a durable item's "uses") regardless of match/no-match.
        for name in typed:
            database.remove_inventory_item(ctx.author.id, name, 1)

        if recipe is not None:
            output_item = recipe["output_item"]
        else:
            _ensure_concoction_registered(ctx.author.id)
            output_item = CONCOCTION_ITEM_NAME

        out_row = database.get_manufactured_good(output_item)
        category = out_row["category"] if out_row else "food_drinks"
        subcategory = out_row["subcategory"] if out_row else "COOKED"

        database.add_inventory_item(ctx.author.id, category, subcategory, output_item, 1)

        if recipe is not None:
            await ctx.send(f"🍲 You cooked up a **{output_item}**!")
        else:
            await ctx.send(
                f"🍲 That combination didn't match any recipe — you ended up "
                f"with a **{output_item}** instead."
            )

    # ------------------------------------------------------------
    # !BRUSH
    # ------------------------------------------------------------

    @commands.command(name="brush")
    async def brush(self, ctx: commands.Context):

        toothbrush = database.get_inventory_item(ctx.author.id, "Toothbrush")
        toothpaste = database.get_inventory_item(ctx.author.id, "Toothpaste")

        missing = []
        if toothbrush is None or toothbrush["qty"] <= 0:
            missing.append("a Toothbrush")
        if toothpaste is None or toothpaste["qty"] <= 0:
            missing.append("Toothpaste")

        if missing:
            await ctx.send(f"⛔ You need {' and '.join(missing)} to brush.")
            return

        totals = await _consume_and_apply(ctx.author.id, ["Toothbrush", "Toothpaste"])

        await ctx.send(f"🪥 You brushed your teeth.{_format_totals(totals)}")

    # ------------------------------------------------------------
    # !BATH
    # ------------------------------------------------------------

    @commands.command(name="bath")
    async def bath(self, ctx: commands.Context):

        soap = database.get_inventory_item(ctx.author.id, "Soap")

        if soap is None or soap["qty"] <= 0:
            await ctx.send("⛔ You need Soap to bathe.")
            return

        totals = await _consume_and_apply(ctx.author.id, ["Soap"])

        await ctx.send(f"🛁 You took a bath.{_format_totals(totals)}")

    # ------------------------------------------------------------
    # !EAT
    # ------------------------------------------------------------

    @commands.command(name="eat")
    async def eat(self, ctx: commands.Context):

        rows = [
            row for row in database.get_inventory(ctx.author.id)
            if row["category"] == "food_drinks" and row["subcategory"] in _EAT_SUBCATEGORIES
        ]

        if not rows:
            await ctx.send("⛔ You don't have any cooked food or snacks to eat.")
            return

        await ctx.send(
            "🍽️ Pick what to eat (you can select more than one):",
            view=_EatView(ctx.author.id, rows),
        )

    # ------------------------------------------------------------
    # !DRINK
    # ------------------------------------------------------------

    @commands.command(name="drink")
    async def drink(self, ctx: commands.Context):

        rows = [
            row for row in database.get_inventory(ctx.author.id)
            if row["category"] == "food_drinks" and row["subcategory"] in _DRINK_SUBCATEGORIES
        ]

        if not rows:
            await ctx.send("⛔ You don't have anything to drink.")
            return

        await ctx.send(
            "🥤 Pick what to drink:",
            view=_DrinkView(ctx.author.id, rows),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ConsumeCog(bot))
