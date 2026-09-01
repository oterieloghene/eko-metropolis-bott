"""
Housing — Chief Housing Officer assignments across mainland/island
estates, the Presidential Villa, and the ghetto.

Four estate "shapes" (config.HOUSING_ESTATE_SHAPES), each creating a
different set of Discord threads:

    standard    ikeja / yaba / surulere / lekki / ikoyi / eko-atlantic
                -> private Kitchen + Bathroom + Bedroom, one resident.

    villa       mayor-villa / deputy-villa
                -> private Kitchen + Bathroom + Bedroom + Office
                   (config.HOUSING_VILLA_OFFICE_LABEL), one resident.

    guesthouse  guesthouse1 / guesthouse2
                -> private "Guest Room" per resident, but Kitchen and
                   Bathroom are SHARED across BOTH guesthouse codes
                   (one cluster, config.HOUSING_GUESTHOUSE_CLUSTER_KEY)
                   — created lazily on first assignment into either.

    ghetto      makoko / ajegunle / tenement
                -> no bedroom at all. Kitchen + Bathroom shared
                   per-location among everyone assigned there. This
                   shared access is resident-only — there is no
                   invite mechanism for ghetto housing at all (see
                   !invite/!assign-visitor below).

Thread names always stay exactly "Kitchen" / "Bathroom" / "Bedroom"
(or the villa's office label / "Guest Room") — the bot never relies
on the name to tell houses apart, only on the `housing` /
`housing_shared_threads` DB rows (thread_id -> resident_id, same
pattern as cogs/hotel.py's get_hotel_room_by_thread). A pinned intro
message in each thread is the only thing that names the owner, so a
human glancing at the thread list can still tell them apart.

PERMISSIONS — !assign-house / !evict-resident / !assign-visitor are
NOT uniformly CHO-only. See config.py's HOUSING_ADMIN_ONLY_ESTATES /
HOUSING_MAYOR_ASSIGN_ESTATES / HOUSING_MAYOR_VISITOR_ESTATES:

                        !assign-house/     !assign-visitor
                        !evict-resident
    standard estates    CHO                 CHO
    mayor-villa/         admin               Mayor of Eko
      deputy-villa
    guesthouse1/2        Mayor of Eko        Mayor of Eko
    ghetto                CHO                 n/a — refused, no
                                               invite mechanism exists

mayor-villa/deputy-villa are the Mayor/Deputy Mayor's own residence,
so assigning/evicting them is admin-only — not even the Mayor of Eko
role can do it. guesthouse1/guesthouse2 residency (an overnight Guest
Room stay) is a Mayor of Eko call, distinct from a short !invite
visit which only requires the visitor role.

Commands:

    !assign-house @player <estate>
        Permission per the table above, at rental-desk (Property
        Development Department). Refuses if the estate has no
        configured capacity yet (see !set-housing-capacity), is
        already full, or the player already has a house anywhere.
        Grants the estate's resident role, creates/joins the
        threads above, and writes the `housing` row.

    !evict-resident @player
        Same permission tier as !assign-house for whichever estate
        the player currently lives in, at rental-desk. Deletes the
        resident's private thread(s), removes them from any shared
        cluster thread (deleting the shared threads too only if
        they were the last resident tied to that cluster), revokes
        the resident role, and frees the house slot.

    !set-housing-capacity <estate> <count>
        Admin-only (ctx.author.guild_permissions.administrator,
        same convention as cogs/location_admin.py). Sets/changes how
        many houses that estate allows. mayor-villa/deputy-villa
        default to 1 (config.HOUSING_DEFAULT_CAPACITY) until an
        admin overrides it here; every other estate has no capacity,
        and !assign-house refuses, until set here.

    !assign-visitor @player <estate>
        Permission per the table above, at rental-desk. Grants that
        estate's existing zone/estate visitor role
        (config.HOUSING_VISITOR_ROLE) — a prerequisite for being
        !invite-d into any room there. Refused outright for ghetto
        estates — there is no visitor role and no invite mechanism
        for ghetto housing.

    !invite @player <room>
        Any resident, inside one of their own housing threads —
        including a villa's Office. `room` is
        kitchen/bathroom/bedroom/office, whichever this thread
        actually is. Refuses if the target doesn't hold the
        estate's visitor role (see !assign-visitor above), or if the
        resident's estate is a ghetto shape (no invite mechanism
        exists there at all, regardless of room). DMs the target an
        Accept/Decline button (HOUSING_INVITE_RESPONSE_TIMEOUT_SECONDS
        to respond). On Accept they're added to the thread and a
        HOUSING_VISIT_TIMEOUT_SECONDS clock starts — the scan loop
        auto-kicks them if it runs out. No exclusivity: multiple
        visitors can be in the same room at once, and a resident can
        have visitors in different rooms at once.

    !leave-room
        Visitor-only, typed inside the visited thread. Leaves early,
        skipping the auto-kick clock. No role changes either way —
        !invite/kick never touch the visitor role granted by
        !assign-visitor.

A background task (HOUSING_SCAN_INTERVAL_SECONDS, mirrors
cogs/hotel.py's scan_room_service) re-locks/unlocks every tracked
thread each cycle based on live presence: a thread is unlocked
(read-write) whenever ANY of its current "watchers" — the owning
resident(s), plus any accepted-and-not-yet-kicked visitor — are
physically at that thread's estate right now, and locked (read-only)
otherwise. The same loop sweeps housing_visitors for anyone past
HOUSING_VISIT_TIMEOUT_SECONDS and kicks them, posting
"@visitor was kicked out of @owner's residence." to the estate's
parent channel.
"""

