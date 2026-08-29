"""
Location & Sub-Location Administration
=======================================

Phase 1 of the banking/business overhaul: lets new drivable
locations (starting with registered businesses) and the private
"rooms" attached to a location (front-desk, ATM, bank-manager,
etc.) get created at runtime, without ever touching config.py or
redeploying.

Commands (all admin-only — ctx.author.guild_permissions.administrator):

    !location-registration <code> <zone> <distance> <category> <name...>
        Register a brand-new drivable location. `code` is the
        unique slug used everywhere else (channel names, routing,
        !drive). `zone` is one of island/mainland/ghetto — this is
        gameplay travel data, unrelated to Discord layout. `distance`
        is how far the location sits from that zone's hub, same
        unit config.py's RAW_DISTANCES already uses. `category`
        picks which Discord category the channel is created under
        — see VALID_CATEGORIES/CATEGORY_DISCORD_NAMES below for the
        full list (e.g. "cat-bank" -> CENTRAL BANK OF ÈKO, "cat-
        business" ->
        BUSINESS & COMMERCE); "other" falls back to the generic
        "Locations" category. Creates the Discord channel and the
        database row.

    !create-sub-location <parent_code> <code> <public|role> [role_name] <name...>
        Attach a sub-location ("room") to an existing parent
        location (either a config.py LOCATIONS code or one
        registered above). access is "public" (opens for anyone
        who arrives at the parent) or "role" (only role_name
        holders get it — role_name is required in that case).
        role_name accepts multiple roles as a comma-separated list
        (e.g. "cbe-chairman,cbe-deputy") — anyone holding ANY one
        of the listed roles gets access. Quote the whole list if
        any role name contains a space (e.g. "cbe-chairman,Bank
        Staff"). Creates the Discord channel and the database row.

    !remove-location <code>
    !remove-sub-location <code>
        Delete a registered location/sub-location. Requires the
        account tied to it to already be closed via
        !close-account (Phase 2/4) — enforced once that command
        exists; for now this just refuses if the code doesn't
        exist. Does NOT delete the Discord channel itself (left
        for the admin to archive/delete manually, since that's
        destructive and not easily undone).

Both !location-registration and !remove-location/!remove-sub-location
write to the `locations` / `sub_locations` tables, which are
deliberately excluded from reset_database() — see database.py.
"""

import discord
from discord.ext import commands

import database
import permissions

from config import LOCATIONS


VALID_ZONES = (
    "island",
    "mainland",
    "ghetto",
)

VALID_CATEGORIES = (
    "cat-arrival-terminal",
    "cat-immigration",
    "cat-lifestyle",
    "cat-island",
    "cat-villa",
    "cat-cityhall",
    "cat-legal",
    "cat-bank",
    "cat-medical",
    "cat-police",
    "cat-property",
    "cat-university",
    "cat-mainland",
    "cat-business",
    "cat-ghetto",
    "cat-announcement",
    "other",
)

# Maps each !location-registration `category` value to the exact
# Discord channel category it should be created under. Every value
# is prefixed "cat-" so it can never collide with a LOCATIONS code
# or a VALID_ZONES value (e.g. "bank"/"police"/"island"/"mainland"/
# "ghetto" already mean something different elsewhere). "other" is
# the one exception — it intentionally falls back to
# DEFAULT_LOCATION_CATEGORY_NAME below rather than appearing here,
# for locations that don't fit any of the server's dedicated
# categories.
CATEGORY_DISCORD_NAMES = {
    "cat-arrival-terminal": "ARRIVAL TERMINAL",
    "cat-immigration": "ÈKO IMMIGRATION OFFICE",
    "cat-lifestyle": "ÈKO LIFESTYLE",
    "cat-island": "ISLAND",
    "cat-villa": "PRESIDENTIAL VILLA",
    "cat-cityhall": "CITY HALL",
    "cat-legal": "LEGAL COUNSEL",
    "cat-bank": "CENTRAL BANK OF ÈKO",
    "cat-medical": "ÈKO MEDICAL SERVICE",
    "cat-police": "ÈKO POLICE DEPARTMENT",
    "cat-property": "PROPERTY AND DEVELOPMENT DEPARTMENT",
    "cat-university": "ÈKO METROPOLIS UNIVERSITY",
    "cat-mainland": "MAINLAND",
    "cat-business": "BUSINESS & COMMERCE",
    "cat-ghetto": "GHETTO",
    "cat-announcement": "ANNOUNCEMENT",
}

# Discord channel category new dynamic locations are created
# under by default. Business registrations (Phase 4) will likely
# want their own category ("Business & Commerce") — this stays
# configurable per-call rather than hardcoded so that future
# phases can pass a different one.
DEFAULT_LOCATION_CATEGORY_NAME = "Locations"


def _is_admin():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.guild_permissions.administrator

    return commands.check(predicate)


def _slugify_channel_name(code: str) -> str:
    """Discord channel names are lowercase-hyphen; codes already are."""
    return code.lower().strip().replace(" ", "-")


