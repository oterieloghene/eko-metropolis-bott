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
"""

import discord

from config import LOCATIONS


# ============================================================
# CHANNEL LOOKUP
# ============================================================

def get_channel_for_code(
    guild: discord.Guild,
    code: str
) -> discord.TextChannel | None:
    """Return the Discord channel mapped to a location code."""

    loc = LOCATIONS.get(code)

    if not loc:
        return None

    return discord.utils.get(
        guild.text_channels,
        name=loc["channel"]
    )


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

    for code, loc in LOCATIONS.items():

        channel = discord.utils.get(
            guild.text_channels,
            name=loc["channel"]
        )

        if channel is None:
            continue

        try:
            overwrite = channel.overwrites_for(me)

            overwrite.view_channel = True
            overwrite.send_messages = True
            overwrite.embed_links = True
            overwrite.read_message_history = True

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

async def set_write_access(
    guild: discord.Guild,
    member: discord.Member,
    code: str,
    allowed: bool
) -> None:
    """
    Grant or revoke Send Messages for a specific player.

    This does NOT control channel visibility.
    """

    channel = get_channel_for_code(
        guild,
        code
    )

    if channel is None:
        return

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
    """Reverse lookup from Discord channel name to location code."""

    for code, loc in LOCATIONS.items():

        if loc["channel"] == channel_name:
            return code

    return None