from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import checks
import database

from config import (
    LOCATIONS,
    HOUSING_ESTATE_SHAPES,
    HOUSING_GUESTHOUSE_CLUSTER_KEY,
    HOUSING_GUESTHOUSE_CODES,
    HOUSING_VILLA_OFFICE_LABEL,
    HOUSING_RESIDENT_ROLE,
    HOUSING_VISITOR_ROLE,
    HOUSING_CHO_ROLE,
    HOUSING_MAYOR_ROLE,
    HOUSING_ADMIN_ONLY_ESTATES,
    HOUSING_MAYOR_ASSIGN_ESTATES,
    HOUSING_MAYOR_VISITOR_ESTATES,
    HOUSING_DEFAULT_CAPACITY,
    HOUSING_VISIT_TIMEOUT_SECONDS,
    HOUSING_SCAN_INTERVAL_SECONDS,
    HOUSING_INVITE_RESPONSE_TIMEOUT_SECONDS,
)

PRIVATE_THREAD_COLUMNS = {
    "kitchen": "kitchen_thread_id",
    "bathroom": "bathroom_thread_id",
    "bedroom": "bedroom_thread_id",
    "office": "office_thread_id",
}


# ================================================================
# HELPERS
# ================================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _has_role(member: discord.Member, role_name: str) -> bool:
    return discord.utils.get(member.roles, name=role_name) is not None


def _loc_name(estate: str) -> str:
    loc = LOCATIONS.get(estate)
    return loc["name"] if loc else estate.title()


def _rooms_for_shape(shape: str) -> list[str]:
    if shape == "standard":
        return ["kitchen", "bathroom", "bedroom"]
    if shape == "villa":
        return ["kitchen", "bathroom", "bedroom", "office"]
    if shape == "guesthouse":
        return ["bedroom"]  # kitchen/bathroom are the shared cluster, not private
    if shape == "ghetto":
        return []  # nothing private at all
    return []


def _shared_rooms_for_shape(shape: str) -> list[str]:
    if shape in ("guesthouse", "ghetto"):
        return ["kitchen", "bathroom"]
    return []


def _room_label(estate: str, shape: str, room: str) -> str:
    if room == "office":
        return HOUSING_VILLA_OFFICE_LABEL.get(estate, "Office")
    if shape == "guesthouse" and room == "bedroom":
        return "Guest Room"
    return room.title()


def _cluster_key_for(estate: str) -> str:
    if estate in HOUSING_GUESTHOUSE_CODES:
        return HOUSING_GUESTHOUSE_CLUSTER_KEY
    return estate  # ghetto: each location is its own cluster


