"""
Eko Metropolis — Public Bus / BRT System

BUS SYSTEM
==========

Buses are automatically operated by the system.

Players do NOT drive buses.

Bus capacity:
    10 passengers per bus.

Routes:
    B1 = Ghetto <-> Mainland
    B2 = Mainland <-> Island
    B3 = Ghetto <-> Island

Every normal road location is a bus stop.

Passengers:
    - Must have the BRT Card role.
    - Must have enough BRT balance.
    - Must select a valid destination for the selected route.
    - Must be at a location accessible by that route.
    - Are accepted on a first-come-first-served basis.
    - Are removed from the queue once accepted.
    - Are charged only when successfully boarding.
    - Do not pay road tolls.

The bus checks the same road network used by private travel.

A destination is rejected when:
    - It is not a road destination.
    - It is overseas.
    - It is restricted to the player.
    - The selected bus route cannot reach it.
    - The player is already traveling.
    - The player is not at a valid bus stop.
    - The BRT card is missing.
    - The BRT card has insufficient funds.

Messages are temporary to avoid channel clutter.
"""

import asyncio
import time
import logging

from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands, tasks

import database
import permissions
from config import (
    LOCATIONS,
    RAW_DISTANCES,
    ROAD_DESTINATIONS,
    OVERSEAS,
    BRT_MIN_TRAVEL_TIME_SECONDS,
    BRT_MAX_TRAVEL_TIME_SECONDS,
    BRT_SECONDS_PER_KM,
    BRT_ROUTE_ONE_WAY_SECONDS,
)

# ============================================================
# BUS.PY LOAD TEST
# ============================================================

log = logging.getLogger("ekobot")

log.warning(
    "🚌🚌🚌 BUS.PY MODULE HAS BEEN IMPORTED 🚌🚌🚌"
)


# ============================================================
# BUS CONFIGURATION
# ============================================================

BUS_CAPACITY = 10

BUS_ROUTES = {
    "B1": {
        "name": "Farmland ↔ Ghetto ↔ Mainland",
        "zones": {
            "farmland",
            "ghetto",
            "mainland",
        },
    },

    "B2": {
        "name": "Mainland ↔ Island",
        "zones": {
            "mainland",
            "island",
        },
    },

    "B3": {
        "name": "Farmland ↔ Ghetto ↔ Island",
        "zones": {
            "farmland",
            "ghetto",
            "island",
        },
    },
}

# ============================================================
# PER-ZONE STOP SEQUENCE
#
# A bus doesn't just jump between zone hubs — it drives
# through every individual road location within a zone before
# continuing to the next zone. The order used is the order
# each location is declared in config.LOCATIONS (which is
# already grouped and laid out zone-by-zone, hub first), so
# there is exactly one place that defines stop order.
# ============================================================

def _zone_stop_sequence(zone: str) -> list[str]:

    return [
        code
        for code, location in LOCATIONS.items()
        if location.get("zone") == zone
        and code not in OVERSEAS
    ]


# ============================================================
# ROUTE STOP LIST (EVERY INDIVIDUAL LOCATION, OUT AND BACK)
#
# Each route drives forward through every stop in each of its
# zones (in the corridor order below), then turns around and
# drives back through the exact same stops in reverse — a real
# out-and-back bus route — ending back where it started.
# ============================================================

_ROUTE_ZONE_ORDER = {
    "B1": ["farmland", "ghetto", "mainland"],
    "B2": ["mainland", "island"],
    "B3": ["farmland", "ghetto", "island"],
}


def _build_route_stops(zone_order: list[str]) -> list[str]:

    outbound: list[str] = []

    for zone in zone_order:
        outbound.extend(_zone_stop_sequence(zone))

    # Reverse for the return leg, dropping the first element of
    # the reversal so the final outbound stop isn't repeated.
    inbound = list(reversed(outbound))[1:]

    return outbound + inbound


ROUTE_STOPS = {
    route: _build_route_stops(zone_order)
    for route, zone_order in _ROUTE_ZONE_ORDER.items()
}

# ============================================================
# PER-HOP TRAVEL TIME (BRT-ONLY DIAL)
#
# BRT_ROUTE_ONE_WAY_SECONDS gives each route a fixed one-way
# trip length (start zone -> end/last location of its final
# zone). That total is split evenly across every hop on the
# route (there are len(ROUTE_STOPS[route]) - 1 hops total,
# covering both the outbound and the mirrored return leg), so
# every stop-to-stop hop takes the same amount of time. This
# is completely independent of TRAVEL_SECONDS_PER_KM /
# MIN_TRAVEL_TIME_SECONDS / MAX_TRAVEL_TIME_SECONDS, which only
# govern private car travel.
# ============================================================

def _route_hop_seconds(route: str) -> Optional[float]:

    zone_order = _ROUTE_ZONE_ORDER.get(route)

    if not zone_order:
        return None

    total_seconds = BRT_ROUTE_ONE_WAY_SECONDS.get(route)

    if total_seconds is None:
        return None

    # ------------------------------------------------
    # IMPORTANT: use the OUTBOUND-ONLY hop count here,
    # not the full out-and-back ROUTE_STOPS list. The
    # timing the Mayor gives us ("Farmland to end/last
    # location of Mainland = 1.2 min") describes a
    # ONE-WAY trip. Dividing by the full round-trip hop
    # count (which is roughly double the outbound count)
    # would silently halve every one-way time.
    # ------------------------------------------------

    outbound_stop_count = sum(
        len(
            _zone_stop_sequence(
                zone
            )
        )
        for zone in zone_order
    )

    outbound_hops = outbound_stop_count - 1

    if outbound_hops <= 0:
        return None

    return total_seconds / outbound_hops


