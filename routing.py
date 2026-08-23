"""
Shortest-path routing over the Eko Metropolis road network.

Locations connect to zone hubs, and zone hubs connect to other zones.

TOLL RULE:
A toll is charged ONLY when the player is LEAVING a toll-controlled zone.

Examples:

    dealership -> repair
    Mainland -> Mainland
    NO TOLL

    dealership -> bank
    Mainland -> Island
    MAINLAND TOLL

    bank -> hospital
    Island -> Island
    NO TOLL

    makoko -> ajegunle
    Ghetto -> Ghetto
    NO TOLL
"""

import heapq

from config import (
    RAW_DISTANCES,
    LOCATIONS,
    TOLL_ZONES,
    ZONE_HUBS,
)


# ============================================================
# BUILD ROAD GRAPH
# ============================================================

GRAPH: dict[str, dict[str, float]] = {}


def _add_edge(
    a: str,
    b: str,
    dist: float
) -> None:

    GRAPH.setdefault(a, {})[b] = dist
    GRAPH.setdefault(b, {})[a] = dist


for (a, b), dist in RAW_DISTANCES.items():
    _add_edge(a, b, dist)


# ============================================================
# ROUTE ERROR
# ============================================================

class NoRouteError(Exception):
    pass


# ============================================================
# FIND SHORTEST ROUTE
# ============================================================

def find_route(
    origin: str,
    destination: str
) -> tuple[list[str], float]:
    """
    Find the shortest road route using Dijkstra's algorithm.

    Returns:

        (
            path,
            total_distance_km
        )

    Example:

        [
            "dealership",
            "mainland",
            "island",
            "bank"
        ]
    """

    if origin not in GRAPH:
        raise NoRouteError(
            f"No road route from '{origin}'."
        )

    if destination not in GRAPH:
        raise NoRouteError(
            f"No road route to '{destination}'."
        )

    distances = {
        origin: 0.0
    }

    previous: dict[str, str] = {}

    visited = set()

    heap = [
        (0.0, origin)
    ]

    while heap:

        current_distance, node = heapq.heappop(heap)

        if node in visited:
            continue

        visited.add(node)

        if node == destination:
            break

        for neighbor, edge_distance in GRAPH.get(
            node,
            {}
        ).items():

            new_distance = (
                current_distance
                + edge_distance
            )

            if new_distance < distances.get(
                neighbor,
                float("inf")
            ):

                distances[neighbor] = new_distance

                previous[neighbor] = node

                heapq.heappush(
                    heap,
                    (
                        new_distance,
                        neighbor
                    )
                )

    if destination not in distances:
        raise NoRouteError(
            f"No road route between "
            f"'{origin}' and '{destination}'."
        )

    # --------------------------------------------------------
    # Reconstruct path
    # --------------------------------------------------------

    path = [
        destination
    ]

    while path[-1] != origin:

        previous_node = previous.get(
            path[-1]
        )

        if previous_node is None:
            raise NoRouteError(
                f"Unable to reconstruct route "
                f"between '{origin}' and '{destination}'."
            )

        path.append(
            previous_node
        )

    path.reverse()

    return (
        path,
        distances[destination]
    )


# ============================================================
# ZONE LOOKUP
# ============================================================

def _zone_for(code: str) -> str | None:
    """
    Return the actual zone for a location.

    Zone hubs return their own zone.
    Normal locations return the zone stored in LOCATIONS.
    """

    location = LOCATIONS.get(code)

    if location is None:
        return None

    return location.get("zone")


# ============================================================
# TOLL DETECTION
# ============================================================

def tolls_on_route(
    path: list[str]
) -> list[str]:
    """
    Determine which toll gates must be paid on a route.

    IMPORTANT RULE:

    A toll is NOT charged simply because a route contains
    'mainland' or 'island'.

    A toll is charged only when the player is leaving that
    toll-controlled zone and entering another zone.

    Therefore:

        dealership -> repair
        Mainland -> Mainland
        = NO TOLL

        bank -> hospital
        Island -> Island
        = NO TOLL

        dealership -> bank
        Mainland -> Island
        = MAINLAND TOLL

        bank -> dealership
        Island -> Mainland
        = ISLAND TOLL

    The toll returned is the zone being LEFT.
    """

    if len(path) < 2:
        return []

    tolls: list[str] = []

    previous_zone = _zone_for(
        path[0]
    )

    if previous_zone is None:
        return []

    for node in path[1:]:

        current_zone = _zone_for(
            node
        )

        if current_zone is None:
            continue

        # ----------------------------------------------------
        # Same zone:
        #
        # No toll.
        #
        # Example:
        # dealership -> repair
        # Mainland -> Mainland
        # ----------------------------------------------------

        if current_zone == previous_zone:
            continue

        # ----------------------------------------------------
        # Zone changed.
        #
        # The player is leaving previous_zone.
        #
        # If that zone has a toll, charge it.
        # ----------------------------------------------------

        if previous_zone in TOLL_ZONES:

            if previous_zone not in tolls:
                tolls.append(
                    previous_zone
                )

        previous_zone = current_zone

    return tolls
