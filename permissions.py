"""
Discord permission management for Eko Metropolis.

The bot automatically ensures it can SEND messages in every channel
registered in LOCATIONS.

Player visibility remains controlled by Discord roles/channel settings.

Player writing access is controlled separately by their database location:
- At their current location -> can send messages.
- When they leave -> write access is removed.
- At a toll checkpoint -> temporary write access is granted.
- After paying the toll -> toll-channel write access is removed.

SUB-LOCATIONS:
A location can have "rooms" attached to it (see database.py's
sub_locations table) — e.g. the bank has front-desk, atm,
bank-manager, deposit, auditor, cbe-deputy, cbe-chairman all
hanging off the "bank" location.

Arriving at a parent location grants write access to that
location's channel exactly as before, AND to every attached
sub-location the member qualifies for:
    - access="public"  -> everyone who arrives gets it (e.g. atm)
    - access="role"    -> only members holding role_name get it
                           (e.g. bank-manager, cbe-chairman)

Leaving the parent location revokes all of it, same as the
existing single-channel behavior.
"""

import discord

import database

from config import LOCATIONS


# ============================================================
# DYNAMIC LOCATION LOOKUP
#
# Dynamically registered locations (config.LOCATIONS + the
# `locations` table created via !location-registration) are
# merged here so the rest of this module doesn't need to care
# which source a code came from.
# ============================================================

def _lookup_location(
    code: str
) -> dict | None:

    """
    Return a LOCATIONS-shaped dict for `code`, checking the
    hand-authored config.LOCATIONS first, then the dynamically
    registered `locations` table.
    """

    static_loc = LOCATIONS.get(code)

    if static_loc is not None:
        return static_loc

    row = database.get_location(code)

    if row is None:
        return None

    return {
        "name": row["name"],
        "channel": row["channel_name"],
        "zone": row["zone"],
    }


# ============================================================
# CHANNEL LOOKUP
# ============================================================

def get_channel_for_code(
    guild: discord.Guild,
    code: str
) -> discord.TextChannel | None:
    """
    Return the Discord channel mapped to a location code.

    Checks config.LOCATIONS first, then dynamically registered
    locations (businesses etc.), then sub-locations, so callers
    don't need to know which table a code came from.
    """

    loc = _lookup_location(code)

    if loc is not None:
        return discord.utils.get(
            guild.text_channels,
            name=loc["channel"]
        )

    sub = database.get_sub_location(code)

    if sub is not None:
        return discord.utils.get(
            guild.text_channels,
            name=sub["channel_name"]
        )

    return None


# ============================================================
# BOT PERMISSIONS
# ============================================================

async def ensure_bot_channel_permissions(
    guild: discord.Guild
) -> None:
    """
    Ensure the bot has permission to view and send messages in every
    channel registered in LOCATIONS.

    This means arrival messages and toll messages do not require
    manually configuring every individual destination channel.
    """

    me = guild.me

    if me is None:
        return

    all_channel_names = [
        loc["channel"]
        for loc in LOCATIONS.values()
    ]

    all_channel_names += [
        loc["channel_name"]
        for loc in database.get_all_dynamic_locations().values()
    ]

    all_channel_names += [
        sub["channel_name"]
        for sub in database.get_all_sub_locations()
    ]

    for channel_name in all_channel_names:

        channel = discord.utils.get(
            guild.text_channels,
            name=channel_name
        )

        if channel is None:
            continue

        try:
            overwrite = channel.overwrites_for(me)

            overwrite.view_channel = True
            overwrite.send_messages = True
            overwrite.embed_links = True
            overwrite.read_message_history = True
            overwrite.attach_files = True

            await channel.set_permissions(
                me,
                overwrite=overwrite,
                reason="Eko Bot automatic channel permissions"
            )

        except discord.Forbidden:
            print(
                f"[PERMISSIONS] Cannot configure #{channel.name}. "
                f"Bot lacks Manage Channels permission."
            )

        except discord.HTTPException as error:
            print(
                f"[PERMISSIONS] Failed configuring "
                f"#{channel.name}: {error}"
            )


