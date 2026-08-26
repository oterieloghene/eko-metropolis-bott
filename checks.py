"""
Location-sensitive commands must validate BOTH the channel the command was
typed in AND the player's actual database location (requirements #23).
Never "they typed it here, so they must be here."
"""

from discord.ext import commands

import database
from config import LOCATIONS, AREAS


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


class NotInArea(commands.CheckFailure):
    def __init__(self):
        super().__init__("This command only works inside an active area thread.")


class WrongAreaKind(commands.CheckFailure):
    def __init__(self, expected_kind: str):
        self.expected_kind = expected_kind
        label = "a shopping area" if expected_kind == "shop" else "an event area"
        super().__init__(f"This command only works in {label}.")


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


def require_area(kind: str | None = None):
    """
    Command must be typed inside the Discord thread that is
    CURRENTLY an active area (see database.areas). If `kind` is
    given ("shop" or "event"), the area must also be of that
    kind — e.g. !mall only works in a "shop" area's thread.

    On success, stashes the area row on ctx.area so the command
    doesn't have to look it up again.
    """

    async def predicate(ctx: commands.Context) -> bool:
        area = database.get_area_by_thread(ctx.channel.id)

        if area is None:
            raise NotInArea()

        area_kind = AREAS.get(area["area_code"], {}).get("kind")

        if kind is not None and area_kind != kind:
            raise WrongAreaKind(kind)

        ctx.area = area
        return True

    return commands.check(predicate)