def _cluster_estates_for(estate: str) -> tuple[str, ...]:
    if estate in HOUSING_GUESTHOUSE_CODES:
        return HOUSING_GUESTHOUSE_CODES
    return (estate,)


def _assign_permission(author: discord.Member, estate: str) -> tuple[bool, str]:
    """
    Which role/permission !assign-house and !evict-resident require
    for this estate, per the table in the module docstring. Returns
    (allowed, human-readable requirement) so callers can report a
    specific "you need X" message either way.
    """
    if estate in HOUSING_ADMIN_ONLY_ESTATES:
        return author.guild_permissions.administrator, "administrator permissions"
    if estate in HOUSING_MAYOR_ASSIGN_ESTATES:
        return _has_role(author, HOUSING_MAYOR_ROLE), f"the **{HOUSING_MAYOR_ROLE}** role"
    return _has_role(author, HOUSING_CHO_ROLE), f"the **{HOUSING_CHO_ROLE}** role"


def _visitor_permission(author: discord.Member, estate: str) -> tuple[bool, str]:
    """Which role !assign-visitor requires for this estate."""
    if estate in HOUSING_MAYOR_VISITOR_ESTATES:
        return _has_role(author, HOUSING_MAYOR_ROLE), f"the **{HOUSING_MAYOR_ROLE}** role"
    return _has_role(author, HOUSING_CHO_ROLE), f"the **{HOUSING_CHO_ROLE}** role"


async def _get_thread(guild: discord.Guild, thread_id: int) -> discord.Thread | None:
    thread = guild.get_thread(thread_id)
    if thread is not None:
        return thread
    try:
        channel = await guild.fetch_channel(thread_id)
        return channel if isinstance(channel, discord.Thread) else None
    except discord.HTTPException:
        return None


async def _post_and_pin_owner_note(thread: discord.Thread, owner: discord.Member, label: str) -> None:
    try:
        msg = await thread.send(f"\U0001f3e0 This is **{owner.display_name}**'s {label}.")
        await msg.pin()
    except discord.HTTPException:
        pass


# ================================================================
# ASSIGNMENT / EVICTION (shared logic)
# ================================================================

