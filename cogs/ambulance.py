"""
Ambulance — fleet system (mirrors cogs/police.py's patrol-car
model exactly) + patient pickup/transport for unconscious
players.

FLEET MODEL (identical shape to cogs/police.py -- see its module
docstring for the full explanation):

    1. Mayor buys N ambulances: !purchaseambulance <qty>
       -> land in an UNASSIGNED POOL, persisted to
          AMBULANCE_FLEET_FILE (ambulance_fleet.json).

    2. Chief/Deputy Chief Medical Officer hand one to a specific
       Medic Staff member: !assignambulance <@medic>
       -> pulls an ambulance out of the pool, adds it to that
          medic's owned-vehicle list via database.add_vehicle(...,
          vehicle_type="ambulance").

    3. Chief/Deputy Chief Medical Officer take it back:
       !retrieveambulance <@medic>

    4. !ambulancefleet
       -> status check, same as !pdfleet.

CAPACITY (this is where it differs from a patrol car):
    - 2 crew seats: 1 driver + 1 optional ride-along medic, same
      as !patrol's optional passenger.
    - 2 SEPARATE patient slots for unconscious players. A patient
      can't act for themselves, so a crew member has to
      !loadpatient them in while the ambulance is stationary at
      the patient's location, before the ambulance departs.

TRANSPORT FLOW:
    1. Medic: !loadpatient <@player> -- ambulance must be at the
       same location as the collapsed player, and not already
       en route. Fills one of 2 patient slots.
    2. Medic: !ambulance <destination> [@ride-along medic] --
       same movement shape as !patrol (single point-to-point
       trip, not a stop-by-stop walk/bus route).
    3. On arrival, IF destination is the hospital, every patient
       onboard is automatically treated (cogs.walk.recover_player)
       and dropped off there. If the destination is anywhere
       else, patients just come along for the ride, still
       unconscious, relocated with the ambulance (a medic would
       normally only ever drive an occupied ambulance TO the
       hospital, but nothing stops a different use).
"""

import asyncio
import json
import os
import uuid

import discord
from discord.ext import commands

import database
import permissions

from routing import (
    find_route,
    NoRouteError,
)

from cogs.walk import recover_player

from config import (
    LOCATIONS,
    VEHICLES,
    RESUSCITATE_ROLE,
    AMBULANCE_ASSIGNMENT_ROLES,
    AMBULANCE_PURCHASE_ROLE,
    AMBULANCE_STATION_LOCATION,
    AMBULANCE_VEHICLE,
    AMBULANCE_FLEET_FILE,
    AMBULANCE_MESSAGE_DELETE_DELAY_SECONDS,
    AMBULANCE_PATIENT_CAPACITY,
    CONDITION_LOSS_PER_KM,
    MIN_TRAVEL_TIME_SECONDS,
    MAX_TRAVEL_TIME_SECONDS,
    TRAVEL_SECONDS_PER_KM,
)


# ================================================================
# SMALL LOCATION HELPERS (duplicated, not imported -- same
# pattern cogs/police.py uses)
# ================================================================

def _name(code: str) -> str:
    loc = LOCATIONS.get(code)
    return loc["name"] if loc else code


def _normalise_code(value: str) -> str:
    return str(value).strip().lower().replace(" ", "-")


def _travel_duration(distance: float) -> float:
    return max(
        MIN_TRAVEL_TIME_SECONDS,
        min(distance * TRAVEL_SECONDS_PER_KM, MAX_TRAVEL_TIME_SECONDS),
    )


# ================================================================
# ACTIVE RUNS + LOADED PATIENTS
#
# Module-level, same pattern as cogs/police.py's _active_patrols.
#
#     _active_runs[driver_id] = {
#         "passenger_id": int | None,
#         "origin": str,
#         "destination": str,
#         "patient_ids": [int, ...],
#     }
#
#     _loaded_patients[driver_id] = [int, ...]   # while stationary,
#                                                 # before !ambulance
#                                                 # is used to depart
# ================================================================

_active_runs: dict[int, dict] = {}

_loaded_patients: dict[int, list[int]] = {}


def _patients_on(driver_id: int) -> list[int]:
    return _loaded_patients.setdefault(driver_id, [])


async def _delete_after_delay(msg: discord.Message, delay: float) -> None:

    await asyncio.sleep(delay)

    try:
        await msg.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _send_and_delete(ctx: commands.Context, content: str) -> None:

    msg = await ctx.send(content)

    asyncio.create_task(
        _delete_after_delay(msg, AMBULANCE_MESSAGE_DELETE_DELAY_SECONDS)
    )

    try:
        await ctx.message.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


