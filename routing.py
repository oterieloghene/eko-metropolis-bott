"""
Shortest-path routing over the Eko Metropolis road network.

The road network uses zone hubs:

    Island
        ↓
    Mainland
        ↓
    Ghetto

Ghetto ↔ Island travel MUST pass through Mainland.

This is important because Mainland is the toll checkpoint
between Ghetto and Island.

TOLL RULE:

A toll is charged only when leaving a toll-controlled zone
and entering another zone.

Examples:

    dealership -> repair
    Mainland -> Mainland
    NO TOLL

    bank -> hospital
    Island -> Island
    NO TOLL

    dealership -> bank
    Mainland -> Island
    MAINLAND TOLL

    bank -> dealership
    Island -> Mainland
    ISLAND TOLL

    makoko -> island
    Ghetto -> Mainland -> Island
    MAINLAND TOLL

    island -> makoko
    Island -> Mainland -> Ghetto
    ISLAND TOLL
"""

import heapq

from config import (
    RAW_DISTANCES,
    LOCATIONS,
    TOLL_ZONES,
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
# REMOVE INVALID DIRECT CROSS-ZONE ROADS
# ============================================================
#
# IMPORTANT:
#
# Your config currently contains:
#
#     ("island", "ghetto"): 20
#
# That creates a direct Island <-> Ghetto road.
#
# This allows Dijkstra to bypass Mainland.
#
# We remove that direct connection so the only valid road is:
#
#     Island <-> Mainland <-> Ghetto
#
# This ensures the Mainland toll checkpoint is encountered.
# ============================================================

GRAPH.get("island", {}).pop("ghetto", None)
GRAPH.get("ghetto", {}).pop("island", None)


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
    Find the shortest valid road route using Dijkstra's
    algorithm.

    Returns:

        (
            path,
            total_distance_km
        )

    Examples:

        dealership -> bank

        [
            "dealership",
            "mainland",
            "island",
            "bank"
        ]

        makoko -> bank

        [
            "makoko",
            "ghetto",
            "mainland",
            "island",
            "bank"
        ]
    """

    origin = str(origin).strip().lower()
    destination = str(destination).strip().lower()

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
    # RECONSTRUCT PATH
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
                f"between '{origin}' and "
                f"'{destination}'."
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

def _zone_for(
    code: str
) -> str | None:

    """
    Return the actual zone for a location.

    Examples:

        bank       -> island
        dealership -> mainland
        makoko     -> ghetto
        farmland   -> farmland
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
    Determine which toll gates must be paid.

    A toll is generated when the route changes zones.

    The toll returned is the zone being LEFT.

    Examples:

        dealership -> repair

        Mainland -> Mainland

        []

        dealership -> bank

        Mainland -> Island

        ["mainland"]

        bank -> dealership

        Island -> Mainland

        ["island"]

        makoko -> bank

        Ghetto
            ↓
        Mainland
            ↓
        Island

        ["mainland"]

        bank -> makoko

        Island
            ↓
        Mainland
            ↓
        Ghetto

        ["island"]
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
        # SAME ZONE
        # ----------------------------------------------------

        if current_zone == previous_zone:
            continue

        # ----------------------------------------------------
        # ZONE CHANGED
        #
        # The player is leaving previous_zone.
        # ----------------------------------------------------

        if previous_zone in TOLL_ZONES:

            if previous_zone not in tolls:

                tolls.append(
                    previous_zone
                )

        previous_zone = current_zone

    return tolls