async def assign_house(
    guild: discord.Guild,
    target: discord.Member,
    estate: str,
) -> str:
    shape = HOUSING_ESTATE_SHAPES.get(estate)
    if shape is None:
        return f"\u26d4 `{estate}` is not a housing-eligible estate."

    if database.get_housing(target.id) is not None:
        return f"\u26d4 {target.mention} already has a house. Evict them first with `!evict-resident`."

    capacity = database.get_housing_capacity(estate)
    if capacity is None:
        capacity = HOUSING_DEFAULT_CAPACITY.get(estate)
    if capacity is None:
        return (
            f"\u26d4 No housing capacity has been set for {_loc_name(estate)} yet "
            f"\u2014 an admin needs to run `!set-housing-capacity {estate} <count>` first."
        )

    taken_numbers = database.housing_numbers_in_use(estate)
    if len(taken_numbers) >= capacity:
        return f"\u26d4 {_loc_name(estate)} is full ({len(taken_numbers)}/{capacity} houses occupied)."

    house_number = 1
    while house_number in taken_numbers:
        house_number += 1

    parent_channel = discord.utils.get(guild.text_channels, name=LOCATIONS[estate]["channel"])
    if parent_channel is None:
        return f"\u26d4 Couldn't find the Discord channel for {_loc_name(estate)}."

    perms = parent_channel.permissions_for(guild.me)
    required_perms = {
        "View Channel": perms.view_channel,
        "Create Private Threads": perms.create_private_threads,
        "Send Messages in Threads": perms.send_messages_in_threads,
        "Manage Threads": perms.manage_threads,
    }
    missing_perms = [name for name, has_it in required_perms.items() if not has_it]
    if missing_perms:
        return (
            f"\u26d4 The bot is missing permission(s) in {parent_channel.mention}: "
            f"**{', '.join(missing_perms)}**. This is what Discord itself reports for "
            f"the bot's *effective* permissions there (role + category + channel overwrites "
            f"all combined) \u2014 fix whichever overwrite is still denying it, then try again."
        )

    # Grant the resident role BEFORE creating/joining any threads — the role is what
    # gives the target read_messages access to the parent channel at all (see the
    # channel's @everyone deny overwrite); adding them to a thread under a channel
    # they can't yet see gets rejected by Discord with Missing Access.
    role_name = HOUSING_RESIDENT_ROLE.get(estate)
    role = discord.utils.get(guild.roles, name=role_name) if role_name else None
    role_was_granted = False
    if role is not None:
        try:
            await target.add_roles(role, reason="Housing assignment")
            role_was_granted = True
        except discord.HTTPException:
            pass

    created_thread_ids: dict[str, int] = {}
    last_step = "starting"

    try:
        for room in _rooms_for_shape(shape):
            label = _room_label(estate, shape, room)
            last_step = f"create_thread({label})"
            thread = await parent_channel.create_thread(
                name=label,
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
            created_thread_ids[room] = thread.id  # track immediately so a failed add_user/pin below still gets cleaned up
            last_step = f"join own thread on {label} (thread {thread.id})"
            await thread.join()
            last_step = f"add_user({target}) on {label} (thread {thread.id})"
            await thread.add_user(target)
            last_step = f"pin owner note on {label}"
            await _post_and_pin_owner_note(thread, target, label)

        if shape in ("guesthouse", "ghetto"):
            cluster_key = _cluster_key_for(estate)
            shared = database.get_shared_housing_threads(cluster_key)

            if shared is None:
                last_step = "create_thread(Kitchen, shared)"
                kitchen_thread = await parent_channel.create_thread(
                    name="Kitchen", type=discord.ChannelType.private_thread, invitable=False,
                )
                last_step = "create_thread(Bathroom, shared)"
                bathroom_thread = await parent_channel.create_thread(
                    name="Bathroom", type=discord.ChannelType.private_thread, invitable=False,
                )
                database.create_shared_housing_threads(cluster_key, kitchen_thread.id, bathroom_thread.id)
                shared_kitchen_id, shared_bathroom_id = kitchen_thread.id, bathroom_thread.id
            else:
                shared_kitchen_id = int(shared["kitchen_thread_id"])
                shared_bathroom_id = int(shared["bathroom_thread_id"])

            for thread_id in (shared_kitchen_id, shared_bathroom_id):
                thread = await _get_thread(guild, thread_id)
                if thread is not None:
                    try:
                        await thread.join()
                    except discord.HTTPException:
                        pass  # already a member, most likely
                    last_step = f"add_user({target}) on shared thread {thread_id}"
                    await thread.add_user(target)  # let a failure here raise too, same as the private-room loop above

    except discord.HTTPException as e:
        for thread_id in created_thread_ids.values():
            thread = await _get_thread(guild, thread_id)
            if thread is not None:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass
        if role_was_granted and role is not None:
            try:
                await target.remove_roles(role, reason="Housing assignment rolled back")
            except discord.HTTPException:
                pass
        return (
            f"\u26d4 Couldn't create the housing threads. Try again \u2014 nothing was assigned.\n"
            f"Failed at: `{last_step}`\n"
            f"`{type(e).__name__} {e.status}: {e.text}`\n"
            f"Channel used: {parent_channel.mention} (`{parent_channel.id}`) \u2014 confirm this is the "
            f"channel you fixed permissions on, and that no other channel shares its exact name."
        )

    database.create_housing(
        resident_id=target.id,
        estate=estate,
        house_number=house_number,
        assigned_at=_iso(_now()),
        kitchen_thread_id=created_thread_ids.get("kitchen"),
        bathroom_thread_id=created_thread_ids.get("bathroom"),
        bedroom_thread_id=created_thread_ids.get("bedroom"),
        office_thread_id=created_thread_ids.get("office"),
    )

    return f"\U0001f3e0 {target.mention} has been assigned House #{house_number} at {_loc_name(estate)}."


async def evict_resident(guild: discord.Guild, target_id: int) -> str:
    housing = database.get_housing(target_id)
    if housing is None:
        return "\u26d4 That player doesn't have an assigned house."

    estate = housing["estate"]
    shape = HOUSING_ESTATE_SHAPES.get(estate)

    for column in PRIVATE_THREAD_COLUMNS.values():
        thread_id = housing[column]
        if thread_id:
            thread = await _get_thread(guild, int(thread_id))
            if thread is not None:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass

    if shape in ("guesthouse", "ghetto"):
        cluster_key = _cluster_key_for(estate)
        shared = database.get_shared_housing_threads(cluster_key)

        if shared is not None:
            member = guild.get_member(target_id)

            for column in ("kitchen_thread_id", "bathroom_thread_id"):
                thread_id = shared[column]
                if thread_id and member is not None:
                    thread = await _get_thread(guild, int(thread_id))
                    if thread is not None:
                        try:
                            await thread.remove_user(member)
                        except discord.HTTPException:
                            pass

            remaining = []
            for cluster_estate in _cluster_estates_for(estate):
                remaining.extend(database.housing_in_estate(cluster_estate))
            remaining = [row for row in remaining if row["resident_id"] != str(target_id)]

            if not remaining:
                for column in ("kitchen_thread_id", "bathroom_thread_id"):
                    thread_id = shared[column]
                    if thread_id:
                        thread = await _get_thread(guild, int(thread_id))
                        if thread is not None:
                            try:
                                await thread.delete()
                            except discord.HTTPException:
                                pass
                database.delete_shared_housing_threads(cluster_key)

    role_name = HOUSING_RESIDENT_ROLE.get(estate)
    if role_name:
        role = discord.utils.get(guild.roles, name=role_name)
        member = guild.get_member(target_id)
        if role is not None and member is not None:
            try:
                await member.remove_roles(role, reason="Housing eviction")
            except discord.HTTPException:
                pass

    database.delete_housing(target_id)

    return f"\U0001f6aa House #{housing['house_number']} at {_loc_name(estate)} has been vacated."


# ================================================================
# VISITOR INVITE UI
# ================================================================

class _InviteView(discord.ui.View):
    def __init__(self, owner_id: int, visitor_id: int, thread_id: int, estate: str, room_label: str, bot: commands.Bot):
        super().__init__(timeout=HOUSING_INVITE_RESPONSE_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.visitor_id = visitor_id
        self.thread_id = thread_id
        self.estate = estate
        self.room_label = room_label
        self.bot = bot

    async def _disable(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="\u2705")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.visitor_id:
            await interaction.response.send_message("This invite isn't for you.", ephemeral=True)
            return

        await self._disable(interaction)

        guild = self.bot.get_guild(interaction.guild_id) if interaction.guild_id else self.bot.guilds[0]
        thread = guild.get_thread(self.thread_id)

        if thread is not None:
            try:
                await thread.add_user(interaction.user)
                await thread.send(f"{interaction.user.mention} has joined **{self.room_label}**. \U0001f44b")
            except discord.HTTPException:
                pass

        database.add_housing_visitor(
            visitor_id=self.visitor_id,
            owner_id=self.owner_id,
            thread_id=self.thread_id,
            estate=self.estate,
            invited_at=_iso(_now()),
        )

        await interaction.followup.send(f"Joined **{self.room_label}**!", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="\u274c")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.visitor_id:
            await interaction.response.send_message("This invite isn't for you.", ephemeral=True)
            return

        await self._disable(interaction)
        await interaction.followup.send("Invite declined.", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        pass  # nothing was granted yet — nothing to undo


# ================================================================
# COG
# ================================================================

class HousingCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scan_housing.start()

    def cog_unload(self):
        self.scan_housing.cancel()

    # ============================================================
    # !ASSIGN-HOUSE / !EVICT-RESIDENT
    # (permission tier depends on estate — see module docstring)
    # ============================================================

    @commands.command(name="assign-house")
    @checks.require_location("rental")
    async def assign_house_cmd(self, ctx: commands.Context, target: discord.Member, estate: str):
        estate = estate.strip().lower()

        if estate not in HOUSING_ESTATE_SHAPES:
            await ctx.send(f"\u26d4 `{estate}` is not a housing-eligible estate.")
            return

        allowed, requirement = _assign_permission(ctx.author, estate)
        if not allowed:
            await ctx.send(f"\u26d4 You need {requirement} to do this.")
            return

        result = await assign_house(ctx.guild, target, estate)
        await ctx.send(result)

    @commands.command(name="evict-resident")
    @checks.require_location("rental")
    async def evict_resident_cmd(self, ctx: commands.Context, target: discord.Member):
        housing = database.get_housing(target.id)
        if housing is None:
            await ctx.send("\u26d4 That player doesn't have an assigned house.")
            return

        allowed, requirement = _assign_permission(ctx.author, housing["estate"])
        if not allowed:
            await ctx.send(f"\u26d4 You need {requirement} to do this.")
            return

        result = await evict_resident(ctx.guild, target.id)
        await ctx.send(result)

    # ============================================================
    # !SET-HOUSING-CAPACITY (admin-only)
    # ============================================================

    @commands.command(name="set-housing-capacity")
    async def set_housing_capacity_cmd(self, ctx: commands.Context, estate: str, capacity: int):
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("\u26d4 Admins only.")
            return

        estate = estate.strip().lower()
        if estate not in HOUSING_ESTATE_SHAPES:
            await ctx.send(f"\u26d4 `{estate}` is not a housing-eligible estate.")
            return

        if capacity < 1:
            await ctx.send("\u26d4 Capacity must be at least 1.")
            return

        database.set_housing_capacity(estate, capacity)
        await ctx.send(f"\u2705 {_loc_name(estate)} housing capacity set to **{capacity}**.")

    # ============================================================
    # !HOUSING-DEBUG (admin-only) — dumps the raw overwrites Discord
    # actually has for an estate's channel, for diagnosing 403s that
    # persist despite permissions_for() reporting everything is fine.
    # Temporary/diagnostic command, not part of the original spec.
    # ============================================================

    @commands.command(name="housing-debug")
    async def housing_debug_cmd(self, ctx: commands.Context, estate: str):
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("\u26d4 Admins only.")
            return

        estate = estate.strip().lower()
        if estate not in HOUSING_ESTATE_SHAPES:
            await ctx.send(f"\u26d4 `{estate}` is not a housing-eligible estate.")
            return

        parent_channel = discord.utils.get(ctx.guild.text_channels, name=LOCATIONS[estate]["channel"])
        if parent_channel is None:
            await ctx.send(f"\u26d4 Couldn't find the Discord channel for {_loc_name(estate)}.")
            return

        me = ctx.guild.me
        lines = [
            f"discord.py version: {discord.__version__}",
            f"Channel: {parent_channel.mention} (`{parent_channel.id}`)",
            f"Category: {parent_channel.category.name if parent_channel.category else 'None'} "
            f"(`{parent_channel.category.id if parent_channel.category else '-'}`)",
            f"Bot member roles: {', '.join(r.name for r in me.roles)}",
            "",
            f"Guild features: {', '.join(ctx.guild.features) if ctx.guild.features else '(none)'}",
            f"  PRIVATE_THREADS feature present: {'PRIVATE_THREADS' in ctx.guild.features}",
            "",
            "-- Effective permissions_for(bot) --",
        ]
        perms = parent_channel.permissions_for(me)
        for name in ("view_channel", "create_private_threads", "send_messages_in_threads", "manage_threads"):
            lines.append(f"  {name}: {getattr(perms, name)}")

        lines.append("")
        lines.append("-- Raw channel overwrites --")
        if parent_channel.overwrites:
            for target_obj, overwrite in parent_channel.overwrites.items():
                allow, deny = overwrite.pair()
                lines.append(f"  {target_obj.name}: allow={[p for p, v in allow if v]} deny={[p for p, v in deny if v]}")
        else:
            lines.append("  (none \u2014 fully inherited from category)")

        if parent_channel.category is not None:
            lines.append("")
            lines.append("-- Raw category overwrites --")
            if parent_channel.category.overwrites:
                for target_obj, overwrite in parent_channel.category.overwrites.items():
                    allow, deny = overwrite.pair()
                    lines.append(f"  {target_obj.name}: allow={[p for p, v in allow if v]} deny={[p for p, v in deny if v]}")
            else:
                lines.append("  (none)")

        lines.append("")
        lines.append(f"Synced to category: {parent_channel.permissions_synced}")

        text = "\n".join(lines)
        await ctx.send(f"```\n{text[:1900]}\n```")

    # ============================================================
    # !ASSIGN-VISITOR
    # (CHO for standard estates, Mayor of Eko for the Presidential
    # Villa cluster, refused outright for ghetto — see docstring)
    # ============================================================

    @commands.command(name="assign-visitor")
    @checks.require_location("rental")
    async def assign_visitor_cmd(self, ctx: commands.Context, target: discord.Member, estate: str):
        estate = estate.strip().lower()
        shape = HOUSING_ESTATE_SHAPES.get(estate)

        if shape is None:
            await ctx.send(f"\u26d4 `{estate}` is not a housing-eligible estate.")
            return

        if shape == "ghetto":
            await ctx.send(
                "\u26d4 Ghetto residences don't have invitable rooms \u2014 there's no visitor role to assign."
            )
            return

        allowed, requirement = _visitor_permission(ctx.author, estate)
        if not allowed:
            await ctx.send(f"\u26d4 You need {requirement} to do this.")
            return

        role_name = HOUSING_VISITOR_ROLE.get(estate)
        if role_name is None:
            await ctx.send(f"\u26d4 `{estate}` doesn't have a visitor role configured.")
            return

        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if role is None:
            await ctx.send(f"\u26d4 Role **{role_name}** doesn't exist on this server.")
            return

        await target.add_roles(role, reason="Housing visitor eligibility")
        await ctx.send(f"\u2705 {target.mention} can now be invited to houses at {_loc_name(estate)}.")

    # ============================================================
    # !INVITE / !LEAVE-ROOM
    # ============================================================

    @commands.command(name="invite")
    async def invite_cmd(self, ctx: commands.Context, target: discord.Member, room: str):
        housing = database.get_housing(ctx.author.id)
        if housing is None:
            await ctx.send("\u26d4 You don't have a house to invite anyone into.")
            return

        estate = housing["estate"]
        shape = HOUSING_ESTATE_SHAPES.get(estate)

        if shape == "ghetto":
            await ctx.send(
                "\u26d4 You can't invite anyone into ghetto housing \u2014 the shared Kitchen and "
                "Bathroom there are for residents only, not invite-based."
            )
            return

        room = room.strip().lower()

        column = PRIVATE_THREAD_COLUMNS.get(room)
        thread_id = None

        if column and housing[column]:
            thread_id = int(housing[column])
        elif room in ("kitchen", "bathroom") and shape == "guesthouse":
            shared = database.get_shared_housing_threads(_cluster_key_for(estate))
            if shared is not None:
                shared_column = "kitchen_thread_id" if room == "kitchen" else "bathroom_thread_id"
                thread_id = int(shared[shared_column]) if shared[shared_column] else None

        if thread_id is None:
            await ctx.send(f"\u26d4 Your house at {_loc_name(estate)} doesn't have a `{room}`.")
            return

        if ctx.channel.id != thread_id:
            await ctx.send("\u26d4 Run `!invite` inside the room's own thread.")
            return

        visitor_role_name = HOUSING_VISITOR_ROLE.get(estate)
        if visitor_role_name is None or not _has_role(target, visitor_role_name):
            await ctx.send(
                f"\u26d4 {target.mention} needs the **{visitor_role_name or 'visitor'}** role first "
                f"\u2014 ask the right person to run `!assign-visitor`."
            )
            return

        label = _room_label(estate, shape, room)

        try:
            await target.send(
                f"\U0001f3e0 {ctx.author.mention} has invited you to their **{label}** "
                f"at {_loc_name(estate)}. You have "
                f"{HOUSING_INVITE_RESPONSE_TIMEOUT_SECONDS // 60} minutes to respond.",
                view=_InviteView(ctx.author.id, target.id, thread_id, estate, label, self.bot),
            )
        except discord.Forbidden:
            await ctx.send(f"\u26d4 Couldn't DM {target.mention} \u2014 they may have DMs disabled.")
            return

        await ctx.send(f"\U0001f4e9 Invite sent to {target.mention}.")

    @commands.command(name="leave-room")
    async def leave_room_cmd(self, ctx: commands.Context):
        visitor_row = None
        for row in database.visitors_for_thread(ctx.channel.id):
            if row["visitor_id"] == str(ctx.author.id):
                visitor_row = row
                break

        if visitor_row is None:
            await ctx.send("\u26d4 You're not visiting this room.")
            return

        try:
            await ctx.channel.remove_user(ctx.author)
        except discord.HTTPException:
            pass

        database.remove_housing_visitor(ctx.author.id, ctx.channel.id)
        await ctx.send(f"\U0001f44b {ctx.author.mention} has left.")

    # ============================================================
    # BACKGROUND SCAN — presence-based locking + visit timeouts
    # ============================================================

    @tasks.loop(seconds=HOUSING_SCAN_INTERVAL_SECONDS)
    async def scan_housing(self):
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            return

        now = _now()

        # ------------------------------------------------------
        # 1. Auto-kick expired visits.
        # ------------------------------------------------------
        for visitor_row in database.all_housing_visitors():
            invited_at = _parse(visitor_row["invited_at"])
            if (now - invited_at).total_seconds() < HOUSING_VISIT_TIMEOUT_SECONDS:
                continue

            visitor_id = int(visitor_row["visitor_id"])
            owner_id = int(visitor_row["owner_id"])
            thread_id = int(visitor_row["thread_id"])
            estate = visitor_row["estate"]

            thread = await _get_thread(guild, thread_id)
            member = guild.get_member(visitor_id)

            if thread is not None and member is not None:
                try:
                    await thread.remove_user(member)
                except discord.HTTPException:
                    pass

            database.remove_housing_visitor(visitor_id, thread_id)

            parent_channel = discord.utils.get(guild.text_channels, name=LOCATIONS.get(estate, {}).get("channel", ""))
            owner_member = guild.get_member(owner_id)
            if parent_channel is not None:
                visitor_mention = member.mention if member else f"<@{visitor_id}>"
                owner_mention = owner_member.mention if owner_member else f"<@{owner_id}>"
                try:
                    await parent_channel.send(f"\U0001f6aa {visitor_mention} was kicked out of {owner_mention}'s residence.")
                except discord.HTTPException:
                    pass

        # ------------------------------------------------------
        # 2. Build watcher lists: thread_id -> [(user_id, estate), ...]
        # ------------------------------------------------------
        watchers: dict[int, list[tuple[int, str]]] = {}

        def _watch(thread_id, user_id, estate):
            if thread_id is None:
                return
            watchers.setdefault(int(thread_id), []).append((user_id, estate))

        for housing in database.all_housing():
            resident_id = int(housing["resident_id"])
            estate = housing["estate"]
            for column in PRIVATE_THREAD_COLUMNS.values():
                _watch(housing[column], resident_id, estate)

        for visitor_row in database.all_housing_visitors():
            _watch(visitor_row["thread_id"], int(visitor_row["visitor_id"]), visitor_row["estate"])

        for shared in database.all_shared_housing_threads():
            cluster_key = shared["cluster_key"]
            cluster_estates = HOUSING_GUESTHOUSE_CODES if cluster_key == HOUSING_GUESTHOUSE_CLUSTER_KEY else (cluster_key,)
            residents = []
            for cluster_estate in cluster_estates:
                residents.extend(database.housing_in_estate(cluster_estate))
            for row in residents:
                _watch(shared["kitchen_thread_id"], int(row["resident_id"]), row["estate"])
                _watch(shared["bathroom_thread_id"], int(row["resident_id"]), row["estate"])

        # ------------------------------------------------------
        # 3. Lock/unlock each tracked thread based on presence.
        # ------------------------------------------------------
        for thread_id, watcher_list in watchers.items():
            present = False
            for user_id, estate in watcher_list:
                player = database.get_player(user_id)
                if player is not None and player["location"] == estate:
                    present = True
                    break

            thread = await _get_thread(guild, thread_id)
            if thread is None:
                continue

            desired_locked = not present
            if thread.locked != desired_locked:
                try:
                    await thread.edit(locked=desired_locked)
                except discord.HTTPException:
                    pass

    @scan_housing.before_loop
    async def before_scan_housing(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(HousingCog(bot))