# ============================================================
# PLAYER WRITE ACCESS
# ============================================================

async def _set_channel_write_access(
    channel: discord.TextChannel,
    member: discord.Member,
    allowed: bool
) -> None:

    """
    Low-level helper: grant or revoke Send Messages for a single
    member in a single channel. Shared by set_write_access() and
    the sub-location grant/revoke logic below.
    """

    overwrite = channel.overwrites_for(
        member
    )

    if allowed:
        overwrite.send_messages = True

    else:
        # Remove the player's explicit send permission.
        overwrite.send_messages = None

    try:

        await channel.set_permissions(
            member,
            overwrite=overwrite,
            reason="Eko player location write access"
        )

    except discord.Forbidden:
        print(
            f"[PERMISSIONS] Cannot change player permission "
            f"for {member} in #{channel.name}."
        )

    except discord.HTTPException as error:
        print(
            f"[PERMISSIONS] Failed changing player permission "
            f"for #{channel.name}: {error}"
        )


async def set_write_access(
    guild: discord.Guild,
    member: discord.Member,
    code: str,
    allowed: bool
) -> None:
    """
    Grant or revoke Send Messages for a specific player in a
    location's own channel, AND in every sub-location attached to
    it that the member qualifies for.

    This does NOT control channel visibility.
    """

    channel = get_channel_for_code(
        guild,
        code
    )

    if channel is not None:
        await _set_channel_write_access(
            channel,
            member,
            allowed
        )

    await _set_sub_location_access(
        guild,
        member,
        code,
        allowed
    )


# ============================================================
# SUB-LOCATION CLUSTER ACCESS
# ============================================================

async def _set_sub_location_access(
    guild: discord.Guild,
    member: discord.Member,
    parent_code: str,
    allowed: bool
) -> None:

    """
    Grant or revoke write access to every sub-location attached to
    parent_code.

    On grant (allowed=True):
        - access="public" sub-locations open for everyone.
        - access="role" sub-locations only open for members
          holding role_name.

    On revoke (allowed=False):
        - every attached sub-location is closed for this member,
          regardless of access type — leaving the parent location
          closes all of its rooms.
    """

    sub_locations = database.get_sub_locations_for_parent(
        parent_code
    )

    for sub in sub_locations:

        if allowed and sub["access"] == "role":

            role_name = sub["role_name"]

            has_role = (
                role_name is not None
                and discord.utils.get(
                    member.roles,
                    name=role_name
                ) is not None
            )

            if not has_role:
                continue

        sub_channel = discord.utils.get(
            guild.text_channels,
            name=sub["channel_name"]
        )

        if sub_channel is None:
            continue

        await _set_channel_write_access(
            sub_channel,
            member,
            allowed
        )


# ============================================================
# MOVE PLAYER WRITE ACCESS
# ============================================================

async def move_write_access(
    guild: discord.Guild,
    member: discord.Member,
    old_code: str | None,
    new_code: str
) -> None:
    """
    Remove the player's writing permission from the old location
    and give it to the new location.
    """

    if old_code and old_code != new_code:

        await set_write_access(
            guild,
            member,
            old_code,
            allowed=False
        )

    await set_write_access(
        guild,
        member,
        new_code,
        allowed=True
    )


# ============================================================
# CHANNEL NAME -> LOCATION CODE
# ============================================================

def channel_code_for_channel_name(
    channel_name: str
) -> str | None:
    """
    Reverse lookup from Discord channel name to location code.

    Checks config.LOCATIONS first, then dynamically registered
    locations.
    """

    for code, loc in LOCATIONS.items():

        if loc["channel"] == channel_name:
            return code

    for code, loc in database.get_all_dynamic_locations().items():

        if loc["channel_name"] == channel_name:
            return code

    return None