ROUTE_HOP_SECONDS = {
    route: _route_hop_seconds(route)
    for route in ROUTE_STOPS
}

MAYOR_ROLE = "Mayor of Eko"

BRT_ROLE = "BRT Card"

BUS_MESSAGE_DELETE_DELAY = 8

BUS_DEPARTURE_INTERVAL = 60

# Dwell time at each stop for routes that do NOT have a fixed
# BRT_ROUTE_ONE_WAY_SECONDS entry (see ROUTE_HOP_SECONDS above).
# For routes that DO have one, the Mayor's timing already covers
# the full door-to-door time including stops, so no separate
# dwell is added on top of it — see the dispatch loop.
BUS_STOP_DWELL_SECONDS = 5

# ============================================================
# BUS DATA STRUCTURES
# ============================================================

@dataclass
class Passenger:
    user_id: int
    origin: str
    destination: str
    route: str
    queued_at: float


@dataclass
class ActivePassenger:
    user_id: int
    origin: str
    destination: str
    route: str


@dataclass
class Bus:
    bus_id: int
    route: str
    passengers: list[ActivePassenger]
    current_location: Optional[str] = None
    stop_index: int = 0
    # 1 = starts at the route's first stop and drives outbound
    #     first (the normal direction).
    # -1 = starts at the route's LAST stop (the opposite end)
    #      and drives the mirrored route in reverse, so that
    #      two buses on the same route start facing each other.
    direction: int = 1


# ============================================================
# BUS COG
# ============================================================

