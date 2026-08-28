"""
Dispatch system — delivery riders on bicycles (standard) and
motorcycles (premium), gotten at the same Taxi Company location
as taxi drivers.

Modeled closely on cogs/taxi.py's broadcast-to-many-then-first-
to-accept matching engine, with one structural difference: a
taxi driver only ever holds ONE ride at a time, but a dispatch
rider can hold up to DISPATCH_MAX_ORDERS (3) delivery orders at
once, possibly from different senders going to different
destinations. Because of that:

  - Orders are keyed by their own order_id (not by sender_id
    like taxi's booker_id), since multiple senders can have
    open orders broadcasting at the same time.
  - A rider's accepted orders are collected in a list
    (_confirmed_orders[rider_id]) instead of a single dict.
  - Combining several accepted orders into one delivery run
    reuses carpool.py's build_multi_leg_route() (nearest-stop-
    first ordering) — a delivery order IS a "stop" in exactly
    the shape that function already expects.
  - Unlike a taxi ride or carpool passenger, an order has no
    person to lock in/out of channels — the sender and
    recipient never travel. Only the rider (driver) does.

FLOW:

    1. Rider: !becomedispatchrider standard|premium
       -> must hold Campus Resident or Student (kept, not
          removed) and be at the Taxi Company.
       -> handed a free Bicycle/Motorcycle via the multi-vehicle
          ownership system (database.add_vehicle), and granted
          the Dispatch Rider role on top of their existing role.

    2. Rider: !dispatchstart / !dispatchstop
       -> same online/offline toggle as !taxistart/!taxistop.

    3. Sender: !orderdelivery <standard|premium> <destination>
       -> broadcasts to every online, under-capacity, reachable
          rider of that tier, each in their own current-location
          channel.

    4. Rider: !dispatchaccept <order_id> / !dispatchdecline <order_id>
       -> order ids are shown in the ping since a rider can have
          more than one live ping at once (up to their remaining
          capacity), unlike taxi's single-ping !taxiaccept.

    5. Rider: !dispatchpickup (no args)
       -> picks up every accepted-but-not-yet-boarded order whose
          origin matches the rider's CURRENT location/channel.
          A rider can be carrying orders picked up at different
          times/places as long as they're boarded before !drive.

    6. Rider: !drive <anywhere>
       -> travel.py (not yet wired — see take_confirmed_orders()
          and build_delivery_route() below) overrides the trip
          with a combined nearest-stop-first route through every
          boarded order's destination, calling
          handle_delivery_arrival() at each stop the same way
          carpool.py's handle_arrival() drops off passengers.

This file does NOT change how tolls/fuel are calculated — that's
travel.py's job. DISPATCH_FUEL_EXEMPT_VEHICLES /
DISPATCH_TOLL_EXEMPT_VEHICLES in config.py exist so travel.py can
skip fuel/toll entirely for a Bicycle once it's wired in.
"""

import asyncio
import uuid

import discord
from discord.ext import commands

import database
import permissions

from routing import (
    find_route,
    NoRouteError,
)

from cogs.carpool import build_multi_leg_route

from config import (
    LOCATIONS,
    VEHICLES,
    DISPATCH_ELIGIBLE_ROLES,
    DISPATCH_RIDER_ROLE,
    TAXI_REGISTRATION_CODE,
    TAXI_DRIVER_ROLE,
    DISPATCH_COMPANY_VEHICLE,
    DISPATCH_MAX_ORDERS,
    DISPATCH_BASE_FARE_PER_KM,
    DISPATCH_TIER_MULTIPLIER,
    DISPATCH_MIN_FARE,
    DISPATCH_RIDER_CUT,
    DISPATCH_BICYCLE_TIME_MULTIPLIER,
    DISPATCH_FUEL_EXEMPT_VEHICLES,
    DISPATCH_REQUEST_TIMEOUT_SECONDS,
    DISPATCH_QUEUE_TIMEOUT_SECONDS,
    DISPATCH_MESSAGE_DELETE_DELAY_SECONDS,
    COMMERCIAL_VEHICLE_MANAGER_ROLE,
    MIN_TRAVEL_TIME_SECONDS,
    MAX_TRAVEL_TIME_SECONDS,
    TRAVEL_SECONDS_PER_KM,
)


# ================================================================
# MODULE-LEVEL STATE
#
# Module-level (not on the Cog instance) so travel.py can call the
# helper functions below without needing this Cog's instance.
#
#     _open_orders[order_id] = {
#         "order_id":    str,
#         "sender_id":   int,
#         "rider_ids":   [],           # unused, kept for shape
#                                      # parity with build_multi_leg_route
#         "destination": str,
#         "origin":      str,
#         "tier":        str,
#         "fare":        int,
#         "guild_id":    int,
#         "channel_id":  int,          # sender's origin channel
#         "notified": {
#             rider_id: {"channel_id": int, "message_id": int | None},
#             ...
#         },
#         "timeout_task": asyncio.Task,
#     }
#
#     _rider_order_notice[rider_id] = {order_id, order_id, ...}
#         # every order this rider currently has a live ping for —
#         # a rider can hold several pings at once (up to their
#         # remaining capacity), unlike taxi's single-ping driver.
#
#     _confirmed_orders[rider_id] = [
#         {"order_id", "sender_id", "origin", "destination",
#          "tier", "fare", "boarded": bool},
#         ...
#     ]                                # up to DISPATCH_MAX_ORDERS
#
#     _sender_active_order[sender_id] = order_id
#         # one open (pending or queued) order per sender at a time
#
#     _queue[tier] = [ entry, entry, ... ]   # FIFO
# ================================================================