async def _get_or_create_category(
    guild: discord.Guild,
    category_name: str
) -> discord.CategoryChannel:

    existing = discord.utils.get(
        guild.categories,
        name=category_name
    )

    if existing is not None:
        return existing

    return await guild.create_category(
        category_name,
        reason="Eko Bot: auto-created for dynamic locations"
    )


def _parent_channel_name(parent_code: str) -> str | None:
    """
    Resolve a parent location code to its channel name, checking
    the static config.py LOCATIONS dict first, then the dynamic
    `locations` table. Returns None if the parent can't be found
    in either (shouldn't happen, since callers already verified
    the parent exists before getting here).
    """

    static_location = LOCATIONS.get(parent_code)

    if static_location is not None:
        return static_location["channel"]

    dynamic_location = database.get_location(parent_code)

    if dynamic_location is not None:
        return dynamic_location["channel_name"]

    return None


async def _category_for_sub_location(
    guild: discord.Guild,
    parent_code: str
) -> discord.CategoryChannel:
    """
    Sub-locations should sit in the SAME category as their parent's
    channel (e.g. front-desk lands right next to banking-hall), not
    in a generic catch-all. Falls back to the DEFAULT_LOCATION_CATEGORY_NAME
    category only if the parent's channel can't be found (e.g. it
    was deleted or renamed outside the bot).
    """

    parent_channel_name = _parent_channel_name(parent_code)

    if parent_channel_name is not None:

        parent_channel = discord.utils.get(
            guild.text_channels,
            name=parent_channel_name
        )

        if parent_channel is not None and parent_channel.category is not None:
            return parent_channel.category

    return await _get_or_create_category(
        guild,
        DEFAULT_LOCATION_CATEGORY_NAME
    )


class LocationAdminCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ========================================================
    # !LOCATION-REGISTRATION
    # ========================================================

    @commands.command(name="location-registration")
    @_is_admin()
    async def location_registration(
        self,
        ctx: commands.Context,
        code: str,
        zone: str,
        distance: float,
        category: str,
        *,
        name: str
    ):

        code = code.lower().strip()
        zone = zone.lower().strip()
        category = category.lower().strip()

        if code in LOCATIONS or database.get_location(code) is not None:
            await ctx.send(
                f"\u26d4 A location with the code `{code}` already exists."
            )
            return

        if zone not in VALID_ZONES:
            await ctx.send(
                f"\u26d4 Invalid zone `{zone}`. Must be one of: "
                f"{', '.join(VALID_ZONES)}."
            )
            return

        if category not in VALID_CATEGORIES:
            await ctx.send(
                f"\u26d4 Invalid category `{category}`. Must be one of: "
                f"{', '.join(VALID_CATEGORIES)}."
            )
            return

        if distance <= 0:
            await ctx.send("\u26d4 Distance must be greater than 0.")
            return

        channel_name = _slugify_channel_name(code)

        discord_category_name = CATEGORY_DISCORD_NAMES.get(
            category,
            DEFAULT_LOCATION_CATEGORY_NAME
        )

        discord_category = await _get_or_create_category(
            ctx.guild,
            discord_category_name
        )

        channel = await ctx.guild.create_text_channel(
            channel_name,
            category=discord_category,
            reason=f"Eko Bot: location registered by {ctx.author}"
        )

        created = database.create_location(
            code=code,
            name=name,
            channel_name=channel.name,
            zone=zone,
            distance=distance,
            category=category,
            created_by=ctx.author.id
        )

        if not created:
            await ctx.send(
                "\u26d4 Location could not be created — the code was "
                "taken between the check above and now. Try again."
            )
            return

        await permissions.ensure_bot_channel_permissions(ctx.guild)

        embed = discord.Embed(
            title="\U0001f4cd Location Registered",
            color=discord.Color.green()
        )
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        embed.add_field(name="Zone", value=zone, inline=True)
        embed.add_field(name="Distance", value=str(distance), inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)

        await ctx.send(embed=embed)

    # ========================================================
    # !CREATE-SUB-LOCATION
    # ========================================================

    @commands.command(name="create-sub-location")
    @_is_admin()
    async def create_sub_location(
        self,
        ctx: commands.Context,
        parent_code: str,
        code: str,
        access: str,
        role_name: str = None,
        *,
        name: str = None
    ):

        parent_code = parent_code.lower().strip()
        code = code.lower().strip()
        access = access.lower().strip()

        parent_exists = (
            parent_code in LOCATIONS
            or database.get_location(parent_code) is not None
        )

        if not parent_exists:
            await ctx.send(
                f"\u26d4 No location with the code `{parent_code}` exists. "
                f"Register it first with !location-registration."
            )
            return

        if code in LOCATIONS or database.get_sub_location(code) is not None:
            await ctx.send(
                f"\u26d4 A sub-location with the code `{code}` already exists."
            )
            return

        if access not in ("public", "role"):
            await ctx.send(
                "\u26d4 Access must be either `public` or `role`."
            )
            return

        if access == "role":

            if not role_name:
                await ctx.send(
                    "\u26d4 A role-gated sub-location needs a role name: "
                    "!create-sub-location <parent> <code> role <role_name> <display name...>"
                )
                return

            requested_role_names = [
                r.strip()
                for r in role_name.split(",")
                if r.strip()
            ]

            discord_roles = []
            missing_role_names = []

            for requested_name in requested_role_names:

                discord_role = discord.utils.get(
                    ctx.guild.roles,
                    name=requested_name
                )

                if discord_role is None:
                    missing_role_names.append(requested_name)
                else:
                    discord_roles.append(discord_role)

            if missing_role_names:
                await ctx.send(
                    f"\u26d4 No role(s) named "
                    f"{', '.join(f'`{n}`' for n in missing_role_names)} "
                    f"exist on this server."
                )
                return

            # Canonicalize to the roles' actual on-server casing,
            # and store back into role_name as the comma-joined
            # list — this is what gets saved to the database and
            # what permissions.py splits on later to check "does
            # this member hold ANY of these roles".
            role_name = ",".join(role.name for role in discord_roles)

        else:
            # access == "public" — role_name (if anything was
            # typed there) is actually the start of the display
            # name, since it's an optional positional argument.
            if role_name:
                name = f"{role_name} {name}" if name else role_name
            role_name = None

        if not name:
            await ctx.send("\u26d4 A display name is required.")
            return

        channel_name = _slugify_channel_name(code)

        discord_category = await _category_for_sub_location(
            ctx.guild,
            parent_code
        )

        channel = await ctx.guild.create_text_channel(
            channel_name,
            category=discord_category,
            reason=f"Eko Bot: sub-location registered by {ctx.author}"
        )

        # Role-gated rooms shouldn't be visible to @everyone by
        # default — lock view access down to each listed role
        # (and admins, who always see everything) at creation time.
        if access == "role":

            await channel.set_permissions(
                ctx.guild.default_role,
                view_channel=False,
                reason="Eko Bot: role-gated sub-location"
            )

            for discord_role in discord_roles:

                await channel.set_permissions(
                    discord_role,
                    view_channel=True,
                    send_messages=False,
                    reason="Eko Bot: role-gated sub-location — visible, "
                           "write access still granted only on arrival"
                )

        created = database.create_sub_location(
            code=code,
            parent_code=parent_code,
            name=name,
            channel_name=channel.name,
            access=access,
            role_name=role_name,
            created_by=ctx.author.id
        )

        if not created:
            await ctx.send(
                "\u26d4 Sub-location could not be created — the code was "
                "taken between the check above and now. Try again."
            )
            return

        embed = discord.Embed(
            title="\U0001f6aa Sub-Location Created",
            color=discord.Color.green()
        )
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        embed.add_field(name="Parent", value=f"`{parent_code}`", inline=True)
        embed.add_field(
            name="Access",
            value=(
                "Public (opens on arrival for everyone)"
                if access == "public"
                else f"Role-gated (`{role_name.replace(',', '`, `')}`)"
            ),
            inline=False
        )
        embed.add_field(name="Channel", value=channel.mention, inline=True)

        await ctx.send(embed=embed)

    # ========================================================
    # !REMOVE-LOCATION
    # ========================================================

    @commands.command(name="remove-location")
    @_is_admin()
    async def remove_location(
        self,
        ctx: commands.Context,
        code: str
    ):

        code = code.lower().strip()

        if code in LOCATIONS:
            await ctx.send(
                "\u26d4 That location is hand-authored in config.py, "
                "not dynamically registered, and can't be removed with "
                "this command."
            )
            return

        location = database.get_location(code)

        if location is None:
            await ctx.send(f"\u26d4 No registered location `{code}` exists.")
            return

        # ----------------------------------------------------
        # Phase 2/4 will wire this up to require !close-account
        # to have been run first (business/org account attached
        # to this location must be settled/closed before the
        # location itself can be deleted). For now, there is no
        # account system yet to check against, so this proceeds
        # directly — this check gets tightened once !close-account
        # exists.
        # ----------------------------------------------------

        remaining_subs = database.get_sub_locations_for_parent(code)

        if remaining_subs:
            names = ", ".join(f"`{s['code']}`" for s in remaining_subs)
            await ctx.send(
                f"\u26d4 This location still has sub-locations attached: "
                f"{names}. Remove those first with !remove-sub-location."
            )
            return

        database.delete_location(code)

        await ctx.send(
            f"\u2705 Location `{code}` removed from the database. "
            f"The Discord channel #{location['channel_name']} was left "
            f"in place — archive or delete it manually if needed."
        )

    # ========================================================
    # !REMOVE-SUB-LOCATION
    # ========================================================

    @commands.command(name="remove-sub-location")
    @_is_admin()
    async def remove_sub_location(
        self,
        ctx: commands.Context,
        code: str
    ):

        code = code.lower().strip()

        sub = database.get_sub_location(code)

        if sub is None:
            await ctx.send(f"\u26d4 No sub-location `{code}` exists.")
            return

        database.delete_sub_location(code)

        await ctx.send(
            f"\u2705 Sub-location `{code}` removed from the database. "
            f"The Discord channel #{sub['channel_name']} was left in "
            f"place — archive or delete it manually if needed."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LocationAdminCog(bot))
