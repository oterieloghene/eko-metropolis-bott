"""
Driving School — an in-character, always-open help desk.

Location: "driving-school" (#driving-school), mainland zone, a
normal road node like any other (reachable with !drive, no role
requirement once you're there — see LOCATIONS in config.py).

The one command here, !learn, opens an interactive "book" of
tutorial chapters covering every transport system in the game
(driving, carpool, buses, taxis, and the full list of location
codes). Players page through it with buttons, or jump straight
to a chapter with the dropdown.

This cog only reads other systems' commands to describe them —
it doesn't touch database state or any other cog's logic.
"""

import discord
from discord.ext import commands

from checks import require_location
from config import LOCATIONS, ZONE_LABELS, TAXI_REGISTRATION_CODE

DRIVING_SCHOOL_CODE = "driving-school"


# ================================================================
# CHAPTER CONTENT
# ================================================================

def _routes_chapter_text() -> str:

    zones: dict[str, list[str]] = {}

    for code, loc in LOCATIONS.items():

        zones.setdefault(loc["zone"], []).append(
            f"`{code}` — {loc['name']}"
        )

    lines = []

    for zone in ("island", "mainland", "ghetto", "farmland", "overseas"):

        entries = zones.get(zone)

        if not entries:
            continue

        zone_label = ZONE_LABELS.get(zone, zone.title())

        lines.append(f"**{zone_label}**")
        lines.extend(entries)
        lines.append("")

    return (
        "These are the location codes used by `!drive`, `!route`, "
        "`!book`, and `!bus` — always the code in backticks, "
        "never the full name.\n\n" + "\n".join(lines)
    ).strip()


CHAPTERS: list[tuple[str, str]] = [
    (
        "1. Welcome",
        "Welcome to Eko Metropolis! A few basics before you hit "
        "the road:\n\n"
        "`!balance` — check your cash\n"
        "`!map` — see the full Èko Metropolis map, with where "
        "you are right now marked\n"
        "`!route <destination>` — preview the road distance to "
        "somewhere before you commit to driving there\n\n"
        "Use the ▶ button below to keep reading, or jump to a "
        "chapter with the dropdown."
    ),
    (
        "2. Getting a Vehicle",
        "You need your own vehicle before you can drive anywhere.\n\n"
        "`!cars` — see what the Vehicle Dealership has in stock\n"
        "`!buy <vehicle name>` — purchase one\n"
        "`!vehicle` — check your vehicle's fuel and condition\n"
        "`!refuel` — top up your tank\n"
        "`!fixcar` — repair condition damage at Automobile Repair"
    ),
    (
        "3. Driving",
        "`!drive <destination>` — the core travel command. Costs "
        "fuel, wears down condition, and may charge a toll "
        "crossing between zones.\n"
        "`!paytoll` — pay a toll gate you're stopped at.\n\n"
        "Watch your fuel and condition — running out mid-route "
        "isn't fun."
    ),
    (
        "4. Carpool",
        "Own a multi-seat vehicle? You can bring passengers along, "
        "each with their own destination.\n\n"
        "`!accept` — a passenger confirms a pickup you've queued "
        "for them\n"
        "`!decline` — a passenger turns it down\n"
        "`!cancelpickup` — driver cancels a queued pickup\n"
        "`!dropoffuser` — used internally as each passenger reaches "
        "their stop along your route"
    ),
    (
        "5. BRT Buses",
        "Public transport — cheaper than a taxi, no fuel or "
        "condition to worry about, first-come-first-served.\n\n"
        "`!brtcard buy` — get a BRT Card\n"
        "`!brtcard balance` — check its balance\n"
        "`!brtcard recharge <amount>` — top it up\n"
        "`!bus <route> <destination>` — board a bus on route B1, "
        "B2, or B3 heading your way\n"
        "`!busfleet` — see buses currently in service"
    ),
    (
        "6. Booking a Taxi",
        "A taxi is a private, on-demand, door-to-door ride — "
        "pricier than the bus, but it comes straight to you.\n\n"
        "`!book <standard|premium> <destination>` — request a ride. "
        "If every driver is busy, you're queued and auto-matched "
        "the moment one frees up.\n"
        "`!addrider <@user>` — bring up to 2 more people along, "
        "same destination, before the driver responds\n"
        "`!cancelride` — back out of a pending request, or leave "
        "the queue"
    ),
    (
        "7. Becoming a Taxi Driver",
        f"Head to **{LOCATIONS[TAXI_REGISTRATION_CODE]['name']}** "
        f"(`!drive {TAXI_REGISTRATION_CODE}`) to sign up.\n\n"
        "`!becometaxidriver <standard|premium>` — register; the "
        "company hands you a car on the spot, no purchase needed\n"
        "`!taxistart` / `!taxistop` — go online or offline. This "
        "sticks until you toggle it again — it does NOT reset "
        "after every trip.\n"
        "`!taxiaccept` / `!taxidecline` — respond to a ride ping\n"
        "`!taxipickup` — confirm pickup once you've physically "
        "arrived at the passenger's location\n\n"
        "The taxi company takes a cut of every fare — the car "
        "belongs to them, after all."
    ),
    (
        "8. Routes & Location Codes",
        _routes_chapter_text()
    ),
]