class BusCog(commands.Cog):

    def __init__(
        self,
        bot: commands.Bot
    ):
        self.bot = bot

        self.buses: dict[str, list[Bus]] = {
            "B1": [],
            "B2": [],
            "B3": [],
        }

        self.queues: dict[str, deque[Passenger]] = {
            "B1": deque(),
            "B2": deque(),
            "B3": deque(),
        }

        self.active_passengers: dict[int, ActivePassenger] = {}

        self.next_bus_id = 1

        self.bus_tasks: dict[int, asyncio.Task] = {}

        self._load_bus_fleet()

    # ========================================================
    # TEMPORARY MESSAGE
    # ========================================================

    async def _temporary_message(
        self,
        destination,
        content: str,
        delay: int = BUS_MESSAGE_DELETE_DELAY,
        silent: bool = False
    ):

        try:

            message = await destination.send(
                content,
                silent=silent
            )

            await asyncio.sleep(
                delay
            )

            try:
                await message.delete()
            except (
                discord.NotFound,
                discord.Forbidden
            ):
                pass

            return message

        except (
            discord.Forbidden,
            discord.HTTPException
        ):

            return None

    # ========================================================
    # LOCATION HELPERS
    # ========================================================

    def _location_name(
        self,
        code: str
    ) -> str:

        location = LOCATIONS.get(
            code
        )

        if not location:
            return code

        return location.get(
            "name",
            code
        )

    # ========================================================
    # ZONE
    # ========================================================

    def _zone(
        self,
        code: str
    ) -> Optional[str]:

        location = LOCATIONS.get(
            code
        )

        if not location:
            return None

        return location.get(
            "zone"
        )

    # ========================================================
    # CHANNEL FOR LOCATION
    # ========================================================

    def _channel_for_location(
        self,
        guild: discord.Guild,
        code: str
    ):

        location = LOCATIONS.get(
            code
        )

        if not location:
            return None

        channel_name = location.get(
            "channel"
        )

        if not channel_name:
            return None

        return discord.utils.get(
            guild.text_channels,
            name=channel_name
        )

    # ========================================================
    # CHANNEL FOR BUS'S CURRENT STOP
    #
    # _run_bus() sends "departing"/"arrived" announcements
    # via self._bus_channel(bus), but that method never
    # existed on this class — only _channel_for_location()
    # (which needs an explicit guild) did. Calling the
    # missing method raised an AttributeError the very first
    # time a dispatched bus tried to announce its departure,
    # which is caught by the broad except Exception in
    # _run_bus() and only printed to the console. The bus
    # task died right there, before it ever traveled to the
    # next stop or updated bus.current_location — so the bus
    # appeared to never come, no matter where a passenger was
    # queued or how many times the dispatch loop retried it.
    #
    # This wraps _channel_for_location() by resolving the
    # bus's current stop against every guild the bot is in,
    # the same guild-lookup pattern already used elsewhere in
    # this cog (e.g. _board_passengers/_drop_passengers).
    # ========================================================

    def _bus_channel(
        self,
        bus: "Bus"
    ):

        for guild in self.bot.guilds:

            channel = self._channel_for_location(
                guild,
                bus.current_location
            )

            if channel:
                return channel

        return None

    # ========================================================
    # VALID ROAD LOCATION
    # ========================================================

    def _is_road_destination(
        self,
        code: str
    ) -> bool:

        return (
            code in ROAD_DESTINATIONS
            and code not in OVERSEAS
        )

    # ========================================================
    # ROLE CHECK
    # ========================================================

    def _has_role(
        self,
        member: discord.Member,
        role_name: str
    ) -> bool:

        return any(
            role.name == role_name
            for role in member.roles
        )

    # ========================================================
    # MAYOR CHECK
    # ========================================================

    def _is_mayor(
        self,
        member: discord.Member
    ) -> bool:

        return self._has_role(
            member,
            MAYOR_ROLE
        )

    # ========================================================
    # BRT CARD CHECK
    # ========================================================

    def _has_brt_card(
        self,
        member: discord.Member
    ) -> bool:

        return self._has_role(
            member,
            BRT_ROLE
        )

    # ========================================================
    # BUS FLEET STORAGE
    # ========================================================

    def _fleet_file(
        self
    ) -> str:

        return "bus_fleet.json"

        # ========================================================
    # LOAD FLEET
    # ========================================================

    def _load_bus_fleet(
        self
    ) -> None:

        import json
        import os

        path = self._fleet_file()

        if not os.path.exists(path):
            return

        try:

            with open(
                path,
                "r",
                encoding="utf-8"
            ) as file:

                data = json.load(
                    file
                )

            self.next_bus_id = int(
                data.get(
                    "next_bus_id",
                    1
                )
            )

            for route in BUS_ROUTES:

                self.buses[route] = []

                for item in data.get(
                    route,
                    []
                ):

                    current_location = item.get(
                        "current_location"
                    )

                    if current_location is None:

                        current_location = (
                            self._route_start(
                                route
                            )
                        )

                    self.buses[route].append(
                        Bus(
                            bus_id=int(
                                item["bus_id"]
                            ),
                            route=route,
                            passengers=[],
                            current_location=current_location,
                            stop_index=int(
                                item.get(
                                    "stop_index",
                                    0
                                )
                            ),
                            direction=int(
                                item.get(
                                    "direction",
                                    1
                                )
                            )
                        )
                    )

        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError
        ):

            self.next_bus_id = 1
    
        

        # ========================================================
    # SAVE FLEET
    # ========================================================

    def _save_bus_fleet(
        self
    ) -> None:

        import json

        data = {
            "next_bus_id": self.next_bus_id
        }

        for route in BUS_ROUTES:

            data[route] = [
                {
                    "bus_id": bus.bus_id,
                    "current_location": (
                        bus.current_location
                    ),
                    "stop_index": bus.stop_index,
                    "direction": bus.direction
                }
                for bus in self.buses[route]
            ]

        with open(
            self._fleet_file(),
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                indent=4
                    )

        # ========================================================
    # PURCHASE BUS
    # ========================================================

    @commands.command(
        name="purchasebus"
    )
    async def purchase_bus(
        self,
        ctx: commands.Context,
        route: str,
        quantity: int = 1
    ):

        if not isinstance(
            ctx.author,
            discord.Member
        ):
            return

        if not self._is_mayor(
            ctx.author
        ):

            await self._temporary_message(
                ctx.channel,
                "❌ Only the **Mayor of Eko** can purchase buses."
            )

            return

        route = str(
            route
        ).upper().strip()

        if route not in BUS_ROUTES:

            await self._temporary_message(
                ctx.channel,
                "❌ Invalid bus route. Use **B1**, **B2**, or **B3**."
            )

            return

        try:

            quantity = int(
                quantity
            )

        except ValueError:

            await self._temporary_message(
                ctx.channel,
                "❌ Quantity must be a number."
            )

            return

        if quantity < 1:

            await self._temporary_message(
                ctx.channel,
                "❌ You must purchase at least one bus."
            )

            return

        # ------------------------------------------------
        # DETERMINE STARTING LOCATION
        #
        # Buses on the same route alternate which end they
        # start from — the 1st, 3rd, 5th... bus purchased for
        # a route starts at the normal route start and drives
        # forward; the 2nd, 4th, 6th... starts at the OPPOSITE
        # end of the route and drives it in reverse, so two
        # buses on a route set off toward each other instead
        # of both leaving from the same stop.
        # ------------------------------------------------

        forward_start = self._route_start(
            route
        )

        if forward_start is None:

            await self._temporary_message(
                ctx.channel,
                "❌ Unable to determine the starting location for this route."
            )

            return

        reverse_stops = list(
            reversed(
                ROUTE_STOPS.get(
                    route,
                    []
                )
            )
        )

        reverse_start = (
            reverse_stops[0]
            if reverse_stops
            else forward_start
        )

        purchased = []

        # ------------------------------------------------
        # PURCHASE BUSES
        # ------------------------------------------------

        existing_count = len(
            self.buses[route]
        )

        for offset in range(quantity):

            bus_number = existing_count + offset

            direction = (
                1
                if bus_number % 2 == 0
                else -1
            )

            starting_location = (
                forward_start
                if direction == 1
                else reverse_start
            )

            bus = Bus(
                bus_id=self.next_bus_id,
                route=route,
                passengers=[],
                current_location=starting_location,
                direction=direction
            )

            self.buses[
                route
            ].append(
                bus
            )

            purchased.append(
                self.next_bus_id
            )

            self.next_bus_id += 1

        # ------------------------------------------------
        # SAVE FLEET
        # ------------------------------------------------

        self._save_bus_fleet()

        ids = ", ".join(
            f"B{bus_id}"
            for bus_id in purchased
        )

        await self._temporary_message(
            ctx.channel,
            (
                f"🚌 **Bus Purchase Successful**\n"
                f"Route: **{route} — {BUS_ROUTES[route]['name']}**\n"
                f"Buses purchased: **{len(purchased)}**\n"
                f"Bus ID(s): **{ids}**\n"
                f"Starting location: **{self._location_name(starting_location)}**\n"
                f"Cost: **₦0**"
            ),
            delay=12
        )
    # ========================================================
    # BUS FLEET
    # ========================================================

    @commands.command(
        name="busfleet"
    )
    async def bus_fleet(
        self,
        ctx: commands.Context
    ):

        lines = [
            "🚌 **EKO PUBLIC BUS FLEET**",
            ""
        ]

        total = 0

        for route, buses in self.buses.items():

            count = len(
                buses
            )

            total += count

            lines.append(
                f"**{route}** — "
                f"{BUS_ROUTES[route]['name']} — "
                f"{count} bus(es)"
            )

        lines.append(
            ""
            f"Total buses: **{total}**"
        )

        await self._temporary_message(
            ctx.channel,
            "\n".join(
                lines
            ),
                    delay=10
        )

    # ========================================================
    # VALIDATE ROUTE
    # ========================================================
    def _route_allows(
        self,
        route: str,
        origin: str,
        destination: str
    ) -> bool:

        """
        Check whether a passenger can use the selected BRT
        corridor between two valid road bus stops.

        The route is a corridor, NOT a restriction to only
        the two endpoint channels.

        Example:

            B2 = Mainland ↔ Island

        Therefore, any valid Mainland road stop can use B2
        toward any valid Island road stop.

        Likewise, valid stops within the same corridor are
        allowed when the road network connects them.

        The actual road network is checked by _road_distance().
        """

        # ----------------------------------------------------
        # ROUTE CHECK
        # ----------------------------------------------------

        if route not in BUS_ROUTES:
            return False

        # ----------------------------------------------------
        # ORIGIN MUST BE A ROAD BUS STOP
        # ----------------------------------------------------

        if not self._is_road_destination(
            origin
        ):
            return False

        # ----------------------------------------------------
        # DESTINATION MUST BE A ROAD BUS STOP
        # ----------------------------------------------------

        if not self._is_road_destination(
            destination
        ):
            return False

        # ----------------------------------------------------
        # CANNOT TRAVEL TO CURRENT LOCATION
        # ----------------------------------------------------

        if origin == destination:
            return False

        # ----------------------------------------------------
        # GET LOCATION ZONES
        # ----------------------------------------------------

        origin_zone = self._zone(
            origin
        )

        destination_zone = self._zone(
            destination
        )

        if (
            origin_zone is None
            or destination_zone is None
        ):
            return False

                # ----------------------------------------------------
        # ROUTE CORRIDOR
        #
        # The selected route connects its two zones.
        #
        # Example:
        #
        # B1 = Ghetto ↔ Mainland
        # B2 = Mainland ↔ Island
        # B3 = Ghetto ↔ Island
        #
        # Any road stop belonging to either side of the
        # corridor may be used.
        # ----------------------------------------------------

        route_zones = BUS_ROUTES[
            route
        ][
            "zones"
        ]

        # ----------------------------------------------------
        # BOTH LOCATIONS MUST BELONG TO THE SELECTED
        # CORRIDOR.
        #
        # This allows:
        #
        # Mainland Taxi Company → Island Mall
        # Mainland Taxi Company → Island Bank
        # Mainland Taxi Company → Mainland Restaurant
        #
        # provided those locations are valid road stops.
        # ----------------------------------------------------

        if origin_zone not in route_zones:
            return False

        if destination_zone not in route_zones:
            return False

        # ----------------------------------------------------
        # VALID BRT CORRIDOR
        #
        # Every road location in either zone is a valid
        # bus stop. Same-zone trips are also allowed.
        # ----------------------------------------------------

        return True

    # ========================================================
    # CHECK ACCESS
    # ========================================================

    def _has_location_access(
        self,
        member: discord.Member,
        destination: str
    ) -> bool:

        location = LOCATIONS.get(
            destination
        )

        if location is None:
            return False

        required_roles = location.get(
            "roles"
        )

        if not required_roles:
            return True

        member_role_names = {
            role.name
            for role in member.roles
        }

        return any(
            required_role in member_role_names
            for required_role in required_roles
        )

    # ========================================================
    # ROAD CHECK
    # ========================================================

    def _road_distance(
        self,
        origin: str,
        destination: str
    ) -> Optional[float]:

        import heapq

        graph = {}

        def add_edge(
            a,
            b,
            distance
        ):

            graph.setdefault(
                a,
                {}
            )[b] = distance

            graph.setdefault(
                b,
                {}
            )[a] = distance

        for (
            a,
            b
        ), distance in RAW_DISTANCES.items():

            add_edge(
                a,
                b,
                distance
            )

        graph.get(
            "island",
            {}
        ).pop(
            "ghetto",
            None
        )

        graph.get(
            "ghetto",
            {}
        ).pop(
            "island",
            None
        )

        if origin not in graph:
            return None

        if destination not in graph:
            return None

        distances = {
            origin: 0.0
        }

        heap = [
            (
                0.0,
                origin
            )
        ]

        visited = set()

        while heap:

            current_distance, node = (
                heapq.heappop(
                    heap
                )
            )

            if node in visited:
                continue

            visited.add(
                node
            )

            if node == destination:
                return current_distance

            for (
                neighbor,
                edge_distance
            ) in graph.get(
                node,
                {}
            ).items():

                if neighbor in visited:
                    continue

                new_distance = (
                    current_distance
                    + edge_distance
                )

                if new_distance < distances.get(
                    neighbor,
                    float("inf")
                ):

                    distances[
                        neighbor
                    ] = new_distance

                    heapq.heappush(
                        heap,
                        (
                            new_distance,
                            neighbor
                        )
                    )

        return None
        # ========================================================
    # BUS COMMAND
    # ========================================================

    @commands.command(
        name="bus"
    )
    async def bus(
        self,
        ctx: commands.Context,
        route: str,
        destination: str
    ):

        member = ctx.author

        if not isinstance(
            member,
            discord.Member
        ):
            return

        try:
            await ctx.message.delete()
        except (
            discord.NotFound,
            discord.Forbidden
        ):
            pass

        route = str(
            route
        ).upper().strip()

        destination = str(
            destination
        ).lower().strip()

        if route not in BUS_ROUTES:

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"invalid bus route. Use **B1**, **B2**, or **B3**."
                )
            )

            return

        # ------------------------------------------------
        # CHECK BUS AVAILABILITY
        # ------------------------------------------------

        available_bus = self._find_available_bus(
            route
        )

        if available_bus is None:

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"there is currently no **{route}** bus available.\n"
                    f"Please wait until a bus is purchased."
                ),
                delay=8
            )

            return

        player = database.get_player(
            member.id
        )

        if player is None:

            player = database.create_player(
                member.id
            )

        origin = str(
            player["location"]
        ).lower().strip()

        if player["traveling"]:

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"you cannot board a bus while you are already traveling."
                )
            )

            return

        if member.id in self.active_passengers:

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"you are already on a bus."
                )
            )

            return

        if not self._has_brt_card(
            member
        ):

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"you need a **BRT Card** before using the bus."
                )
            )

            return

        if not self._is_road_destination(
            origin
        ):

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"your current location is not a valid road bus stop."
                )
            )

            return

        if destination not in LOCATIONS:

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"**{destination}** is not a valid location."
                )
            )

            return

        if not self._is_road_destination(
            destination
        ):

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"**{self._location_name(destination)}** "
                    f"is not a road destination."
                )
            )

            return

        if destination in OVERSEAS:

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"**{self._location_name(destination)}** "
                    f"is not accessible by road."
                )
            )

            return

        if not self._has_location_access(
            member,
            destination
        ):

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"you cannot access **"
                    f"{self._location_name(destination)}**. "
                    f"It is a restricted location."
                )
            )

            return

        if not self._route_allows(
            route,
            origin,
            destination
        ):

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"you cannot travel from "
                    f"**{self._location_name(origin)}** "
                    f"to **{self._location_name(destination)}** "
                    f"using **{route}**.\n"
                    f"This bus operates on "
                    f"**{BUS_ROUTES[route]['name']}**."
                )
            )

            return

        distance = self._road_distance(
            origin,
            destination
        )

        if distance is None:

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"there is no road route from "
                    f"**{self._location_name(origin)}** "
                    f"to **{self._location_name(destination)}**."
                )
            )

            return

        fare = self._calculate_fare(
            distance
        )

        brt_balance = self._get_brt_balance(
            member.id
        )

        if brt_balance < fare:

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"you have **missed this bus**.\n"
                    f"Your BRT Card balance is insufficient.\n"
                    f"Required: **₦{fare:,.0f}**\n"
                    f"Available: **₦{brt_balance:,.0f}**"
                ),
                delay=6
            )

            return

        if any(
            passenger.user_id == member.id
            for passenger in self.queues[route]
        ):

            await self._temporary_message(
                ctx.channel,
                (
                    f"❌ <@{member.id}> "
                    f"you are already in the **{route}** bus queue."
                )
            )

            return

        passenger = Passenger(
            user_id=member.id,
            origin=origin,
            destination=destination,
            route=route,
            queued_at=time.monotonic()
        )

        self.queues[
            route
        ].append(
            passenger
        )

        position = len(
            self.queues[route]
        )

        await self._temporary_message(
            ctx.channel,
            (
                f"🚌 <@{member.id}> "
                f"you are in the **{route}** bus queue.\n"
                f"Route: **{BUS_ROUTES[route]['name']}**\n"
                f"Destination: **{self._location_name(destination)}**\n"
                f"Queue position: **{position}**\n"
                f"Fare: **₦{fare:,.0f}**"
            ),
            delay=8
        )
                                

    # ========================================================
    # FARE
    # ========================================================

    def _calculate_fare(
        self,
        distance: float
    ) -> int:

        fare = (
            500
            + (distance * 100)
        )

        return int(
            round(fare)
        )

    # ========================================================
    # BRT BALANCE
    # ========================================================

    def _get_brt_balance(
        self,
        user_id: int
    ) -> int:

        return database.get_brt_balance(
            user_id
        )

    # ========================================================
    # CHARGE BRT CARD
    # ========================================================

    def _charge_brt(
        self,
        user_id: int,
        amount: int
    ) -> bool:

        return database.deduct_brt_balance(
            user_id,
            amount
        )

