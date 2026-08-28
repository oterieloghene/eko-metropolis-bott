"""
Police system — patrol car fleet + !patrol.

STAGE 1 (fleet administration): !purchasepd, !assignpd,
!retrievepd, !pdfleet — see the FLEET MODEL section below.

STAGE 2 (this update): !patrol — the officer's !drive equivalent.
Deliberately self-contained rather than hooked into cogs/travel.py's
!drive (which is tightly bound to private-car/taxi/mechanic
overrides) — patrol trips get their own simple point-to-point
movement, with the "@officer1 on patrol" / "10-1" tagging the
spec calls for. Fuel and vehicle condition are still deducted
through the same database.update_vehicle() call every other
multi-vehicle-aware cog uses, so !vehicle/!refuel/!fixcar all
still work normally on a patrol car afterward.

STAGE 3 (this update): !checkpoints/!clear — pausing OTHER
travelers (private cars, taxis, dispatch motorcycles) at a
checkpoint, stacked independently from toll gates via its own
"current_checkpoint" journey-state field in cogs/travel.py's
active_journeys (see _stop_at_checkpoint / resume_after_checkpoint
there). A dispatch Bicycle is exempt, same as it is from tolls
and fuel.

CHECKPOINT FLOW:

    1. Officer: !checkpoints (no args — uses wherever they
       currently are)
       -> needs the Officer role, must be standing in the
          channel matching their current location, and must have
          their OWN patrol car currently at that same location
          (checked via database.get_selected_vehicle()).
       -> only allowed on a major route zone (island, mainland,
          ghetto) — see CHECKPOINT_ZONES below.
       -> running it again at the same location where THIS
          officer already has a checkpoint mounted dismounts it.

    2. cogs/travel.py's journey loop pauses any traveling private
       car, taxi, or dispatch motorcycle the instant it reaches a
       checkpointed node — write access to that channel unlocks
       for the driver only (passengers stay read-only), exactly
       like a toll stop, except there's no self-service way
       through.

    3. Officer: !clear <@player>
       -> must be standing at the checkpoint the player is
          currently paused at.
       -> releases that one player and resumes their journey via
          TravelCog.resume_after_checkpoint().

FLEET MODEL — mirrors cogs/dispatch.py's commercial-vehicle
pattern (NOT cogs/bus.py's autonomous route-simulated buses,
since patrol cars are player-driven):

    1. Mayor buys N patrol cars: !purchasepd <qty>
       -> land in an UNASSIGNED POOL, persisted to
          POLICE_FLEET_FILE (police_fleet.json), same on-disk
          JSON pattern as cogs/bus.py's bus_fleet.json.

    2. Chief/Deputy Chief hand one to a specific officer:
       !assignpd <@officer>
       -> pulls a car out of the pool, adds it to that officer's
          owned-vehicle list via database.add_vehicle(...,
          vehicle_type="police") — same call dispatch.py uses
          for bicycles/motorcycles.

    3. Chief/Deputy Chief take it back:
       !retrievepd <@officer>
       -> database.remove_vehicle() takes it off the officer,
          it goes back into the unassigned pool.

    4. !pdfleet
       -> status check: how many cars are purchased, how many
          are sitting unassigned vs currently assigned.

PATROL FLOW:

    1. Officer: !patrol <destination> [@ride-along officer]
       -> must have a Police Patrol Car selected (!usevehicle).
       -> optional passenger must also be an Officer, physically
          at the same current location, and not already
          traveling — patrol car capacity is driver + 1.
       -> both get write access revoked at the origin and
          traveling=1, exactly like a normal !drive departure.
       -> departure message: "🚓 @officer1 on patrol" (solo) or
          "🚓 @officer1 and @officer2 on patrol" (with passenger).

    2. After the normal distance-based travel time:
       -> fuel + condition deducted via database.update_vehicle()
          on the driver's selected (police) vehicle.
       -> both get write access granted at the destination and
          traveling=0.
       -> arrival message: "🚓 @officer1 10-1" (+ @officer2 if
          riding along).

All fleet-administration commands must be used from the Police
Department channel (POLICE_STATION_LOCATION); !patrol is used
from wherever the officer currently is, same as !drive.
"""

