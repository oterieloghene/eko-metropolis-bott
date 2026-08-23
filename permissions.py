"""
Discord permission model (requirements #24):
  - Roles control channel VISIBILITY (handled by normal Discord role/channel
    permission setup done in the server itself — this bot does not need to
    manage visibility, only WRITING).
  - The player's current DB location controls channel WRITING access.
    The bot actively grants send-message permission at the new location and
    revokes it at the old one whenever location changes.

This keeps roles and physical location as two separate systems, as required.
"""

import discord

from config import LOCATIONS


def get_channel_for_code(guild: discord.Guild, code: str) -> discord.TextChannel | None:
    loc = LOCATIONS.get(code)
    if not loc:
        return None
    return discord.utils.get(guild.text_channels, name=loc["channel"])


async def set_write_access(guild: discord.Guild, member: discord.Member, code: str, allowed: bool) -> None:
    """Grant or revoke send_messages for `member` in the channel mapped to `code`."""
    channel = get_channel_for_code(guild, code)
    if channel is None:
        return
    overwrite = channel.overwrites_for(member)
    overwrite.send_messages = allowed if allowed else False
    if not allowed:
        # Fully clear the overwrite when revoking so it doesn't linger
        # forever for players who've moved on, keeping channel overwrite
        # lists small.
        overwrite.send_messages = None
    await channel.set_permissions(member, overwrite=overwrite)


async def move_write_access(guild: discord.Guild, member: discord.Member,
                             old_code: str | None, new_code: str) -> None:
    """Revoke writing at the old location, grant it at the new one."""
    if old_code and old_code != new_code:
        await set_write_access(guild, member, old_code, allowed=False)
    await set_write_access(guild, member, new_code, allowed=True)


def channel_code_for_channel_name(channel_name: str) -> str | None:
    """Reverse-lookup: given a Discord channel name, find its location code."""
    for code, loc in LOCATIONS.items():
        if loc["channel"] == channel_name:
            return code
    return None
