"""
Google Map App — !map.

Replaces the old !location command. Purely presentational: reads
LOCATIONS/ZONE_LABELS the same way driving_school.py's tutorial
chapter already does, plus the player's OWN database location (a
read, never a write) so it can be pinned in the listing with a
📍. No other database state is touched.

Display shape: 44+ locations is too many for one flat embed
field, so entries are grouped by zone (Island/Mainland/Ghetto/
Farmland/Overseas — MAP_ZONE_ORDER), each shown as
`Name — \\`code\\``, one embed field per zone.

The actual map art (MAP_IMAGE_FILENAME, in cogs/assets like
phone_home.png) is attached alongside the embed so this reads as
a real graphical map, not just a text listing.

Reachable two ways:
    - Typed: !map
    - Phone: MainMenuView's "Map" button (cogs/phone.py), which
      invokes this command through the same _invoke() helper as
      every other phone screen — since that patches ctx.send to
      an ephemeral interaction.followup.send, the map opens
      exactly as privately as everything else on the phone, with
      no extra work needed here.
"""

import os

import discord
from discord.ext import commands

import database
from config import LOCATIONS, MAP_COLOR_RGB, MAP_IMAGE_FILENAME, MAP_ZONE_ORDER, ZONE_LABELS


MAP_IMAGE_PATH = os.path.join(
    os.path.dirname(__file__),
    "assets",
    MAP_IMAGE_FILENAME
)

MAP_COLOR = discord.Color.from_rgb(*MAP_COLOR_RGB)

_PIN = "\U0001f4cd"
_BULLET = "\u2022"


def _zone_groups() -> dict[str, list[tuple[str, str]]]:
    """{zone: [(code, name), ...]} — grouping only, no formatting."""

    zones: dict[str, list[tuple[str, str]]] = {}

    for code, loc in LOCATIONS.items():
        zones.setdefault(loc["zone"], []).append((code, loc["name"]))

    for entries in zones.values():
        entries.sort(key=lambda pair: pair[1])

    return zones


def build_map_embed(current_code: str | None = None) -> discord.Embed:
    """
    Build the zone-grouped listing embed. current_code (the
    player's own DB location, if known) gets a 📍 next to its
    entry instead of the plain bullet.
    """

    embed = discord.Embed(
        title="\U0001f5fa\ufe0f Èko Metropolis — Map",
        description=(
            "Every location code used by `!drive`, `!route`, "
            "`!book`, and `!bus` — always the code in backticks, "
            "never the full name."
        ),
        color=MAP_COLOR
    )

    embed.set_image(url=f"attachment://{MAP_IMAGE_FILENAME}")

    zones = _zone_groups()

    for zone in MAP_ZONE_ORDER:

        entries = zones.get(zone)

        if not entries:
            continue

        lines = [
            f"{_PIN if code == current_code else _BULLET} "
            f"{name} — `{code}`"
            for code, name in entries
        ]

        embed.add_field(
            name=ZONE_LABELS.get(zone, zone.title()),
            value="\n".join(lines),
            inline=False
        )

    if current_code is not None and current_code in LOCATIONS:
        embed.set_footer(text=f"📍 marks where you are right now.")

    return embed


class MapCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="map")
    async def map(self, ctx: commands.Context):
        """Show the Èko Metropolis map, with your current location marked."""

        player = database.get_or_create_player(ctx.author.id)
        current_code = player["location"] if player else None

        embed = build_map_embed(current_code)

        try:
            await ctx.send(
                embed=embed,
                file=discord.File(MAP_IMAGE_PATH, filename=MAP_IMAGE_FILENAME)
            )

        except discord.HTTPException:

            # Falls back to the text-only listing if the image
            # can't be attached for some reason (missing file,
            # size limit, etc.) rather than failing silently.
            embed.set_image(url=None)
            await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MapCog(bot))