import json
import os
import uuid
import asyncio

import discord
from discord.ext import commands

import database
import permissions

from routing import (
    find_route,
    NoRouteError,
)

from config import (
    LOCATIONS,
    VEHICLES,
    OFFICER_ROLE,
    POLICE_CHIEF_ROLE,
    POLICE_DEPUTY_CHIEF_ROLE,
    POLICE_ASSIGNMENT_ROLES,
    POLICE_PURCHASE_ROLE,
    POLICE_STATION_LOCATION,
    POLICE_PATROL_VEHICLE,
    POLICE_FLEET_FILE,
    POLICE_MESSAGE_DELETE_DELAY_SECONDS,
    CONDITION_LOSS_PER_KM,
    MIN_TRAVEL_TIME_SECONDS,
    MAX_TRAVEL_TIME_SECONDS,
    TRAVEL_SECONDS_PER_KM,
)


# ================================================================
# SMALL LOCATION HELPERS
#
# Duplicated (not imported) from travel.py on purpose, to avoid a
# circular import — same pattern carpool.py/dispatch.py already
# use.
# ================================================================

def _name(code: str) -> str:
    loc = LOCATIONS.get(code)

    return loc["name"] if loc else code


def _normalise_code(value: str) -> str:
    return (
        str(value)
        .strip()
        .lower()
        .replace(" ", "-")
    )


def _travel_duration(distance: float) -> float:

    return max(
        MIN_TRAVEL_TIME_SECONDS,
        min(
            distance * TRAVEL_SECONDS_PER_KM,
            MAX_TRAVEL_TIME_SECONDS,
        ),
    )


# ================================================================
# ACTIVE PATROLS
#
# Module-level so a restart-safe check ("already on patrol") is
# possible without needing the Cog instance.
#
#     _active_patrols[driver_id] = {
#         "passenger_id": int | None,
#         "origin": str,
#         "destination": str,
#     }
# ================================================================

_active_patrols: dict[int, dict] = {}


# ================================================================
# ACTIVE CHECKPOINTS
#
#     _active_checkpoints[location_code] = {
#         "officer_id": int,
#         "guild_id":   int,
#         "paused":     {user_id: True, ...},  # who's currently
#                                              # stopped here
#     }
#
# Module-level (not on the Cog instance) so cogs/travel.py can
# call the helper functions below without needing this Cog's
# instance — same pattern _active_patrols already uses.
# ================================================================

_active_checkpoints: dict[str, dict] = {}

# Checkpoints can only be mounted on a major route, not at any
# arbitrary location (a house, an office, a shop, etc).
CHECKPOINT_ZONES = {"island", "mainland", "ghetto"}


def get_checkpoint(location_code: str) -> dict | None:
    """
    Look up an active checkpoint by location code. Called by
    cogs/travel.py's journey loop on every node the traveler
    passes through.
    """

    return _active_checkpoints.get(location_code)


def mark_paused(location_code: str, user_id: int) -> None:
    """
    Record that this player is currently stopped at this
    checkpoint, so !clear knows who's actually waiting here.
    Called by cogs/travel.py's _stop_at_checkpoint().
    """

    checkpoint = _active_checkpoints.get(location_code)

    if checkpoint is not None:
        checkpoint["paused"][user_id] = True


# AUTO-DELETE HELPERS (same pattern as taxi.py/dispatch.py)
# ================================================================

async def _delete_after_delay(msg: discord.Message, delay: float) -> None:

    await asyncio.sleep(delay)

    try:
        await msg.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _send_and_delete(ctx: commands.Context, content: str) -> None:

    msg = await ctx.send(content)

    asyncio.create_task(
        _delete_after_delay(msg, POLICE_MESSAGE_DELETE_DELAY_SECONDS)
    )

    try:
        await ctx.message.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