# ========================================================
    # ========================================================
    # FIND AVAILABLE BUS
    # ========================================================

    def _find_available_bus(
        self,
        route: str
    ) -> Optional[Bus]:

        for bus in self.buses.get(
            route,
            []
        ):

            if len(
                bus.passengers
            ) < BUS_CAPACITY:

                return bus

        return None

    # ========================================================
    # BOARD QUEUED PASSENGERS
    # ========================================================

    async def _board_passengers(
        self,
        bus: Bus
    ) -> None:

        queue = self.queues[
            bus.route
        ]

        # ------------------------------------------------
        # SCAN THE WHOLE QUEUE, NOT JUST THE FRONT.
        #
        # This queue is shared by the entire route. If we
        # only ever look at queue[0] and bail out the
        # instant it doesn't match this stop, a passenger
        # waiting further down the route blocks EVERY
        # passenger behind them — even ones standing right
        # here at this stop, ready to board. Passengers who
        # don't match this stop are set aside and returned
        # to the front of the queue afterward, preserving
        # their original order for later stops.
        # ------------------------------------------------

        skipped = deque()

        while (
            queue
            and len(bus.passengers)
            < BUS_CAPACITY
        ):

            passenger = queue.popleft()

            # ------------------------------------------------
            # BUS MUST BE AT THE PASSENGER'S EXACT STOP.
            #
            # The route now visits every individual road
            # location, not just the zone hub, so a rider only
            # boards when the bus is literally sitting at
            # their specific location — not merely somewhere
            # else in the same zone.
            # ------------------------------------------------

            if passenger.origin != bus.current_location:

                skipped.append(
                    passenger
                )

                continue

            member = None

            for guild in self.bot.guilds:

                member = guild.get_member(
                    passenger.user_id
                )

                if member:
                    break

            if member is None:
                continue

            player = database.get_player(
                member.id
            )

            if player is None:
                continue

            if player["traveling"]:
                continue

            if not self._has_brt_card(
                member
            ):
                continue

            distance = self._road_distance(
                passenger.origin,
                passenger.destination
            )

            if distance is None:
                continue

            fare = self._calculate_fare(
                distance
            )

            balance = self._get_brt_balance(
                member.id
            )

            if balance < fare:

                channel = self._channel_for_location(
                    member.guild,
                    passenger.origin
                )

                if channel:

                    await self._temporary_message(
                        channel,
                        (
                            f"❌ <@{member.id}> "
                            f"you missed the **{passenger.route}** bus.\n"
                            f"BRT Card balance is insufficient.\n"
                            f"Required: **₦{fare:,.0f}**\n"
                            f"Available: **₦{balance:,.0f}**"
                        ),
                        delay=8
                    )

                continue

            charged = self._charge_brt(
                member.id,
                fare
            )

            if not charged:
                continue

            active = ActivePassenger(
                user_id=member.id,
                origin=passenger.origin,
                destination=passenger.destination,
                route=passenger.route
            )

            bus.passengers.append(
                active
            )

            self.active_passengers[
                member.id
            ] = active

            database.update_player(
                member.id,
                traveling=1
            )

            # ------------------------------------------------
            # LOCK THE DEPARTURE CHANNEL
            #
            # Mirrors private travel: write access to the
            # origin location is removed the moment a
            # passenger actually boards, not merely when they
            # join the queue.
            # ------------------------------------------------

            await permissions.set_write_access(
                member.guild,
                member,
                passenger.origin,
                allowed=False
            )

            channel = self._channel_for_location(
                member.guild,
                passenger.origin
            )

            if channel:

                await self._temporary_message(
                    channel,
                    (
                        f"🚌 <@{member.id}> "
                        f"has boarded **{passenger.route}**.\n"
                        f"Destination: "
                        f"**{self._location_name(passenger.destination)}**.\n"
                        f"Passengers onboard: "
                        f"**{len(bus.passengers)}/{BUS_CAPACITY}**"
                    ),
                    delay=BUS_MESSAGE_DELETE_DELAY
                        )

        # ------------------------------------------------
        # RETURN SKIPPED PASSENGERS TO THE FRONT OF THE
        # QUEUE, IN THEIR ORIGINAL ORDER, SO THEY'RE STILL
        # FIRST IN LINE FOR THEIR OWN STOP LATER.
        # ------------------------------------------------

        queue.extendleft(
            reversed(
                skipped
            )
        )

        # ========================================================
    # DROP PASSENGERS
    # ========================================================

    async def _drop_passengers(
        self,
        bus: Bus
    ) -> None:

        remaining_passengers = []

        # ------------------------------------------------
        # DROP AT THE PASSENGER'S EXACT STOP.
        #
        # As with boarding, the route now visits every
        # individual road location, so a rider is only
        # dropped off when the bus is literally at their
        # destination — not just anywhere in the same zone.
        # This is what stops a same-zone trip (e.g. dealership
        # -> mall) from dragging the passenger through the
        # bus's entire remaining loop before they get off.
        # ------------------------------------------------

        for passenger in bus.passengers:

            if passenger.destination == bus.current_location:

                member = None

                for guild in self.bot.guilds:

                    member = guild.get_member(
                        passenger.user_id
                    )

                    if member:
                        break

                if member:

                    database.update_player(
                        member.id,
                        location=passenger.destination,
                        traveling=0
                    )

                    self.active_passengers.pop(
                        member.id,
                        None
                    )

                    # ------------------------------------------------
                    # UNLOCK THE DESTINATION CHANNEL
                    #
                    # Mirrors private travel: write access moves
                    # from the origin (already revoked at
                    # boarding) to the destination the instant
                    # the passenger arrives.
                    # ------------------------------------------------

                    await permissions.move_write_access(
                        member.guild,
                        member,
                        old_code=passenger.origin,
                        new_code=passenger.destination
                    )

                    channel = self._channel_for_location(
                        member.guild,
                        passenger.destination
                    )

                    if channel:

                        await self._temporary_message(
                            channel,
                            (
                                f"🚌 <@{member.id}> "
                                f"has arrived at "
                                f"**{self._location_name(passenger.destination)}**."
                            ),
                            delay=BUS_MESSAGE_DELETE_DELAY
                        )

            else:

                remaining_passengers.append(
                    passenger
                )

        bus.passengers = remaining_passengers
    # ========================================================
    # RUN BUS
    # ========================================================

    async def _run_bus(
        self,
        bus: Bus
    ) -> None:

        try:

            # ------------------------------------------------
            # INITIAL BUS LOCATION + RESET STOP INDEX FOR A
            # FRESH RUN
            #
            # A bus always begins a new dispatch run sitting at
            # its OWN starting stop — which depends on its
            # direction, so two buses on the same route can
            # start from opposite ends — so both current_location
            # and stop_index reset every run. Otherwise a bus
            # can only ever complete one trip before it gets
            # stuck, and a reverse-direction bus would start
            # from the wrong end.
            # ------------------------------------------------

            bus_stops = self._stops_for_bus(
                bus
            )

            if not bus_stops:
                return

            bus.current_location = bus_stops[0]

            bus.stop_index = 0

            # ------------------------------------------------
            # CONTINUOUS ROUTE
            # ------------------------------------------------

            while True:

                # ------------------------------------------------
                # DROP PASSENGERS AT CURRENT STOP
                # ------------------------------------------------

                await self._drop_passengers(
                    bus
                )

                # ------------------------------------------------
                # BOARD PASSENGERS AT CURRENT STOP
                # ------------------------------------------------

                await self._board_passengers(
                    bus
                )

                # ------------------------------------------------
                # NEXT STOP
                # ------------------------------------------------

                next_index, next_stop = self._next_bus_stop_by_index(
                    bus,
                    bus.stop_index
                )

                if next_stop is None:
                    return

                bus.stop_index = next_index

                # ------------------------------------------------
                # BUS STAYS AT STOP
                #
                # Skipped for routes with a fixed
                # BRT_ROUTE_ONE_WAY_SECONDS entry — that timing
                # (e.g. "Farmland to end/last location of
                # Mainland = 1.2 min") is the full door-to-door
                # time INCLUDING every stop, so ROUTE_HOP_SECONDS
                # already has the dwell folded in. Adding a
                # separate sleep here on top of it would make the
                # trip longer than what was configured.
                # ------------------------------------------------

                if ROUTE_HOP_SECONDS.get(
                    bus.route
                ) is None:

                    await asyncio.sleep(
                        BUS_STOP_DWELL_SECONDS
                    )

                # ------------------------------------------------
                # TRAVEL TIME
                #
                # BRT_ROUTE_ONE_WAY_SECONDS (config.py) gives this
                # route a fixed hop time (ROUTE_HOP_SECONDS), which
                # is what actually paces the bus. This is a
                # completely independent dial from private car
                # travel — it never touches MIN_TRAVEL_TIME_SECONDS
                # / MAX_TRAVEL_TIME_SECONDS / TRAVEL_SECONDS_PER_KM.
                # Road distance is still computed as a fallback for
                # any route that doesn't have a fixed hop time
                # configured, clamped using the BRT-only bounds.
                # ------------------------------------------------

                travel_time = ROUTE_HOP_SECONDS.get(
                    bus.route
                )

                if travel_time is None:

                    distance = self._road_distance(
                        bus.current_location,
                        next_stop
                    )

                    if distance is None:
                        return

                    travel_time = max(
                        BRT_MIN_TRAVEL_TIME_SECONDS,
                        min(
                            BRT_MAX_TRAVEL_TIME_SECONDS,
                            distance * BRT_SECONDS_PER_KM
                        )
                    )

                from cogs.weather import get_movement_multiplier
                travel_time *= get_movement_multiplier()

                # ------------------------------------------------
                # DEPARTURE
                # ------------------------------------------------

                channel = self._bus_channel(
                    bus
                )

                if channel:

                    await self._temporary_message(
                        channel,
                        (
                            f"🚌 **{bus.route}** is departing from "
                            f"**{self._location_name(bus.current_location)}**.\n"
                            f"Passengers onboard: "
                            f"**{len(bus.passengers)}/{BUS_CAPACITY}**"
                        ),
                        delay=BUS_MESSAGE_DELETE_DELAY,
                        silent=True
                    )

                # ------------------------------------------------
                # TRAVEL
                # ------------------------------------------------

                await asyncio.sleep(
                    travel_time
                )

                # ------------------------------------------------
                # ARRIVE
                # ------------------------------------------------

                bus.current_location = next_stop

                channel = self._bus_channel(
                    bus
                )

                if channel:

                    await self._temporary_message(
                        channel,
                        (
                            f"🚌 **{bus.route}** has arrived at "
                            f"**{self._location_name(bus.current_location)}**.\n"
                            f"Passengers onboard: "
                            f"**{len(bus.passengers)}/{BUS_CAPACITY}**"
                        ),
                        delay=BUS_MESSAGE_DELETE_DELAY,
                        silent=True
                    )

                # ------------------------------------------------
                # SAVE CURRENT BUS LOCATION
                # ------------------------------------------------

                self._save_bus_fleet()

        except asyncio.CancelledError:

            raise

        except Exception as error:

            print(
                f"BUS ERROR [{bus.route} #{bus.bus_id}]: "
                f"{error}"
            )

    # ========================================================
    # ROUTE START
    # ========================================================

    def _route_start(
        self,
        route: str
    ) -> Optional[str]:

        stops = ROUTE_STOPS.get(
            route
        )

        if not stops:
            return None

        return stops[0]

    # ========================================================
    # DIRECTION-AWARE STOP LIST
    #
    # Buses on the same route can be started from opposite
    # ends. A bus with direction=1 drives ROUTE_STOPS in the
    # normal (forward) order. A bus with direction=-1 starts
    # at the opposite end and drives the exact same stops in
    # reverse order, so the two buses set off toward each
    # other instead of both leaving from the same stop.
    # ========================================================

    def _stops_for_bus(
        self,
        bus: Bus
    ) -> list[str]:

        stops = ROUTE_STOPS.get(
            bus.route,
            []
        )

        if bus.direction == -1:
            return list(
                reversed(
                    stops
                )
            )

        return stops

        # ========================================================
    # NEXT BUS STOP
    # ========================================================

    def _next_bus_stop_by_index(
        self,
        bus: Bus,
        stop_index: int
    ) -> tuple[Optional[int], Optional[str]]:

        # ------------------------------------------------
        # NOTE: some routes (e.g. B1/B3) visit the same
        # stop name twice ("ghetto"). We must advance by
        # POSITION in the route, never by looking the stop
        # name back up with list.index(), which always
        # resolves to the FIRST occurrence and causes the
        # bus to loop between two stops forever instead of
        # completing its route.
        # ------------------------------------------------

        stops = self._stops_for_bus(
            bus
        )

        if not stops:
            return None, None

        next_index = stop_index + 1

        if next_index >= len(stops):
            return None, None

        return next_index, stops[next_index]
        # ========================================================
    # DISPATCH LOOP
    # ========================================================

    @tasks.loop(
        seconds=BUS_DEPARTURE_INTERVAL
    )
    async def bus_dispatch_loop(
        self
    ):

        print(
            "🚌🚌🚌 BUS DISPATCH LOOP RUNNING 🚌🚌🚌"
        )

        for route in BUS_ROUTES:

            buses = self.buses.get(
                route,
                []
            )

            queue = self.queues.get(
                route,
                deque()
            )

            print(
                f"🚌 ROUTE {route} | "
                f"BUSES={len(buses)} | "
                f"QUEUE={len(queue)}"
            )

            for bus in buses:

                print(
                    f"🚌 BUS #{bus.bus_id} | "
                    f"ROUTE={bus.route} | "
                    f"LOCATION={bus.current_location} | "
                    f"PASSENGERS={len(bus.passengers)}"
                )

            if not queue:

                print(
                    f"🚌 {route} QUEUE EMPTY"
                )

                continue

            # ------------------------------------------------
            # PICK THE NEAREST IDLE BUS TO SERVE THE QUEUE
            #
            # Only the bus closest (by road distance) to the
            # oldest waiting passenger's stop is dispatched.
            # The rest stay idle and get reconsidered next
            # tick — either for the same passenger (if this
            # bus doesn't have room) or for whoever is next in
            # line once it's serving them.
            # ------------------------------------------------

            idle_buses = [
                bus
                for bus in buses
                if bus.bus_id not in self.bus_tasks
            ]

            if not idle_buses:

                print(
                    f"🚌 {route} HAS NO IDLE BUSES"
                )

                continue

            front_passenger = queue[0]

            nearest_bus = None
            nearest_distance = None

            for candidate in idle_buses:

                origin = (
                    candidate.current_location
                    or self._stops_for_bus(
                        candidate
                    )[0]
                )

                distance = self._road_distance(
                    origin,
                    front_passenger.origin
                )

                if distance is None:
                    continue

                if (
                    nearest_distance is None
                    or distance < nearest_distance
                ):
                    nearest_distance = distance
                    nearest_bus = candidate

            if nearest_bus is None:

                print(
                    f"🚌 {route} COULD NOT DETERMINE "
                    f"NEAREST BUS"
                )

                continue

            print(
                f"🚌🚌🚌 STARTING BUS "
                f"{route} #{nearest_bus.bus_id} "
                f"(NEAREST TO "
                f"{front_passenger.origin}) "
                f"TO SERVE QUEUE 🚌🚌🚌"
            )

            task = asyncio.create_task(
                self._run_bus(
                    nearest_bus
                )
            )

            self.bus_tasks[
                nearest_bus.bus_id
            ] = task

            def done_callback(
                completed_task,
                bus_id=nearest_bus.bus_id
            ):

                self.bus_tasks.pop(
                    bus_id,
                    None
                )

                print(
                    f"🚌 BUS TASK FINISHED: "
                    f"#{bus_id}"
                )

                if completed_task.cancelled():

                    print(
                        f"🚌 BUS TASK #{bus_id} "
                        f"WAS CANCELLED"
                    )

                elif completed_task.exception():

                    print(
                        f"🚌 BUS TASK #{bus_id} "
                        f"FAILED: "
                        f"{completed_task.exception()}"
                    )

                else:

                    print(
                        f"🚌 BUS TASK #{bus_id} "
                        f"COMPLETED SUCCESSFULLY"
                    )

            task.add_done_callback(
                done_callback
            )
    # ========================================================
    # LOOP START
    # ========================================================

    @bus_dispatch_loop.before_loop
    async def before_bus_dispatch_loop(
        self
    ):

        await self.bot.wait_until_ready()

        print(
            "🚌 BUS DISPATCH LOOP READY"
        )


    # ========================================================
    # COG LOAD
    # ========================================================

    async def cog_load(
        self
    ):

        print(
            "🚌 COG LOAD RUNNING"
        )

        if not self.bus_dispatch_loop.is_running():

            self.bus_dispatch_loop.start()

            print(
                "🚌 BUS DISPATCH LOOP STARTED"
            )

        else:

            print(
                "🚌 BUS DISPATCH LOOP WAS ALREADY RUNNING"
            )


    # ========================================================
    # COG UNLOAD
    # ========================================================

    async def cog_unload(
        self
    ):

        print(
            "🚌 COG UNLOAD RUNNING"
        )

        self.bus_dispatch_loop.cancel()

        for task in self.bus_tasks.values():

            if not task.done():

                task.cancel()

        self.bus_tasks.clear()


# ================================================================
# DISCORD EXTENSION SETUP
# ================================================================

async def setup(
    bot: commands.Bot
):

    print(
        "🚌 BUS SETUP FUNCTION RUNNING"
    )

    cog = BusCog(
        bot
    )

    await bot.add_cog(
        cog
    )

    print(
        "🚌 BUS COG ADDED"
    )

    if not cog.bus_dispatch_loop.is_running():

        cog.bus_dispatch_loop.start()

        print(
            "🚌 BUS DISPATCH LOOP STARTED"
        )

    else:

        print(
            "🚌 BUS DISPATCH LOOP WAS ALREADY RUNNING"
        )
