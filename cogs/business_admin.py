"""
Phase 4 — Business Accounts (registration half)
=================================================

Builds on Phase 1's location registration and reuses its channel/
category helpers directly (see the import below) rather than
duplicating them.

Registers a business as BOTH a real drivable map location (a
`locations` row, same as !location-registration) AND the
business-specific bookkeeping in database.businesses: who owns it,
its business_type (which fixes its licensed inventory categories
in Phase 5), and a shared "owner-type" Discord role.

The financial half — opening the actual balance-holding account —
is a deliberately separate step: !create-business-account, in
cogs/banking.py (see that file for why it lives there instead of
here).

Commands:

    !business-registration <code> <owner> <business_type> <zone> <distance> <name...>
        Registers a new business. `code`/`zone`/`distance`/`name`
        mirror !location-registration exactly. `owner` is the
        member being registered as the business's owner. `type`
        is one of "mall" / "mamaput" / "club" / "gasstation" —
        fixed, per spec, to the categories each may sell in Phase
        5. Gated to the "Minister of Justice" role or admin — this
        is a formal government registration, not something bank
        staff or the owner themselves can self-serve.

        Creates (or reuses) a shared Discord role for that
        business_type (e.g. "mallowner") and grants it to the
        owner — this role is NOT unique per business; it just
        marks "an owner of this TYPE of business" for Phase 5's
        category licensing. Per-business ownership itself is
        tracked separately (database.businesses.owner_id), which
        is what gates business-specific commands like Phase 5's
        !add/!sell.

        Creates the business's channel under the "Business &
        Commerce" category, LOCKED — hidden from everyone except
        the owner — until the owner physically arrives there for
        the first time (see permissions.py's business-lock
        handling, which every arrival path already funnels
        through).
"""

import discord
from discord.ext import commands

import database
import permissions

from config import LOCATIONS

from cogs.location_admin import (
    VALID_ZONES,
    _slugify_channel_name,
    _get_or_create_category,
)


MINISTER_OF_JUSTICE_ROLE = "Minister of Justice"

BUSINESS_CATEGORY_NAME = "Business & Commerce"

# Locked-in category mapping (Phase 5) keys off of these exact
# role names — see the final spec's "@mallowner" / "@mamaput" /
# "@clubowner" / "@gasstation" bullets. Deliberately NOT unique
# per business — every owner of a given business_type shares the
# same role; per-business ownership lives in database.businesses.
BUSINESS_TYPE_ROLES = {
    "mall": "mallowner",
    "mamaput": "mamaput",
    "club": "clubowner",
    "gasstation": "gasstation",
}


def _is_minister_or_admin():
    async def predicate(ctx: commands.Context) -> bool:

        if ctx.author.guild_permissions.administrator:
            return True

        return discord.utils.get(
            ctx.author.roles,
            name=MINISTER_OF_JUSTICE_ROLE
        ) is not None

    return commands.check(predicate)


async def _get_or_create_role(
    guild: discord.Guild,
    role_name: str
) -> discord.Role:

    existing = discord.utils.get(
        guild.roles,
        name=role_name
    )

    if existing is not None:
        return existing

    return await guild.create_role(
        name=role_name,
        reason="Eko Bot: business_type owner role"
    )


class BusinessAdminCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ========================================================
    # !BUSINESS-REGISTRATION
    # ========================================================

    @commands.command(name="business-registration")
    @_is_minister_or_admin()
    async def business_registration(
        self,
        ctx: commands.Context,
        code: str,
        owner: discord.Member,
        business_type: str,
        zone: str,
        distance: float,
        *,
        name: str
    ):

        code = code.lower().strip()
        zone = zone.lower().strip()
        business_type = business_type.lower().strip()

        if code in LOCATIONS or database.get_location(code) is not None:
            await ctx.send(
                f"⛔ A location with the code `{code}` already exists."
            )
            return

        if database.get_business(code) is not None:
            await ctx.send(
                f"⛔ A business with the code `{code}` already exists."
            )
            return

        if owner.bot:
            await ctx.send("⛔ A bot can't own a business.")
            return

        if business_type not in BUSINESS_TYPE_ROLES:
            await ctx.send(
                f"⛔ Invalid business type `{business_type}`. Must be "
                f"one of: {', '.join(BUSINESS_TYPE_ROLES)}."
            )
            return

        if zone not in VALID_ZONES:
            await ctx.send(
                f"⛔ Invalid zone `{zone}`. Must be one of: "
                f"{', '.join(VALID_ZONES)}."
            )
            return

        if distance <= 0:
            await ctx.send("⛔ Distance must be greater than 0.")
            return

        owner_role_name = BUSINESS_TYPE_ROLES[business_type]
        owner_role = await _get_or_create_role(ctx.guild, owner_role_name)

        try:
            await owner.add_roles(
                owner_role,
                reason=f"Eko Bot: registered as owner of business '{code}'"
            )

        except discord.Forbidden:
            await ctx.send(
                f"⛔ I couldn't grant {owner.mention} the `{owner_role_name}` "
                f"role — check my role position/permissions."
            )
            return

        channel_name = _slugify_channel_name(code)

        discord_category = await _get_or_create_category(
            ctx.guild,
            BUSINESS_CATEGORY_NAME
        )

        channel = await ctx.guild.create_text_channel(
            channel_name,
            category=discord_category,
            reason=f"Eko Bot: business registered by {ctx.author}"
        )

        # ----------------------------------------------------
        # LOCK THE CHANNEL — hidden from everyone except the
        # owner until they physically arrive (see permissions.py's
        # _resolve_business_lock, which every arrival path already
        # funnels through via move_write_access()).
        # ----------------------------------------------------

        await channel.set_permissions(
            ctx.guild.default_role,
            view_channel=False,
            reason="Eko Bot: business locked until owner arrives"
        )

        await channel.set_permissions(
            owner,
            view_channel=True,
            reason="Eko Bot: business owner can see their own unopened business"
        )

        created_location = database.create_location(
            code=code,
            name=name,
            channel_name=channel.name,
            zone=zone,
            distance=distance,
            category="business",
            created_by=ctx.author.id
        )

        created_business = created_location and database.create_business(
            code=code,
            name=name,
            owner_id=owner.id,
            business_type=business_type,
            owner_role_name=owner_role_name,
            created_by=ctx.author.id
        )

        if not created_business:
            await ctx.send(
                "⛔ Business could not be registered — the code was "
                "taken between the check above and now. Try again."
            )
            return

        await permissions.ensure_bot_channel_permissions(ctx.guild)

        embed = discord.Embed(
            title="🏪 Business Registered",
            color=discord.Color.green()
        )
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        embed.add_field(name="Owner", value=owner.mention, inline=True)
        embed.add_field(name="Type", value=business_type, inline=True)
        embed.add_field(name="Zone", value=zone, inline=True)
        embed.add_field(name="Distance", value=str(distance), inline=True)
        embed.add_field(name="Owner Role", value=f"`{owner_role_name}`", inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(
            name="Status",
            value=(
                f"🔒 Locked — hidden until {owner.mention} physically "
                f"travels there for the first time."
            ),
            inline=False
        )

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(BusinessAdminCog(bot))