# ================================================================
# UNASSIGNED FLEET POOL (on-disk JSON, same pattern as
# cogs/police.py's police_fleet.json)
# ================================================================

def _load_pool() -> list:

    if not os.path.exists(AMBULANCE_FLEET_FILE):
        return []

    try:

        with open(AMBULANCE_FLEET_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            return []

        return data

    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _save_pool(pool: list) -> None:

    with open(AMBULANCE_FLEET_FILE, "w", encoding="utf-8") as file:
        json.dump(pool, file, indent=4)


# ================================================================
# ROLE / LOCATION CHECKS
# ================================================================

def _has_role(member: discord.Member, role_name: str) -> bool:
    return discord.utils.get(member.roles, name=role_name) is not None


def _has_any_role(member: discord.Member, role_names: list) -> bool:
    return any(_has_role(member, name) for name in role_names)


def _at_station_channel(ctx: commands.Context) -> bool:
    expected_channel = LOCATIONS[AMBULANCE_STATION_LOCATION]["channel"]
    return ctx.channel.name == expected_channel


async def _require_station_channel(ctx: commands.Context) -> bool:

    if _at_station_channel(ctx):
        return True

    expected_channel = LOCATIONS[AMBULANCE_STATION_LOCATION]["channel"]

    await _send_and_delete(
        ctx, f"\u26d4 You need to be in #{expected_channel} to do this."
    )

    return False


# ================================================================
# COG
# ================================================================

class AmbulanceCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ============================================================
    # !PURCHASEAMBULANCE <qty> -- Mayor-gated.
    # ============================================================

    @commands.command(name="purchaseambulance")
    async def purchaseambulance(
        self,
        ctx: commands.Context,
        quantity: int = 1
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, AMBULANCE_PURCHASE_ROLE):

            await _send_and_delete(
                ctx,
                f"\u26d4 Only the **{AMBULANCE_PURCHASE_ROLE}** can "
                f"purchase ambulances."
            )

            return

        if not await _require_station_channel(ctx):
            return

        try:
            quantity = int(quantity)

        except (TypeError, ValueError):

            await _send_and_delete(ctx, "\u274c Quantity must be a number.")
            return

        if quantity < 1:

            await _send_and_delete(
                ctx, "\u274c You must purchase at least one ambulance."
            )

            return

        vehicle_cfg = VEHICLES[AMBULANCE_VEHICLE]

        pool = _load_pool()

        for _ in range(quantity):

            pool.append({
                "ambulance_id": uuid.uuid4().hex[:8],
                "condition": vehicle_cfg.get("condition", 100),
            })

        _save_pool(pool)

        await ctx.send(
            f"\U0001F691 **{quantity}** ambulance(s) purchased and "
            f"added to the unassigned fleet -- **{len(pool)}** now "
            f"sitting at {LOCATIONS[AMBULANCE_STATION_LOCATION]['name']} "
            f"waiting to be assigned with `!assignambulance @medic`."
        )

    # ============================================================
    # !ASSIGNAMBULANCE <@medic> -- Chief/Deputy Chief Medical
    # Officer gated.
    # ============================================================

    @commands.command(name="assignambulance")
    async def assignambulance(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_any_role(ctx.author, AMBULANCE_ASSIGNMENT_ROLES):

            role_list = " or ".join(
                f"**{r}**" for r in AMBULANCE_ASSIGNMENT_ROLES
            )

            await _send_and_delete(
                ctx,
                f"\u26d4 You need to be {role_list} to assign an "
                f"ambulance."
            )

            return

        if not await _require_station_channel(ctx):
            return

        if member is None:

            await _send_and_delete(ctx, "Usage: `!assignambulance <@medic>`")
            return

        if not _has_role(member, RESUSCITATE_ROLE):

            await _send_and_delete(
                ctx,
                f"\u26d4 {member.mention} isn't **{RESUSCITATE_ROLE}** -- "
                f"ambulances can only be assigned to medics."
            )

            return

        already_assigned = next(
            (
                v for v in database.get_vehicles(member.id)
                if v.get("type") == "ambulance"
            ),
            None,
        )

        if already_assigned is not None:

            await _send_and_delete(
                ctx,
                f"{member.mention} already has an ambulance assigned -- "
                f"use `!retrieveambulance @medic` first to swap it."
            )

            return

        pool = _load_pool()

        if not pool:

            await _send_and_delete(
                ctx,
                "\u26d4 There are no unassigned ambulances right now -- "
                "have the Mayor `!purchaseambulance` more."
            )

            return

        ambulance = pool.pop(0)
        _save_pool(pool)

        vehicle_cfg = VEHICLES[AMBULANCE_VEHICLE]

        database.add_vehicle(
            member.id,
            name=AMBULANCE_VEHICLE,
            vehicle_type="ambulance",
            location=AMBULANCE_STATION_LOCATION,
            condition=ambulance.get(
                "condition", vehicle_cfg.get("condition", 100)
            ),
            fuel=vehicle_cfg["fuel_capacity"],
            select=True,
        )

        await ctx.send(
            f"\U0001F691 {member.mention} has been assigned ambulance "
            f"**#{ambulance['ambulance_id']}** -- check `!vehicle` any "
            f"time. **{len(pool)}** unassigned ambulance(s) remain."
        )

    # ============================================================
    # !RETRIEVEAMBULANCE <@medic>
    # ============================================================

    @commands.command(name="retrieveambulance")
    async def retrieveambulance(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_any_role(ctx.author, AMBULANCE_ASSIGNMENT_ROLES):

            role_list = " or ".join(
                f"**{r}**" for r in AMBULANCE_ASSIGNMENT_ROLES
            )

            await _send_and_delete(
                ctx,
                f"\u26d4 You need to be {role_list} to retrieve an "
                f"ambulance."
            )

            return

        if not await _require_station_channel(ctx):
            return

        if member is None:

            await _send_and_delete(
                ctx, "Usage: `!retrieveambulance <@medic>`"
            )

            return

        assigned = next(
            (
                v for v in database.get_vehicles(member.id)
                if v.get("type") == "ambulance"
            ),
            None,
        )

        if assigned is None:

            await _send_and_delete(
                ctx,
                f"{member.mention} doesn't currently have an ambulance "
                f"assigned."
            )

            return

        if member.id in _active_runs:

            await _send_and_delete(
                ctx,
                f"\u26d4 {member.mention} is currently out on a run -- "
                f"wait for them to arrive before retrieving the "
                f"ambulance."
            )

            return

        database.remove_vehicle(member.id, assigned["id"])

        pool = _load_pool()

        pool.append({
            "ambulance_id": assigned.get("id", uuid.uuid4().hex[:8]),
            "condition": assigned.get("condition", 100),
        })

        _save_pool(pool)

        _loaded_patients.pop(member.id, None)

        await ctx.send(
            f"\U0001F691 {member.mention}'s ambulance has been "
            f"retrieved and returned to the unassigned fleet -- "
            f"**{len(pool)}** now waiting to be reassigned."
        )

    # ============================================================
    # !AMBULANCEFLEET -- status check, no gating.
    # ============================================================

    @commands.command(name="ambulancefleet")
    async def ambulancefleet(self, ctx: commands.Context):

        pool = _load_pool()

        assigned_count = 0

        for member in ctx.guild.members:

            if member.bot:
                continue

            has_ambulance = any(
                v.get("type") == "ambulance"
                for v in database.get_vehicles(member.id)
            )

            if has_ambulance:
                assigned_count += 1

        total = len(pool) + assigned_count

        await ctx.send(
            f"\U0001F691 **Ambulance Fleet Status**\n\n"
            f"Total ambulances purchased: **{total}**\n"
            f"Currently assigned to medics: **{assigned_count}**\n"
            f"Unassigned (in the pool): **{len(pool)}**"
        )

    # ============================================================
    # !LOADPATIENT <@player> -- fills one of 2 patient slots.
    # Ambulance must be stationary (not mid-run) at the patient's
    # exact current location.
    # ============================================================

    @commands.command(name="loadpatient")
    async def loadpatient(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, RESUSCITATE_ROLE):

            await _send_and_delete(
                ctx,
                f"\u26d4 You need the **{RESUSCITATE_ROLE}** role to "
                f"load a patient."
            )

            return

        if member is None:

            await _send_and_delete(ctx, "Usage: `!loadpatient <@player>`")
            return

        selected = database.get_selected_vehicle(ctx.author.id)

        if selected is None or selected.get("type") != "ambulance":

            await _send_and_delete(
                ctx,
                "\u26d4 You need an ambulance selected to load a "
                "patient -- check `!vehicle` / `!usevehicle`."
            )

            return

        if ctx.author.id in _active_runs:

            await _send_and_delete(
                ctx, "\u26d4 You can't load a patient while en route."
            )

            return

        if not database.is_unconscious(member.id):

            await _send_and_delete(
                ctx, f"{member.mention} isn't unconscious."
            )

            return

        target_player = database.get_or_create_player(member.id)
        ambulance_location = _normalise_code(selected.get("location") or "")

        if _normalise_code(target_player["location"]) != ambulance_location:

            await _send_and_delete(
                ctx,
                f"\u26d4 {member.mention} is at "
                f"**{_name(target_player['location'])}**, but your "
                f"ambulance is at **{_name(ambulance_location)}**."
            )

            return

        patients = _patients_on(ctx.author.id)

        if member.id in patients:

            await _send_and_delete(
                ctx, f"{member.mention} is already loaded."
            )

            return

        if len(patients) >= AMBULANCE_PATIENT_CAPACITY:

            await _send_and_delete(
                ctx,
                f"\u26d4 Your ambulance already has "
                f"{AMBULANCE_PATIENT_CAPACITY} patients aboard -- full."
            )

            return

        patients.append(member.id)

        await ctx.send(
            f"\U0001F691 {member.mention} has been loaded aboard the "
            f"ambulance ({len(patients)}/{AMBULANCE_PATIENT_CAPACITY})."
        )

    # ============================================================
    # !AMBULANCE <destination> [@ride-along medic] -- the medic's
    # !patrol equivalent. Single point-to-point trip carrying the
    # driver, an optional ride-along medic, and however many
    # patients are currently loaded.
    # ============================================================

    @commands.command(name="ambulance")
    async def ambulance(
        self,
        ctx: commands.Context,
        destination: str = None,
        passenger: discord.Member = None
    ):

        if not isinstance(ctx.author, discord.Member):
            return

        if not _has_role(ctx.author, RESUSCITATE_ROLE):

            await _send_and_delete(
                ctx,
                f"\u26d4 You need the **{RESUSCITATE_ROLE}** role to "
                f"drive an ambulance."
            )

            return

        if not destination:

            await _send_and_delete(
                ctx,
                "Usage: `!ambulance <destination> [@ride-along medic]`"
            )

            return

        destination = _normalise_code(destination)

        driver = database.get_or_create_player(ctx.author.id)

        origin = _normalise_code(driver["location"])

        if driver["traveling"] or ctx.author.id in _active_runs:

            await _send_and_delete(ctx, "You're already on the move.")
            return

        selected = database.get_selected_vehicle(ctx.author.id)

        if selected is None or selected.get("type") != "ambulance":

            await _send_and_delete(
                ctx,
                "\u26d4 You need an ambulance selected to `!ambulance` -- "
                "check `!vehicle` / `!usevehicle`."
            )

            return

        if destination not in LOCATIONS:

            await _send_and_delete(ctx, "\u26d4 Unknown destination.")
            return

        if destination == origin:

            await _send_and_delete(ctx, "You're already there.")
            return

        try:
            path, distance = find_route(origin, destination)

        except NoRouteError:

            await _send_and_delete(
                ctx,
                f"No road route exists between {_name(origin)} and "
                f"{_name(destination)}."
            )

            return

        if passenger is not None:

            if passenger.id == ctx.author.id:

                await _send_and_delete(
                    ctx, "\u26d4 You can't ride along with yourself."
                )

                return

            if not _has_role(passenger, RESUSCITATE_ROLE):

                await _send_and_delete(
                    ctx,
                    f"\u26d4 {passenger.mention} isn't "
                    f"**{RESUSCITATE_ROLE}** -- only medics can ride "
                    f"along."
                )

                return

            passenger_player = database.get_or_create_player(passenger.id)

            if (
                _normalise_code(passenger_player["location"]) != origin
                or passenger_player["traveling"]
                or passenger.id in _active_runs
            ):

                await _send_and_delete(
                    ctx,
                    f"\u26d4 {passenger.mention} needs to be here at "
                    f"{_name(origin)}, and not already traveling, to "
                    f"ride along."
                )

                return

        vehicle_cfg = VEHICLES.get(AMBULANCE_VEHICLE, {})
        consumption = vehicle_cfg.get("fuel_consumption", 0.1)
        fuel_needed = distance * consumption

        if selected.get("fuel", 0) < fuel_needed:

            await _send_and_delete(
                ctx,
                f"\u26d4 Not enough fuel for this trip. Need "
                f"{fuel_needed:.1f}, you have "
                f"{selected.get('fuel', 0):.1f}."
            )

            return

        # --------------------------------------------------------
        # DEPART
        # --------------------------------------------------------

        patients = list(_patients_on(ctx.author.id))

        await permissions.set_write_access(
            ctx.guild, ctx.author, origin, allowed=False
        )

        database.update_player(ctx.author.id, traveling=1)

        if passenger is not None:

            await permissions.set_write_access(
                ctx.guild, passenger, origin, allowed=False
            )

            database.update_player(passenger.id, traveling=1)

        _active_runs[ctx.author.id] = {
            "passenger_id": passenger.id if passenger else None,
            "origin": origin,
            "destination": destination,
            "patient_ids": patients,
        }

        _loaded_patients[ctx.author.id] = []

        if passenger is not None:

            tag_line = (
                f"\U0001F691 {ctx.author.mention} and "
                f"{passenger.mention} en route to "
                f"**{_name(destination)}**"
            )

        else:

            tag_line = (
                f"\U0001F691 {ctx.author.mention} en route to "
                f"**{_name(destination)}**"
            )

        if patients:
            tag_line += f" with **{len(patients)}** patient(s) aboard."
        else:
            tag_line += "."

        try:
            await ctx.message.delete()

        except (discord.Forbidden, discord.NotFound):
            pass

        departure_msg = await ctx.send(tag_line)

        asyncio.create_task(
            _delete_after_delay(
                departure_msg, AMBULANCE_MESSAGE_DELETE_DELAY_SECONDS
            )
        )

        duration = _travel_duration(distance)

        asyncio.create_task(
            self._complete_run(
                ctx.guild, ctx.author.id, distance, fuel_needed, duration,
            )
        )

    # ============================================================
    # COMPLETE RUN
    # ============================================================

    async def _complete_run(
        self,
        guild: discord.Guild,
        driver_id: int,
        distance: float,
        fuel_needed: float,
        duration: float,
    ) -> None:

        await asyncio.sleep(duration)

        run = _active_runs.pop(driver_id, None)

        if run is None:
            return

        destination = run["destination"]
        origin = run["origin"]
        passenger_id = run["passenger_id"]
        patient_ids = run["patient_ids"]

        driver = guild.get_member(driver_id)

        # ------------------------------------------------------------
        # VEHICLE -- FUEL + CONDITION
        # ------------------------------------------------------------

        selected = database.get_selected_vehicle(driver_id)

        if selected is not None and selected.get("type") == "ambulance":

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
        # DRIVER + PASSENGER
        # ------------------------------------------------------------

        database.update_player(driver_id, location=destination, traveling=0)

        if driver is not None:

            await permissions.set_write_access(
                guild, driver, destination, allowed=True
            )

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
        # PATIENTS
        #
        # Only actually TREATED if the ambulance arrived at the
        # hospital. Anywhere else, they're relocated but stay
        # unconscious (still frozen, still blocked from every
        # command) -- a medic would normally only ever drive an
        # occupied ambulance to the hospital.
        # ------------------------------------------------------------

        at_hospital = destination == AMBULANCE_STATION_LOCATION

        for patient_id in patient_ids:

            patient = guild.get_member(patient_id)

            if patient is None:
                continue

            if at_hospital:

                await recover_player(
                    guild,
                    patient,
                    new_location=destination,
                    announce_channel_code=None,
                    reason="",
                )

            else:

                database.update_player(
                    patient_id, location=destination, traveling=0
                )

        # ------------------------------------------------------------
        # ARRIVAL MESSAGE
        # ------------------------------------------------------------

        dest_channel = permissions.get_channel_for_code(guild, destination)

        if dest_channel is None or driver is None:
            return

        if passenger is not None:

            tag_line = (
                f"\U0001F691 {driver.mention} and {passenger.mention} "
                f"have arrived at {_name(destination)}."
            )

        else:

            tag_line = (
                f"\U0001F691 {driver.mention} has arrived at "
                f"{_name(destination)}."
            )

        if patient_ids and at_hospital:

            mentions = ", ".join(
                f"<@{pid}>" for pid in patient_ids
            )

            tag_line += (
                f" {len(patient_ids)} patient(s) ({mentions}) have "
                f"been treated and admitted."
            )

        arrival_msg = await dest_channel.send(tag_line)

        asyncio.create_task(
            _delete_after_delay(
                arrival_msg, AMBULANCE_MESSAGE_DELETE_DELAY_SECONDS
            )
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AmbulanceCog(bot))