_open_orders: dict[str, dict] = {}
_rider_order_notice: dict[int, set] = {}
_confirmed_orders: dict[int, list] = {}
_sender_active_order: dict[int, str] = {}
_queue: dict[str, list[dict]] = {}


# ================================================================
# SMALL HELPERS
#
# Duplicated (not imported) from travel.py/taxi.py on purpose, to
# avoid a circular import.
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


def _normalise_tier(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip().lower()

    return value if value in DISPATCH_TIER_MULTIPLIER else None


def _is_road_destination(code: str) -> bool:
    location = LOCATIONS.get(code)

    if location is None:
        return False

    return location.get("zone") != "overseas"


def _has_location_access(
    member: discord.Member,
    code: str
) -> bool:

    location = LOCATIONS.get(code)

    if location is None:
        return False

    required_roles = location.get("roles")

    if not required_roles:
        return True

    member_roles = {
        role.name.strip().lower()
        for role in member.roles
    }

    for required_role in required_roles:

        if required_role.strip().lower() in member_roles:
            return True

    return False


def _calculate_fare(distance_km: float, tier: str) -> int:

    fare = (
        distance_km
        * DISPATCH_BASE_FARE_PER_KM
        * DISPATCH_TIER_MULTIPLIER[tier]
    )

    return max(DISPATCH_MIN_FARE[tier], round(fare))


def _is_bicycle(vehicle_name: str | None) -> bool:
    return vehicle_name in DISPATCH_FUEL_EXEMPT_VEHICLES


def _travel_duration(distance_km: float, tier: str) -> float:

    base = max(
        MIN_TRAVEL_TIME_SECONDS,
        min(
            distance_km * TRAVEL_SECONDS_PER_KM,
            MAX_TRAVEL_TIME_SECONDS,
        ),
    )

    if tier == "standard":
        return base * DISPATCH_BICYCLE_TIME_MULTIPLIER

    return base


def _format_eta(seconds: int) -> str:

    seconds = max(0, round(seconds))

    minutes, secs = divmod(seconds, 60)

    if minutes and secs:
        return f"{minutes}m {secs}s"

    if minutes:
        return f"{minutes}m"

    return f"{secs}s"


async def _send_and_delete(
    ctx: commands.Context,
    content: str
) -> None:

    msg = await ctx.send(content)

    asyncio.create_task(
        _delete_after_delay(msg, DISPATCH_MESSAGE_DELETE_DELAY_SECONDS)
    )

    try:
        await ctx.message.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _delete_after_delay(
    msg: discord.Message,
    delay: float
) -> None:

    await asyncio.sleep(delay)

    try:
        await msg.delete()

    except (discord.Forbidden, discord.NotFound):
        pass


async def _send_and_delete_channel(
    channel: discord.abc.Messageable | None,
    content: str
) -> None:

    if channel is None:
        return

    try:
        msg = await channel.send(content)

    except (discord.Forbidden, discord.NotFound):
        return

    asyncio.create_task(
        _delete_after_delay(msg, DISPATCH_MESSAGE_DELETE_DELAY_SECONDS)
    )


async def _delete_rider_ping(
    guild: discord.Guild,
    notice: dict
) -> None:

    channel_id = notice.get("channel_id")
    message_id = notice.get("message_id")

    if channel_id is None or message_id is None:
        return

    channel = guild.get_channel(channel_id)

    if channel is None:
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.delete()

    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def _clear_all_rider_pings(
    guild: discord.Guild,
    entry: dict,
    except_rider_id: int | None = None
) -> None:

    for rider_id, notice in list(entry.get("notified", {}).items()):

        if rider_id == except_rider_id:
            continue

        await _delete_rider_ping(guild, notice)

        notices = _rider_order_notice.get(rider_id)

        if notices is not None:
            notices.discard(entry["order_id"])

            if not notices:
                _rider_order_notice.pop(rider_id, None)


def _rider_current_location(rider_id: int) -> str | None:

    player = database.get_player(rider_id)

    if player is None:
        return None

    if player["traveling"]:
        return None

    return _normalise_code(player["location"])


def _rider_capacity_used(rider_id: int) -> int:
    return len(_confirmed_orders.get(rider_id, []))


def _find_available_riders(
    guild: discord.Guild,
    tier: str,
    origin: str
) -> list[tuple[int, discord.TextChannel, float]]:

    """
    Every online, reachable, NOT-already-full dispatch rider of
    `tier`. Unlike taxi's driver matching, a rider already
    holding 1-2 orders is still eligible as long as they have a
    free slot — only riders at DISPATCH_MAX_ORDERS capacity are
    skipped.
    """

    candidates = []

    for row in database.get_online_dispatch_riders(tier):

        rider_id = int(row["user_id"])

        if _rider_capacity_used(rider_id) >= DISPATCH_MAX_ORDERS:
            continue

        rider_location = _rider_current_location(rider_id)

        if rider_location is None:
            continue

        rider_channel = permissions.get_channel_for_code(
            guild, rider_location
        )

        if rider_channel is None:
            continue

        try:
            _, distance = find_route(origin, rider_location)

        except NoRouteError:
            continue

        candidates.append((rider_id, rider_channel, distance))

    candidates.sort(key=lambda c: c[2])

    return candidates


def _find_active_order(sender_id: int):
    """
    Returns ("pending", entry) / ("queued", entry) / (None, None)
    for this sender's one open order, wherever it currently lives.
    """

    order_id = _sender_active_order.get(sender_id)

    if order_id is None:
        return (None, None)

    entry = _open_orders.get(order_id)

    if entry is not None:
        return ("pending", entry)

    for entries in _queue.values():

        for queued_entry in entries:

            if queued_entry["order_id"] == order_id:
                return ("queued", queued_entry)

    return (None, None)


# ================================================================
# DISPATCH / QUEUE ENGINE
# ================================================================

async def _broadcast_to_riders(
    guild: discord.Guild,
    entry: dict,
    riders: list[tuple[int, discord.TextChannel, float]],
    prefix: str = ""
) -> None:

    timeout_task = asyncio.create_task(
        _expire_order(guild, entry["order_id"])
    )

    entry["notified"] = {}
    entry["timeout_task"] = timeout_task
    entry["guild_id"] = guild.id

    _open_orders[entry["order_id"]] = entry

    for rider_id, rider_channel, distance in riders:

        _rider_order_notice.setdefault(rider_id, set()).add(
            entry["order_id"]
        )

        rider_member = guild.get_member(rider_id)
        rider_mention = (
            rider_member.mention
            if rider_member
            else f"<@{rider_id}>"
        )

        eta_text = _format_eta(
            round(_travel_duration(distance, entry["tier"]))
        )
        message_id = None

        try:
            msg = await rider_channel.send(
                f"📦 New **{entry['tier']}** delivery order! "
                f"(id: `{entry['order_id']}`)\n"
                f"Pickup: **{_name(entry['origin'])}** "
                f"(about **{eta_text}** away)\n"
                f"Drop-off: **{_name(entry['destination'])}**\n"
                f"Payout: ₦{round(entry['fare'] * DISPATCH_RIDER_CUT):,}\n\n"
                f"{rider_mention}, use `!dispatchaccept "
                f"{entry['order_id']}` or `!dispatchdecline "
                f"{entry['order_id']}` — whoever accepts first "
                f"gets it."
            )

            message_id = msg.id

        except (discord.Forbidden, discord.NotFound):
            pass

        entry["notified"][rider_id] = {
            "channel_id": rider_channel.id,
            "message_id": message_id,
        }

    sender_channel = guild.get_channel(entry["channel_id"])
    rider_count = len(riders)
    rider_word = "rider" if rider_count == 1 else "riders"

    await _send_and_delete_channel(
        sender_channel,
        f"{prefix}"
        f"📦 Delivery order sent to {rider_count} nearby "
        f"**{entry['tier']}** dispatch {rider_word}.\n"
        f"Drop-off: **{_name(entry['destination'])}**\n"
        f"Fare: ₦{entry['fare']:,}\n"
        f"Order id: `{entry['order_id']}` "
        f"(use with `!cancelorder`)."
    )


async def _queue_entry(guild: discord.Guild, entry: dict, prefix: str = "") -> None:

    entry["timeout_task"] = asyncio.create_task(
        _expire_queue_entry(guild, entry)
    )

    _queue.setdefault(entry["tier"], []).append(entry)

    channel = guild.get_channel(entry["channel_id"])

    await _send_and_delete_channel(
        channel,
        f"{prefix}"
        f"📦 No **{entry['tier']}** dispatch riders are free "
        f"right now. Your order (`{entry['order_id']}`) has "
        f"been queued and will be auto-matched with the next "
        f"available rider (up to "
        f"{_format_eta(DISPATCH_QUEUE_TIMEOUT_SECONDS)} wait).\n"
        f"Use `!cancelorder` to withdraw it."
    )


async def _dispatch_or_queue(
    guild: discord.Guild,
    entry: dict,
    prefix: str = ""
) -> None:

    riders = _find_available_riders(
        guild, entry["tier"], entry["origin"]
    )

    if riders:

        await _broadcast_to_riders(
            guild, entry, riders, prefix=prefix
        )

        return

    await _queue_entry(guild, entry, prefix=prefix)


async def _try_dispatch_queue(guild: discord.Guild, tier: str) -> None:
    """
    Called any time a rider of `tier` becomes free (goes online,
    finishes a delivery, declines, times out, or cancels an
    unboarded order). Matches as many queued orders as possible,
    FIFO, as long as SOME rider still has a free slot.
    """

    queue = _queue.get(tier)

    if not queue:
        return

    while queue:

        entry = queue[0]

        riders = _find_available_riders(guild, tier, entry["origin"])

        if not riders:
            break

        queue.pop(0)

        entry["timeout_task"].cancel()

        await _broadcast_to_riders(
            guild,
            entry,
            riders,
            prefix="🔔 A rider is now free for your queued order!\n\n"
        )


async def _expire_queue_entry(guild: discord.Guild, entry: dict) -> None:

    await asyncio.sleep(DISPATCH_QUEUE_TIMEOUT_SECONDS)

    queue = _queue.get(entry["tier"])

    if queue is None or entry not in queue:
        return

    queue.remove(entry)

    _sender_active_order.pop(entry["sender_id"], None)

    channel = guild.get_channel(entry["channel_id"])

    await _send_and_delete_channel(
        channel,
        f"⌛ No **{entry['tier']}** dispatch rider became "
        f"available in time — your queued order "
        f"(`{entry['order_id']}`) was cancelled. Try "
        f"`!orderdelivery` again."
    )


async def _expire_order(
    guild: discord.Guild,
    order_id: str
) -> None:

    await asyncio.sleep(DISPATCH_REQUEST_TIMEOUT_SECONDS)

    entry = _open_orders.get(order_id)

    if entry is None:
        return

    _open_orders.pop(order_id, None)

    await _clear_all_rider_pings(guild, entry)

    channel = guild.get_channel(entry["channel_id"])

    await _send_and_delete_channel(
        channel,
        f"⌛ Delivery order `{order_id}` expired — no rider "
        f"accepted in time. Looking for another rider..."
    )

    await _dispatch_or_queue(guild, entry)


# ================================================================
# FUNCTIONS CALLED FROM travel.py (not yet wired in — see the
# module docstring)
# ================================================================

def peek_confirmed_orders(rider_id: int) -> list | None:
    """
    Look at this rider's BOARDED orders without popping them.
    Mirrors taxi.peek_confirmed_ride — used by travel.py early in
    !drive to know a dispatch trip is happening at all before any
    of the normal destination checks run.

    Returns None if the rider has no boarded orders yet.
    """

    orders = [
        o for o in _confirmed_orders.get(rider_id, [])
        if o["boarded"]
    ]

    return orders or None


def take_confirmed_orders(rider_id: int) -> list:
    """
    Pop and return every BOARDED order for this rider. Any
    accepted-but-not-yet-picked-up order is left behind in
    _confirmed_orders (the rider can still !dispatchpickup it
    later, or run a second !drive for it).

    Called once, right when !drive actually starts a trip.
    Returns [] if the rider has nothing boarded — in that case
    !drive must behave as a completely normal, unrestricted solo
    trip.
    """

    remaining = _confirmed_orders.get(rider_id, [])

    boarded = [o for o in remaining if o["boarded"]]
    not_boarded = [o for o in remaining if not o["boarded"]]

    if not_boarded:
        _confirmed_orders[rider_id] = not_boarded
    else:
        _confirmed_orders.pop(rider_id, None)

    return boarded


def requeue_orders(rider_id: int, orders: list) -> None:
    """
    Put boarded orders back if !drive fails AFTER already having
    popped them (e.g. not enough fuel), so the rider doesn't lose
    the deliveries.
    """

    if not orders:
        return

    existing = _confirmed_orders.setdefault(rider_id, [])

    _confirmed_orders[rider_id] = orders + existing


def build_delivery_route(origin: str, orders: list):
    """
    Turn a rider's boarded orders into one combined path, reusing
    carpool's nearest-stop-first router — an order's
    {"user_id", "destination"} shape (via _as_stop below) is
    exactly what build_multi_leg_route already expects.

    Returns (full_path, total_distance, pending_tolls, stop_markers)
    exactly like carpool.build_multi_leg_route. `stop_markers`
    entries carry the FULL order dict under "order" so
    handle_delivery_arrival() can charge/pay/announce correctly.

    Raises NoRouteError if any leg has no valid road route.
    """

    stops = orders[:-1]
    final_order = orders[-1]

    full_path, total_distance, pending_tolls, raw_markers = (
        build_multi_leg_route(
            origin,
            [
                {"user_id": o["order_id"], "destination": o["destination"]}
                for o in stops
            ],
            final_order["destination"],
        )
    )

    by_order_id = {o["order_id"]: o for o in stops}

    stop_markers = [
        {
            "index": marker["index"],
            "order": by_order_id[marker["user_id"]],
        }
        for marker in raw_markers
    ]

    stop_markers.append({
        "index": len(full_path) - 1,
        "order": final_order,
    })

    return full_path, total_distance, pending_tolls, stop_markers


async def handle_delivery_arrival(
    guild: discord.Guild,
    rider_id: int,
    journey: dict,
    current_index: int
) -> str | None:
    """
    Called by travel.py's tick loop every time current_index
    advances — same pattern as carpool.handle_arrival(), except
    this one also needs the RIDER's id (unlike carpool, which
    never pays anyone) to credit their cut, same as
    taxi.handle_taxi_arrival() takes `driver` explicitly.

    No-op unless the rider has just reached one (or more) order
    drop-off points. Charges the sender, pays the rider their
    25% cut (remaining 75% vanishes to the company, same as
    repair.py's MECHANIC_CUT), and announces completion in the
    order's origin channel. Cargo has no travel state of its own
    — only the rider's own location/traveling flag is touched by
    travel.py itself.

    Returns a short summary line to append to the rider's arrival
    message, or None.
    """

    stops = journey.get("dispatch_stops")

    if not stops:
        return None

    markers = [
        m for m in stops if m["index"] == current_index
    ]

    if not markers:
        return None

    for marker in markers:
        stops.remove(marker)

    summaries = []

    for marker in markers:

        order = marker["order"]
        fare = order["fare"]

        sender = database.get_player(order["sender_id"])

        if sender is not None:

            database.update_player(
                order["sender_id"],
                balance=max(0, sender["balance"] - fare)
            )

        rider_payout = round(fare * DISPATCH_RIDER_CUT)

        rider_row = database.get_player(rider_id)

        if rider_row is not None:

            database.update_player(
                rider_id,
                balance=rider_row["balance"] + rider_payout
            )

        summaries.append(
            f"📦 Delivered to **{_name(order['destination'])}** "
            f"for <@{order['sender_id']}> — "
            f"₦{fare:,} (rider payout ₦{rider_payout:,})."
        )

        origin_channel = permissions.get_channel_for_code(
            guild, order["origin"]
        )

        await _send_and_delete_channel(
            origin_channel,
            f"📦 <@{order['sender_id']}>, your delivery to "
            f"**{_name(order['destination'])}** has arrived!"
        )

    return "\n".join(summaries)


# ================================================================
# COG
# ================================================================

class DispatchCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ============================================================
    # !BECOMEDISPATCHRIDER
    # ============================================================

    @commands.command(name="becomedispatchrider")
    async def becomedispatchrider(
        self,
        ctx: commands.Context,
        tier: str = None
    ):

        tier = _normalise_tier(tier)

        if tier is None:

            await _send_and_delete(
                ctx,
                "Usage: `!becomedispatchrider standard` "
                "(bicycle) or `!becomedispatchrider premium` "
                "(motorcycle)"
            )

            return

        player = database.get_or_create_player(ctx.author.id)

        origin = _normalise_code(player["location"])

        if origin != TAXI_REGISTRATION_CODE:

            await _send_and_delete(
                ctx,
                f"You need to be at "
                f"{_name(TAXI_REGISTRATION_CODE)} to register "
                f"as a dispatch rider."
            )

            return

        expected_channel = LOCATIONS[
            TAXI_REGISTRATION_CODE
        ]["channel"]

        if ctx.channel.name != expected_channel:

            await _send_and_delete(
                ctx,
                f"You need to be in #{expected_channel} to "
                f"register as a dispatch rider."
            )

            return

        eligible_role = next(
            (
                role for role in ctx.author.roles
                if role.name in DISPATCH_ELIGIBLE_ROLES
            ),
            None,
        )

        if eligible_role is None:

            role_list = " or ".join(
                f"**{r}**" for r in DISPATCH_ELIGIBLE_ROLES
            )

            await _send_and_delete(
                ctx,
                f"⛔ You need the {role_list} role to become a "
                f"dispatch rider."
            )

            return

        existing = database.get_dispatch_rider(ctx.author.id)

        if existing is not None:

            await _send_and_delete(
                ctx,
                f"You're already registered as a "
                f"**{existing['tier']}** dispatch rider."
            )

            return

        database.register_dispatch_rider(ctx.author.id, tier)

        # --------------------------------------------------------
        # HAND OVER THE COMPANY VEHICLE — via the multi-vehicle
        # ownership system, so a rider keeps any personal vehicle
        # they already own and can switch between them with
        # !usevehicle. Does NOT remove the eligible role (Campus
        # Resident/Student stay).
        # --------------------------------------------------------

        company_vehicle_name = DISPATCH_COMPANY_VEHICLE[tier]
        vehicle_cfg = VEHICLES[company_vehicle_name]

        database.add_vehicle(
            ctx.author.id,
            name=company_vehicle_name,
            vehicle_type=(
                "dispatch_bicycle"
                if tier == "standard"
                else "dispatch_motorcycle"
            ),
            location=TAXI_REGISTRATION_CODE,
            condition=vehicle_cfg.get("condition", 100),
            fuel=vehicle_cfg["fuel_capacity"],
            select=True,
        )

        rider_role = discord.utils.get(
            ctx.guild.roles,
            name=DISPATCH_RIDER_ROLE
        )

        if rider_role is not None:

            try:
                await ctx.author.add_roles(
                    rider_role,
                    reason="Became a dispatch rider"
                )

            except discord.Forbidden:
                pass

        await ctx.send(
            f"📦 {ctx.author.mention} is now a "
            f"**{tier}** dispatch rider! The company has handed "
            f"you a **{company_vehicle_name}** — check "
            f"`!vehicle` any time. Use `!dispatchstart` to go "
            f"online and start receiving delivery orders."
        )

    # ============================================================
    # !DISPATCHSTART / !DISPATCHSTOP
    # ============================================================

    @commands.command(name="dispatchstart")
    async def dispatchstart(self, ctx: commands.Context):

        rider = database.get_dispatch_rider(ctx.author.id)

        if rider is None:

            await _send_and_delete(
                ctx,
                "You're not a registered dispatch rider. Use "
                "`!becomedispatchrider standard` or "
                "`!becomedispatchrider premium` first."
            )

            return

        database.set_dispatch_online(ctx.author.id, True)

        await _send_and_delete(
            ctx,
            f"🟢 {ctx.author.mention} is now online and "
            f"bookable as a **{rider['tier']}** dispatch rider "
            f"(up to {DISPATCH_MAX_ORDERS} orders at once). "
            f"You'll stay online until you run `!dispatchstop`."
        )

        asyncio.create_task(
            _try_dispatch_queue(ctx.guild, rider["tier"])
        )

    @commands.command(name="dispatchstop")
    async def dispatchstop(self, ctx: commands.Context):

        rider = database.get_dispatch_rider(ctx.author.id)

        if rider is None:

            await _send_and_delete(
                ctx,
                "You're not a registered dispatch rider."
            )

            return

        database.set_dispatch_online(ctx.author.id, False)

        await _send_and_delete(
            ctx,
            f"🔴 {ctx.author.mention} is now offline."
        )

    # ============================================================
    # !ORDERDELIVERY
    # ============================================================

    @commands.command(name="orderdelivery")
    async def orderdelivery(
        self,
        ctx: commands.Context,
        tier: str = None,
        *,
        destination: str = None
    ):

        tier = _normalise_tier(tier)

        if tier is None or not destination:

            await _send_and_delete(
                ctx,
                "Usage: `!orderdelivery standard <destination>` "
                "or `!orderdelivery premium <destination>`"
            )

            return

        destination = _normalise_code(destination)

        sender = ctx.author
        sender_player = database.get_or_create_player(sender.id)

        kind, _existing = _find_active_order(sender.id)

        if kind is not None:

            await _send_and_delete(
                ctx,
                "You already have a pending delivery order."
            )

            return

        origin = _normalise_code(sender_player["location"])

        expected_channel = LOCATIONS.get(
            origin, {}
        ).get("channel")

        if ctx.channel.name != expected_channel:

            await _send_and_delete(
                ctx,
                f"You are not at {_name(origin)}'s channel "
                f"right now."
            )

            return

        if destination not in LOCATIONS:

            await _send_and_delete(
                ctx,
                f"⛔ `{destination}` is not a valid location."
            )

            return

        if not _is_road_destination(destination):

            await _send_and_delete(
                ctx,
                f"⛔ **{_name(destination)}** is not accessible "
                f"by road."
            )

            return

        if destination == origin:

            await _send_and_delete(
                ctx,
                f"You are already at {_name(destination)}."
            )

            return

        try:
            _, distance = find_route(origin, destination)

        except NoRouteError:

            await _send_and_delete(
                ctx,
                f"No road route exists between "
                f"{_name(origin)} and {_name(destination)}."
            )

            return

        fare = _calculate_fare(distance, tier)
        order_id = uuid.uuid4().hex[:6]

        entry = {
            "order_id": order_id,
            "sender_id": sender.id,
            "origin": origin,
            "destination": destination,
            "tier": tier,
            "fare": fare,
            "guild_id": ctx.guild.id,
            "channel_id": ctx.channel.id,
        }

        _sender_active_order[sender.id] = order_id

        try:
            await ctx.message.delete()

        except (discord.Forbidden, discord.NotFound):
            pass

        await _dispatch_or_queue(ctx.guild, entry)

    # ============================================================
    # !DISPATCHACCEPT / !DISPATCHDECLINE
    # ============================================================

    @commands.command(name="dispatchaccept")
    async def dispatchaccept(
        self,
        ctx: commands.Context,
        order_id: str = None
    ):

        if order_id is None:

            await _send_and_delete(
                ctx,
                "Usage: `!dispatchaccept <order_id>` — the id "
                "is shown in the order ping."
            )

            return

        notices = _rider_order_notice.get(ctx.author.id, set())

        if order_id not in notices:

            await _send_and_delete(
                ctx,
                "You have no pending ping for that order id."
            )

            return

        entry = _open_orders.get(order_id)
        notice = entry["notified"].get(ctx.author.id) if entry else None

        if entry is None or notice is None:

            notices.discard(order_id)

            await _send_and_delete(
                ctx,
                "That order is no longer available — someone "
                "else likely got there first."
            )

            return

        if _rider_capacity_used(ctx.author.id) >= DISPATCH_MAX_ORDERS:

            await _send_and_delete(
                ctx,
                f"⛔ You're already carrying "
                f"{DISPATCH_MAX_ORDERS} orders — drop one off "
                f"before accepting another."
            )

            return

        # Claim it immediately, before doing anything else.
        _open_orders.pop(order_id, None)
        entry["timeout_task"].cancel()

        await _delete_rider_ping(ctx.guild, notice)
        await _clear_all_rider_pings(
            ctx.guild, entry, except_rider_id=ctx.author.id
        )
        notices.discard(order_id)

        if not notices:
            _rider_order_notice.pop(ctx.author.id, None)

        _confirmed_orders.setdefault(ctx.author.id, []).append({
            "order_id": entry["order_id"],
            "sender_id": entry["sender_id"],
            "origin": entry["origin"],
            "destination": entry["destination"],
            "tier": entry["tier"],
            "fare": entry["fare"],
            "boarded": False,
        })

        sender_channel = ctx.guild.get_channel(entry["channel_id"])

        await _send_and_delete_channel(
            sender_channel,
            f"✅ {ctx.author.mention} accepted delivery order "
            f"`{order_id}` to **{_name(entry['destination'])}**."
        )

        used = _rider_capacity_used(ctx.author.id)

        await _send_and_delete(
            ctx,
            f"✅ Order `{order_id}` accepted "
            f"({used}/{DISPATCH_MAX_ORDERS}). Head to "
            f"**{_name(entry['origin'])}** and use "
            f"`!dispatchpickup` once you're there."
        )

    @commands.command(name="dispatchdecline")
    async def dispatchdecline(
        self,
        ctx: commands.Context,
        order_id: str = None
    ):

        if order_id is None:

            await _send_and_delete(
                ctx,
                "Usage: `!dispatchdecline <order_id>`"
            )

            return

        notices = _rider_order_notice.get(ctx.author.id, set())

        if order_id not in notices:

            await _send_and_delete(
                ctx,
                "You have no pending ping for that order id."
            )

            return

        entry = _open_orders.get(order_id)
        notice = (
            entry["notified"].pop(ctx.author.id, None)
            if entry is not None
            else None
        )

        notices.discard(order_id)

        if not notices:
            _rider_order_notice.pop(ctx.author.id, None)

        if entry is None or notice is None:

            await _send_and_delete(
                ctx,
                "That order is no longer available."
            )

            return

        await _delete_rider_ping(ctx.guild, notice)

        await _send_and_delete(
            ctx,
            f"❌ Declined delivery order `{order_id}`."
        )

        if entry["notified"]:
            return

        entry["timeout_task"].cancel()

        _open_orders.pop(order_id, None)

        sender_channel = ctx.guild.get_channel(entry["channel_id"])

        await _send_and_delete_channel(
            sender_channel,
            f"❌ Every nearby **{entry['tier']}** dispatch rider "
            f"declined order `{order_id}` — looking for "
            f"another..."
        )

        await _dispatch_or_queue(ctx.guild, entry)

    # ============================================================
    # !DISPATCHPICKUP (rider, only for orders whose origin matches
    # the rider's current location)
    # ============================================================

    @commands.command(name="dispatchpickup")
    async def dispatchpickup(self, ctx: commands.Context):

        orders = _confirmed_orders.get(ctx.author.id, [])
        pending_here = [
            o for o in orders
            if not o["boarded"]
        ]

        if not pending_here:

            await _send_and_delete(
                ctx,
                "You have no accepted orders waiting for pickup."
            )

            return

        rider_player = database.get_or_create_player(ctx.author.id)

        if rider_player["traveling"]:

            await _send_and_delete(
                ctx,
                "You can't pick up orders mid-trip."
            )

            return

        rider_location = _normalise_code(rider_player["location"])

        picked_up = []

        for order in pending_here:

            if order["origin"] != rider_location:
                continue

            expected_channel = LOCATIONS.get(
                order["origin"], {}
            ).get("channel")

            if ctx.channel.name != expected_channel:
                continue

            order["boarded"] = True
            picked_up.append(order)

        if not picked_up:

            await _send_and_delete(
                ctx,
                "None of your accepted orders are picked up "
                "here — check `!myorders`."
            )

            return

        destinations = ", ".join(
            f"**{_name(o['destination'])}**" for o in picked_up
        )

        await ctx.send(
            f"📦 Picked up {len(picked_up)} order(s), heading "
            f"to {destinations}. Use `!drive <any destination>` "
            f"to head out — the route auto-combines every "
            f"boarded order."
        )

    # ============================================================
    # !MYORDERS
    # ============================================================

    @commands.command(name="myorders")
    async def myorders(self, ctx: commands.Context):

        orders = _confirmed_orders.get(ctx.author.id, [])

        if not orders:

            await _send_and_delete(
                ctx,
                "You have no accepted orders right now."
            )

            return

        lines = []

        for order in orders:

            status = "boarded" if order["boarded"] else "awaiting pickup"

            lines.append(
                f"`{order['order_id']}` — "
                f"{_name(order['origin'])} → "
                f"{_name(order['destination'])} "
                f"({status})"
            )

        await _send_and_delete(
            ctx,
            f"📦 Your orders "
            f"({len(orders)}/{DISPATCH_MAX_ORDERS}):\n"
            + "\n".join(lines)
        )

    # ============================================================
    # !CANCELORDER
    # ============================================================

    @commands.command(name="cancelorder")
    async def cancelorder(
        self,
        ctx: commands.Context,
        order_id: str = None
    ):

        # --------------------------------------------------------
        # RIDER: confirmed, not yet boarded
        # --------------------------------------------------------

        if order_id is not None:

            orders = _confirmed_orders.get(ctx.author.id, [])

            target = next(
                (
                    o for o in orders
                    if o["order_id"] == order_id and not o["boarded"]
                ),
                None,
            )

            if target is not None:

                orders.remove(target)

                if not orders:
                    _confirmed_orders.pop(ctx.author.id, None)

                await _send_and_delete(
                    ctx,
                    f"Cancelled accepted order `{order_id}` "
                    f"(not yet picked up)."
                )

                rider = database.get_dispatch_rider(ctx.author.id)

                if rider is not None and rider["online"]:

                    asyncio.create_task(
                        _try_dispatch_queue(ctx.guild, target["tier"])
                    )

                return

        # --------------------------------------------------------
        # SENDER: pending (broadcasting) or queued
        # --------------------------------------------------------

        kind, entry = _find_active_order(ctx.author.id)

        if kind == "pending":

            entry["timeout_task"].cancel()

            _open_orders.pop(entry["order_id"], None)
            _sender_active_order.pop(ctx.author.id, None)

            await _clear_all_rider_pings(ctx.guild, entry)

            await _send_and_delete(
                ctx,
                "Cancelled the pending delivery order."
            )

            return

        if kind == "queued":

            entry["timeout_task"].cancel()

            queue = _queue.get(entry["tier"])

            if queue is not None and entry in queue:
                queue.remove(entry)

            _sender_active_order.pop(ctx.author.id, None)

            await _send_and_delete(
                ctx,
                "Left the queue — delivery order cancelled."
            )

            return

        await _send_and_delete(
            ctx,
            "You have no cancellable order right now."
        )

    # ============================================================
    # !RETRIEVEVEHICLE — company command to take back a
    # commercial (taxi OR dispatch) vehicle, gated to
    # COMMERCIAL_VEHICLE_MANAGER_ROLE.
    # ============================================================

    @commands.command(name="retrievevehicle")
    async def retrievevehicle(
        self,
        ctx: commands.Context,
        member: discord.Member = None
    ):

        manager_role = discord.utils.get(
            ctx.author.roles,
            name=COMMERCIAL_VEHICLE_MANAGER_ROLE
        )

        if manager_role is None:

            await _send_and_delete(
                ctx,
                f"⛔ You need the "
                f"**{COMMERCIAL_VEHICLE_MANAGER_ROLE}** role to "
                f"retrieve a commercial vehicle."
            )

            return

        if member is None:

            await _send_and_delete(
                ctx,
                "Usage: `!retrievevehicle <@user>`"
            )

            return

        selected = database.get_selected_vehicle(member.id)

        commercial_types = {
            "taxi", "dispatch_bicycle", "dispatch_motorcycle"
        }

        if selected is None or selected.get("type") not in commercial_types:

            await _send_and_delete(
                ctx,
                f"{member.mention} isn't currently driving a "
                f"commercial (taxi/dispatch) vehicle."
            )

            return

        removed = database.remove_vehicle(member.id, selected["id"])

        if not removed:

            # Legacy flat-column-only entry (no persisted record)
            # — clear the flat columns directly.
            database.update_player(
                member.id,
                vehicle=None,
                vehicle_location=None,
                fuel=0,
                vehicle_condition=100,
            )

        if selected.get("type") in ("dispatch_bicycle", "dispatch_motorcycle"):

            database.remove_dispatch_rider(member.id)

            rider_role = discord.utils.get(
                ctx.guild.roles,
                name=DISPATCH_RIDER_ROLE
            )

            if rider_role is not None:

                try:
                    await member.remove_roles(
                        rider_role,
                        reason="Dispatch vehicle retrieved"
                    )

                except discord.Forbidden:
                    pass

            _confirmed_orders.pop(member.id, None)

        else:

            database.set_taxi_online(member.id, False)

        await ctx.send(
            f"🚗 {member.mention}'s **{selected['name']}** has "
            f"been retrieved by the company."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DispatchCog(bot))
