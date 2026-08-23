"""
Location-sensitive commands must validate BOTH the channel the command was
typed in AND the player's actual database location (requirements #23).
Never "they typed it here, so they must be here."
"""

from discord.ext import commands

import database
from config import LOCATIONS


class WrongChannel(commands.CheckFailure):
    def __init__(self, expected_channel: str):
        self.expected_channel = expected_channel
        super().__init__(f"This command only works in #{expected_channel}.")


class NotAtLocation(commands.CheckFailure):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"You are not currently at {LOCATIONS[code]['name']}.")


class CurrentlyTraveling(commands.CheckFailure):
    pass


def require_location(code: str):
    """
    Command must be typed in the channel mapped to `code`, AND the player's
    database location must actually be `code`. Both must agree.
    """
    expected_channel = LOCATIONS[code]["channel"]

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.channel.name != expected_channel:
            raise WrongChannel(expected_channel)

        player = database.get_or_create_player(ctx.author.id)
        if player["traveling"]:
            raise CurrentlyTraveling("You are currently travelling and cannot do this.")
        if player["location"] != code:
            raise NotAtLocation(code)
        return True

    return commands.check(predicate)


def require_not_traveling():
    async def predicate(ctx: commands.Context) -> bool:
        player = database.get_or_create_player(ctx.author.id)
        if player["traveling"]:
            raise CurrentlyTraveling("You are currently travelling and cannot do this.")
        return True

    return commands.check(predicate)
