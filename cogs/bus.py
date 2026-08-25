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
from config import (
    LOCATIONS,
    RAW_DISTANCES,
    ROAD_DESTINATIONS,
    OVERSEAS,
    MIN_TRAVEL_TIME_SECONDS,
    MAX_TRAVEL_TIME_SECONDS,
    TRAVEL_SECONDS_PER_KM,
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

MAYOR_ROLE = "Mayor of Eko"

BRT_ROLE = "BRT Card"

BUS_MESSAGE_DELETE_DELAY = 8

BUS_DEPARTURE_INTERVAL = 60

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
        delay: int = BUS_MESSAGE_DELETE_DELAY
    ):

        try:

            message = await destination.send(
                content
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
                            current_location=current_location
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
                    )
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

        purchased = []

        for _ in range(quantity):

            bus = Bus(
                bus_id=self.next_bus_id,
                route=route,
                passengers=[],
                current_location=self._route_start(
                    route
                )
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

        while (
            queue
            and len(bus.passengers)
            < BUS_CAPACITY
        ):

            passenger = queue[0]

            # ------------------------------------------------
            # BUS MUST BE AT THE PASSENGER'S ZONE
            # ------------------------------------------------

            passenger_zone = self._zone(
                passenger.origin
            )

            bus_zone = self._zone(
                bus.current_location
            )

            if (
                passenger_zone is None
                or bus_zone is None
                or passenger_zone != bus_zone
            ):

                break

            queue.popleft()

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

    # ========================================================
    # DROP PASSENGERS
    # ========================================================

    async def _drop_passengers(
        self,
        bus: Bus
    ) -> None:

        remaining_passengers = []

        bus_zone = self._zone(
            bus.current_location
        )

        for passenger in bus.passengers:

            destination_zone = self._zone(
                passenger.destination
            )

            if (
                bus_zone is not None
                and destination_zone == bus_zone
            ):

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
            # INITIAL BUS LOCATION
            # ------------------------------------------------

            if (
                bus.current_location is None
            ):

                bus.current_location = (
                    self._route_start(
                        bus.route
                    )
                )

            if bus.current_location is None:
                return

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
            # BUS STAYS AT STOP
            # ------------------------------------------------

            if bus.passengers:

                await asyncio.sleep(
                    BUS_STOP_DWELL_SECONDS
                )

            # ------------------------------------------------
            # NEXT STOP
            # ------------------------------------------------

            next_stop = self._next_bus_stop(
                bus.route,
                bus.current_location
            )

            if next_stop is None:
                return

            # ------------------------------------------------
            # TRAVEL TO NEXT STOP
            # ------------------------------------------------

            distance = self._road_distance(
                bus.current_location,
                next_stop
            )

            if distance is None:
                return

            travel_time = max(
                MIN_TRAVEL_TIME_SECONDS,
                min(
                    MAX_TRAVEL_TIME_SECONDS,
                    distance * TRAVEL_SECONDS_PER_KM
                )
            )

            # ------------------------------------------------
            # DEPARTURE
            # ------------------------------------------------

            await self._temporary_message(
                self._bus_channel(
                    bus
                ),
                (
                    f"🚌 **{bus.route}** is departing from "
                    f"**{self._location_name(bus.current_location)}**.\n"
                    f"Passengers onboard: "
                    f"**{len(bus.passengers)}/{BUS_CAPACITY}**"
                ),
                delay=BUS_MESSAGE_DELETE_DELAY
            )

            await asyncio.sleep(
                travel_time
            )

            bus.current_location = next_stop

            # ------------------------------------------------
            # ARRIVAL
            # ------------------------------------------------

            await self._temporary_message(
                self._bus_channel(
                    bus
                ),
                (
                    f"🚌 **{bus.route}** has arrived at "
                    f"**{self._location_name(bus.current_location)}**.\n"
                    f"Passengers onboard: "
                    f"**{len(bus.passengers)}/{BUS_CAPACITY}**"
                ),
                delay=BUS_MESSAGE_DELETE_DELAY
            )

            # ------------------------------------------------
            # DROP PASSENGERS
            # ------------------------------------------------

            await self._drop_passengers(
                bus
            )

            # ------------------------------------------------
            # BOARD NEW PASSENGERS
            # ------------------------------------------------

            await self._board_passengers(
                bus
            )

            # ------------------------------------------------
            # SAVE BUS LOCATION
            # ------------------------------------------------

            self._save_bus_fleet()

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

        if route == "B1":
            return "farmland"

        if route == "B2":
            return "mainland"

        if route == "B3":
            return "farmland"

        return None

    # ========================================================
    # NEXT BUS STOP
    # ========================================================

    def _next_bus_stop(
        self,
        route: str,
        current_location: str
    ) -> Optional[str]:

        route_stops = {
            "B1": [
                "farmland",
                "ghetto",
                "mainland",
                "ghetto",
                "farmland",
            ],

            "B2": [
                "mainland",
                "island",
                "mainland",
            ],

            "B3": [
                "farmland",
                "ghetto",
                "island",
                "ghetto",
                "farmland",
            ],
        }

        stops = route_stops.get(
            route
        )

        if not stops:
            return None

        if current_location not in stops:
            return None

        index = stops.index(
            current_location
        )

        if index >= len(stops) - 1:
            return None

        return stops[
            index + 1
            ]

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
            "🚌 BUS DISPATCH LOOP RUNNING"
        )

        for route in BUS_ROUTES:

            buses = self.buses.get(
                route,
                []
            )

            print(
                f"🚌 ROUTE {route}: "
                f"{len(buses)} bus(es), "
                f"{len(self.queues[route])} queued passenger(s)"
            )

            for bus in buses:

                print(
                    f"🚌 CHECKING {route} "
                    f"BUS #{bus.bus_id} "
                    f"LOCATION={bus.current_location}"
                )

                if bus.bus_id in self.bus_tasks:

                    print(
                        f"🚌 BUS #{bus.bus_id} "
                        f"is already running."
                    )

                    continue

                if not self.queues[route]:

                    print(
                        f"🚌 {route} has no passengers waiting."
                    )

                    continue

                print(
                    f"🚌 STARTING BUS "
                    f"{route} #{bus.bus_id}"
                )

                task = asyncio.create_task(
                    self._run_bus(
                        bus
                    )
                )

                self.bus_tasks[
                    bus.bus_id
                ] = task

                def done_callback(
                    completed_task,
                    bus_id=bus.bus_id
                ):

                    self.bus_tasks.pop(
                        bus_id,
                        None
                    )

                    print(
                        f"🚌 BUS TASK FINISHED: "
                        f"#{bus_id}"
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

    if not self.bus_dispatch_loop.is_running():

        self.bus_dispatch_loop.start()

        print(
            "🚌 BUS DISPATCH LOOP STARTED"
        )

# ========================================================
# COG UNLOAD
# ========================================================

async def cog_unload(
    self
):

    self.bus_dispatch_loop.cancel()

    for task in self.bus_tasks.values():

        if not task.done():

            task.cancel()

    self.bus_tasks.clear()


# ================================================================
# DISCORD EXTENSION SETUP
# ================================================================

print(
    "🚌 BUS.PY REACHED SETUP SECTION"
)

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
            "🚌 BUS DISPATCH LOOP STARTED FROM SETUP"
        )