# ================================================================
# PAGINATED "BOOK" VIEW
# ================================================================

class DrivingSchoolBook(discord.ui.View):

    def __init__(self, author_id: int):

        super().__init__(timeout=300)

        self.author_id = author_id
        self.index = 0

        self._build_chapter_select()
        self._refresh_button_state()

    def _build_chapter_select(self) -> None:

        options = [
            discord.SelectOption(
                label=title,
                value=str(i),
                default=(i == self.index),
            )
            for i, (title, _body) in enumerate(CHAPTERS)
        ]

        self.chapter_select.options = options

    def _refresh_button_state(self) -> None:

        self.previous_button.disabled = (self.index == 0)
        self.next_button.disabled = (self.index == len(CHAPTERS) - 1)

        for option in self.chapter_select.options:
            option.default = (int(option.value) == self.index)

    def _embed(self) -> discord.Embed:

        title, body = CHAPTERS[self.index]

        embed = discord.Embed(
            title=f"📖 Driving School — {title}",
            description=body,
            color=discord.Color.green(),
        )

        embed.set_footer(
            text=f"Page {self.index + 1} of {len(CHAPTERS)}"
        )

        return embed

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:

        if interaction.user.id != self.author_id:

            await interaction.response.send_message(
                "This isn't your lesson — run `!learn` to open "
                "your own copy.",
                ephemeral=True,
            )

            return False

        return True

    async def on_timeout(self) -> None:

        for item in self.children:
            item.disabled = True

    @discord.ui.button(
        label="◀ Previous", style=discord.ButtonStyle.secondary, row=0
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        self.index = max(0, self.index - 1)
        self._refresh_button_state()

        await interaction.response.edit_message(
            embed=self._embed(), view=self
        )

    @discord.ui.button(
        label="Next ▶", style=discord.ButtonStyle.secondary, row=0
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        self.index = min(len(CHAPTERS) - 1, self.index + 1)
        self._refresh_button_state()

        await interaction.response.edit_message(
            embed=self._embed(), view=self
        )

    @discord.ui.select(
        placeholder="Jump to a chapter...", row=1
    )
    async def chapter_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ):

        self.index = int(select.values[0])
        self._refresh_button_state()

        await interaction.response.edit_message(
            embed=self._embed(), view=self
        )


# ================================================================
# COG
# ================================================================

class DrivingSchoolCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="learn")
    @require_location(DRIVING_SCHOOL_CODE)
    async def learn(self, ctx: commands.Context):

        view = DrivingSchoolBook(author_id=ctx.author.id)

        await ctx.send(embed=view._embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(DrivingSchoolCog(bot))