# ================================================================
# UNASSIGNED FLEET POOL (on-disk JSON, same pattern as
# cogs/bus.py's bus_fleet.json)
# ================================================================
#
# Each entry: {"car_id": str, "condition": float}. Fuel isn't
# tracked while a car sits in the pool — it's filled to the
# vehicle's fuel_capacity the moment it's assigned to an officer.
# ================================================================

def _load_pool() -> list:

    if not os.path.exists(POLICE_FLEET_FILE):
        return []

    try:

        with open(POLICE_FLEET_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            return []

        return data

    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _save_pool(pool: list) -> None:

    with open(POLICE_FLEET_FILE, "w", encoding="utf-8") as file:
        json.dump(pool, file, indent=4)


# ================================================================
# ROLE / LOCATION CHECKS
# ================================================================

def _has_role(member: discord.Member, role_name: str) -> bool:

    return discord.utils.get(member.roles, name=role_name) is not None


def _has_any_role(member: discord.Member, role_names: list) -> bool:

    return any(_has_role(member, name) for name in role_names)


def _at_station_channel(ctx: commands.Context) -> bool:

    expected_channel = LOCATIONS[POLICE_STATION_LOCATION]["channel"]
    return ctx.channel.name == expected_channel


async def _require_station_channel(ctx: commands.Context) -> bool:

    if _at_station_channel(ctx):
        return True

    expected_channel = LOCATIONS[POLICE_STATION_LOCATION]["channel"]

    await _send_and_delete(
        ctx,
        f"⛔ You need to be in #{expected_channel} to do this."
    )

    return False


# ================================================================
# COG
# ================================================================

class PoliceCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ============================================================
    # !PURCHASEPD <qty> — Mayor-gated, mirrors purchase_bus() in
    # cogs/bus.py. Adds N cars to the unassigned pool.
    # ============================================================

    @commands.command(name="purchasepd")
    async def purchasepd(
        self,
        ctx: commands.Context,
        quantity: int = 1
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, POLICE_PURCHASE_ROLE):

            await _send_and_delete(
                ctx,
                f"⛔ Only the **{POLICE_PURCHASE_ROLE}** can "
                f"purchase patrol cars."
            )

            return

        if not await _require_station_channel(ctx):
            return

        try:
            quantity = int(quantity)

        except (TypeError, ValueError):

            await _send_and_delete(ctx, "❌ Quantity must be a number.")
            return

        if quantity < 1:

            await _send_and_delete(
                ctx,
                "❌ You must purchase at least one patrol car."
            )

            return

        vehicle_cfg = VEHICLES[POLICE_PATROL_VEHICLE]

        pool = _load_pool()

        purchased_ids = []

        for _ in range(quantity):

            car_id = uuid.uuid4().hex[:8]

            pool.append({
                "car_id": car_id,
                "condition": vehicle_cfg.get("condition", 100),
            })

            purchased_ids.append(car_id)

        _save_pool(pool)

        await ctx.send(
            f"🚔 **{quantity}** patrol car(s) purchased and added "
            f"to the unassigned fleet — "
            f"**{len(pool)}** now sitting at "
            f"{LOCATIONS[POLICE_STATION_LOCATION]['name']} "
            f"waiting to be assigned with `!assignpd @officer`."
        )

    # ============================================================
    # !ASSIGNPD <@officer> — Chief/Deputy Chief gated. Pulls one
    # car from the unassigned pool and hands it to the officer.
    # ============================================================

    @commands.command(name="assignpd")
    async def assignpd(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_any_role(ctx.author, POLICE_ASSIGNMENT_ROLES):

            role_list = " or ".join(
                f"**{r}**" for r in POLICE_ASSIGNMENT_ROLES
            )

            await _send_and_delete(
                ctx,
                f"⛔ You need to be {role_list} to assign a "
                f"patrol car."
            )

            return

        if not await _require_station_channel(ctx):
            return

        if member is None:

            await _send_and_delete(ctx, "Usage: `!assignpd <@officer>`")
            return

        if not _has_role(member, OFFICER_ROLE):

            await _send_and_delete(
                ctx,
                f"⛔ {member.mention} isn't an **{OFFICER_ROLE}** — "
                f"patrol cars can only be assigned to officers."
            )

            return

        already_assigned = next(
            (
                v for v in database.get_vehicles(member.id)
                if v.get("type") == "police"
            ),
            None,
        )

        if already_assigned is not None:

            await _send_and_delete(
                ctx,
                f"{member.mention} already has a patrol car "
                f"assigned — use `!retrievepd @officer` first if "
                f"you want to swap it."
            )

            return

        pool = _load_pool()

        if not pool:

            await _send_and_delete(
                ctx,
                "⛔ There are no unassigned patrol cars right now — "
                "have the Mayor `!purchasepd` more."
            )

            return

        car = pool.pop(0)
        _save_pool(pool)

        vehicle_cfg = VEHICLES[POLICE_PATROL_VEHICLE]

        database.add_vehicle(
            member.id,
            name=POLICE_PATROL_VEHICLE,
            vehicle_type="police",
            location=POLICE_STATION_LOCATION,
            condition=car.get("condition", vehicle_cfg.get("condition", 100)),
            fuel=vehicle_cfg["fuel_capacity"],
            select=True,
        )

        await ctx.send(
            f"🚔 {member.mention} has been assigned patrol car "
            f"**#{car['car_id']}** — check `!vehicle` any time. "
            f"**{len(pool)}** unassigned car(s) remain in the pool."
        )

    # ============================================================
    # !RETRIEVEPD <@officer> — Chief/Deputy Chief gated. Reverses
    # !assignpd: takes the officer's patrol car back and returns
    # it to the unassigned pool.
    # ============================================================

    @commands.command(name="retrievepd")
    async def retrievepd(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_any_role(ctx.author, POLICE_ASSIGNMENT_ROLES):

            role_list = " or ".join(
                f"**{r}**" for r in POLICE_ASSIGNMENT_ROLES
            )

            await _send_and_delete(
                ctx,
                f"⛔ You need to be {role_list} to retrieve a "
                f"patrol car."
            )

            return

        if not await _require_station_channel(ctx):
            return

        if member is None:

            await _send_and_delete(ctx, "Usage: `!retrievepd <@officer>`")
            return

        assigned = next(
            (
                v for v in database.get_vehicles(member.id)
                if v.get("type") == "police"
            ),
            None,
        )

        if assigned is None:

            await _send_and_delete(
                ctx,
                f"{member.mention} doesn't currently have a "
                f"patrol car assigned."
            )

            return

        if member.id in _active_patrols:

            await _send_and_delete(
                ctx,
                f"⛔ {member.mention} is currently out on patrol — "
                f"wait for them to arrive before retrieving their "
                f"car."
            )

            return

        database.remove_vehicle(member.id, assigned["id"])

        pool = _load_pool()

        pool.append({
            "car_id": assigned.get("id", uuid.uuid4().hex[:8]),
            "condition": assigned.get("condition", 100),
        })

        _save_pool(pool)

        await ctx.send(
            f"🚔 {member.mention}'s patrol car has been retrieved "
            f"and returned to the unassigned fleet — "
            f"**{len(pool)}** now waiting to be reassigned."
        )

    # ============================================================
    # !PDFLEET — status check, no gating (read-only).
    # ============================================================

    @commands.command(name="pdfleet")
    async def pdfleet(self, ctx: commands.Context):

        pool = _load_pool()

        assigned_count = 0

        for member in ctx.guild.members:

            if member.bot:
                continue

            has_patrol_car = any(
                v.get("type") == "police"
                for v in database.get_vehicles(member.id)
            )

            if has_patrol_car:
                assigned_count += 1

        total = len(pool) + assigned_count

        await ctx.send(
            f"🚔 **Police Fleet Status**\n\n"
            f"Total patrol cars purchased: **{total}**\n"
            f"Currently assigned to officers: **{assigned_count}**\n"
            f"Unassigned (in the pool): **{len(pool)}**"
        )

    # ============================================================
    # !PATROL <destination> [@ride-along officer] — the officer's
    # !drive. Self-contained point-to-point movement (see the
    # module docstring for why this doesn't hook into travel.py's
    # !drive).
    # ============================================================

    @commands.command(name="patrol")
    async def patrol(
        self,
        ctx: commands.Context,
        destination: str = None,
        passenger: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, OFFICER_ROLE):

            await _send_and_delete(
                ctx,
                f"⛔ You need the **{OFFICER_ROLE}** role to patrol."
            )

            return

        if not destination:

            await _send_and_delete(
                ctx,
                "Usage: `!patrol <destination> [@ride-along officer]`"
            )

            return

        destination = _normalise_code(destination)

        driver = database.get_or_create_player(ctx.author.id)

        origin = _normalise_code(driver["location"])

        # --------------------------------------------------------
        # ALREADY TRAVELING / ALREADY ON PATROL
        # --------------------------------------------------------

        if driver["traveling"] or ctx.author.id in _active_patrols:

            await _send_and_delete(
                ctx,
                "You're already on the move."
            )

            return

        # --------------------------------------------------------
        # MUST HAVE A PATROL CAR SELECTED
        # --------------------------------------------------------

        selected = database.get_selected_vehicle(ctx.author.id)

        if selected is None or selected.get("type") != "police":

            await _send_and_delete(
                ctx,
                "⛔ You need a patrol car selected to `!patrol` — "
                "check `!vehicle` / `!usevehicle`."
            )

            return

        # --------------------------------------------------------
        # DESTINATION MUST BE A VALID, DIFFERENT LOCATION
        # --------------------------------------------------------

        if destination not in LOCATIONS:

            await _send_and_delete(ctx, "⛔ Unknown destination.")
            return

        if destination == origin:

            await _send_and_delete(
                ctx,
                "You're already there."
            )

            return

        try:
            path, distance = find_route(origin, destination)

        except NoRouteError:

            await _send_and_delete(
                ctx,
                f"No road route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )

            return

        # --------------------------------------------------------
        # OPTIONAL RIDE-ALONG PASSENGER
        # --------------------------------------------------------

        if passenger is not None:

            if passenger.id == ctx.author.id:

                await _send_and_delete(
                    ctx,
                    "⛔ You can't ride along with yourself."
                )

                return

            if not _has_role(passenger, OFFICER_ROLE):

                await _send_and_delete(
                    ctx,
                    f"⛔ {passenger.mention} isn't an "
                    f"**{OFFICER_ROLE}** — only officers can ride "
                    f"along on patrol."
                )

                return

            passenger_player = database.get_or_create_player(passenger.id)

            if (
                _normalise_code(passenger_player["location"]) != origin
                or passenger_player["traveling"]
                or passenger.id in _active_patrols
            ):

                await _send_and_delete(
                    ctx,
                    f"⛔ {passenger.mention} needs to be here at "
                    f"{_name(origin)}, and not already traveling, "
                    f"to ride along."
                )

                return

        # --------------------------------------------------------
        # FUEL CHECK
        # --------------------------------------------------------

        vehicle_cfg = VEHICLES.get(POLICE_PATROL_VEHICLE, {})
        consumption = vehicle_cfg.get("fuel_consumption", 0.1)
        fuel_needed = distance * consumption

        if selected.get("fuel", 0) < fuel_needed:

            await _send_and_delete(
                ctx,
                f"⛔ Not enough fuel for this trip. Need "
                f"{fuel_needed:.1f}, you have "
                f"{selected.get('fuel', 0):.1f}."
            )

            return

        # ==========================================================
        # START THE PATROL
        # ==========================================================

        await permissions.set_write_access(
            ctx.guild, ctx.author, origin, allowed=False
        )

        database.update_player(ctx.author.id, traveling=1)

        if passenger is not None:

            await permissions.set_write_access(
                ctx.guild, passenger, origin, allowed=False
            )

            database.update_player(passenger.id, traveling=1)

        _active_patrols[ctx.author.id] = {
            "passenger_id": passenger.id if passenger else None,
            "origin": origin,
            "destination": destination,
        }

        if passenger is not None:

            tag_line = (
                f"🚓 {ctx.author.mention} and {passenger.mention} "
                f"on patrol — heading to **{_name(destination)}**."
            )

        else:

            tag_line = (
                f"🚓 {ctx.author.mention} on patrol — heading to "
                f"**{_name(destination)}**."
            )

        try:
            await ctx.message.delete()

        except (discord.Forbidden, discord.NotFound):
            pass

        departure_msg = await ctx.send(tag_line)

        asyncio.create_task(
            _delete_after_delay(
                departure_msg, POLICE_MESSAGE_DELETE_DELAY_SECONDS
            )
        )

        duration = _travel_duration(distance)

        asyncio.create_task(
            self._complete_patrol(
                ctx.guild,
                ctx.author.id,
                distance,
                fuel_needed,
                duration,
            )
        )

    async def _complete_patrol(
        self,
        guild: discord.Guild,
        driver_id: int,
        distance: float,
        fuel_needed: float,
        duration: float,
    ) -> None:

        await asyncio.sleep(duration)

        patrol = _active_patrols.pop(driver_id, None)

        if patrol is None:
            return

        destination = patrol["destination"]
        origin = patrol["origin"]
        passenger_id = patrol["passenger_id"]

        driver = guild.get_member(driver_id)

        # ------------------------------------------------------------
        # VEHICLE — FUEL + CONDITION, via the multi-vehicle-aware
        # update so the vehicle record AND the synced flat columns
        # both stay correct.
        # ------------------------------------------------------------

        selected = database.get_selected_vehicle(driver_id)

        if selected is not None and selected.get("type") == "police":

            new_fuel = max(0.0, selected.get("fuel", 0) - fuel_needed)

            condition_lost = distance * CONDITION_LOSS_PER_KM

            new_condition = max(
                0.0, selected.get("condition", 100) - condition_lost
            )

            database.update_vehicle(
                driver_id,
                selected["id"],
                fuel=new_fuel,
                condition=new_condition,
                location=destination,
            )

        # ------------------------------------------------------------
        # DRIVER
        # ------------------------------------------------------------

        database.update_player(
            driver_id, location=destination, traveling=0
        )

        if driver is not None:

            await permissions.set_write_access(
                guild, driver, destination, allowed=True
            )

        # ------------------------------------------------------------
        # PASSENGER
        # ------------------------------------------------------------

        passenger = (
            guild.get_member(passenger_id)
            if passenger_id is not None
            else None
        )

        if passenger_id is not None:

            database.update_player(
                passenger_id, location=destination, traveling=0
            )

            if passenger is not None:

                await permissions.set_write_access(
                    guild, passenger, destination, allowed=True
                )

        # ------------------------------------------------------------
        # ARRIVAL TAG MESSAGE — "10-1"
        # ------------------------------------------------------------

        dest_channel = permissions.get_channel_for_code(guild, destination)

        if dest_channel is None:
            return

        if driver is None:
            return

        if passenger is not None:

            tag_line = (
                f"🚓 {driver.mention} and {passenger.mention} — "
                f"**10-1**, arrived at {_name(destination)}."
            )

        else:

            tag_line = (
                f"🚓 {driver.mention} — **10-1**, arrived at "
                f"{_name(destination)}."
            )

        arrival_msg = await dest_channel.send(tag_line)

        asyncio.create_task(
            _delete_after_delay(
                arrival_msg, POLICE_MESSAGE_DELETE_DELAY_SECONDS
            )
        )

    # ============================================================
    # !CHECKPOINTS
    #
    # Mount (or, run a second time at the same spot, dismount) a
    # checkpoint at the officer's current location. Requires the
    # officer's own patrol car to actually be there — you can't
    # checkpoint a location your car isn't at.
    # ============================================================

    @commands.command(name="checkpoints")
    async def checkpoints(self, ctx: commands.Context):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, OFFICER_ROLE):

            await _send_and_delete(
                ctx,
                f"⛔ You need the **{OFFICER_ROLE}** role to "
                f"mount a checkpoint."
            )

            return

        officer = database.get_or_create_player(ctx.author.id)
        location = _normalise_code(officer["location"])

        expected_channel = LOCATIONS.get(
            location, {}
        ).get("channel")

        if expected_channel is None or ctx.channel.name != expected_channel:

            await _send_and_delete(
                ctx,
                f"⛔ You need to be in "
                f"#{expected_channel or 'your current location'} "
                f"to mount a checkpoint here."
            )

            return

        # --------------------------------------------------------
        # PATROL CAR MUST BE HERE
        # --------------------------------------------------------

        selected = database.get_selected_vehicle(ctx.author.id)

        if (
            selected is None
            or selected.get("type") != "police"
            or _normalise_code(
                selected.get("location", "")
            ) != location
        ):

            await _send_and_delete(
                ctx,
                "⛔ You need your patrol car here to mount a "
                "checkpoint — check `!vehicle` / `!usevehicle`."
            )

            return

        # --------------------------------------------------------
        # MAJOR ROUTE ONLY
        # --------------------------------------------------------

        zone = LOCATIONS.get(location, {}).get("zone")

        if zone not in CHECKPOINT_ZONES:

            await _send_and_delete(
                ctx,
                f"⛔ Checkpoints can only be mounted on a major "
                f"route ({', '.join(sorted(CHECKPOINT_ZONES))})."
            )

            return

        # --------------------------------------------------------
        # MOUNT OR DISMOUNT
        # --------------------------------------------------------

        existing = _active_checkpoints.get(location)

        if existing is not None:

            if existing["officer_id"] != ctx.author.id:

                await _send_and_delete(
                    ctx,
                    "⛔ Another officer already has a checkpoint "
                    "mounted here."
                )

                return

            _active_checkpoints.pop(location, None)

            await _send_and_delete(
                ctx,
                f"🚧 Checkpoint at **{_name(location)}** has been "
                f"cleared down."
            )

            return

        _active_checkpoints[location] = {
            "officer_id": ctx.author.id,
            "guild_id": ctx.guild.id,
            "paused": {},
        }

        await _send_and_delete(
            ctx,
            f"🚧 {ctx.author.mention} has mounted a checkpoint at "
            f"**{_name(location)}**. Incoming private cars, "
            f"taxis, and dispatch motorcycles will be paused here "
            f"until cleared."
        )

    # ============================================================
    # !CLEAR
    #
    # Release one player paused at the checkpoint the officer is
    # currently standing at, and resume their journey.
    # ============================================================

    @commands.command(name="clear")
    async def clear_checkpoint(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, OFFICER_ROLE):

            await _send_and_delete(
                ctx,
                f"⛔ You need the **{OFFICER_ROLE}** role to do "
                f"this."
            )

            return

        if member is None:

            await _send_and_delete(
                ctx,
                "Usage: `!clear @player`"
            )

            return

        officer = database.get_or_create_player(ctx.author.id)
        location = _normalise_code(officer["location"])

        checkpoint = _active_checkpoints.get(location)

        if checkpoint is None:

            await _send_and_delete(
                ctx,
                "⛔ There's no checkpoint mounted here."
            )

            return

        if member.id not in checkpoint["paused"]:

            await _send_and_delete(
                ctx,
                f"⛔ {member.mention} isn't paused at this "
                f"checkpoint."
            )

            return

        checkpoint["paused"].pop(member.id, None)

        travel_cog = self.bot.get_cog("TravelCog")

        if travel_cog is not None:

            await travel_cog.resume_after_checkpoint(
                ctx.guild,
                member
            )

        await _send_and_delete(
            ctx,
            f"🚧 {ctx.author.mention} waves {member.mention} "
            f"through — **10-1**, checkpoint clear."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PoliceCog(bot))
