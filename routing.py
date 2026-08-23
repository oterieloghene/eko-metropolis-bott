"""
Shortest-path routing over the location graph.

Every location connects to its zone hub; zone hubs connect to each other.
!drive's origin is ALWAYS the player's database location, never the channel
the command was typed in (requirements #5, #17).
"""

import heapq
from config import RAW_DISTANCES, LOCATIONS, TOLL_ZONES, ZONE_HUBS

# Build an undirected adjacency graph from the raw distance table.
GRAPH: dict[str, dict[str, float]] = {}


def _add_edge(a: str, b: str, dist: float) -> None:
    GRAPH.setdefault(a, {})[b] = dist
    GRAPH.setdefault(b, {})[a] = dist


for (a, b), dist in RAW_DISTANCES.items():
    _add_edge(a, b, dist)


class NoRouteError(Exception):
    pass


def find_route(origin: str, destination: str) -> tuple[list[str], float]:
    """
    Dijkstra shortest path. Returns (path, total_distance_km).
    path is a list of location/zone codes from origin to destination inclusive.
    """
    if origin not in GRAPH or destination not in GRAPH:
        raise NoRouteError(f"No road route between '{origin}' and '{destination}'.")

    dist = {origin: 0.0}
    prev: dict[str, str] = {}
    visited = set()
    heap = [(0.0, origin)]

    while heap:
        d, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == destination:
            break
        for neighbor, edge_dist in GRAPH.get(node, {}).items():
            nd = d + edge_dist
            if nd < dist.get(neighbor, float("inf")):
                dist[neighbor] = nd
                prev[neighbor] = node
                heapq.heappush(heap, (nd, neighbor))

    if destination not in dist:
        raise NoRouteError(f"No road route between '{origin}' and '{destination}'.")

    # reconstruct path
    path = [destination]
    while path[-1] != origin:
        path.append(prev[path[-1]])
    path.reverse()

    return path, dist[destination]


def tolls_on_route(path: list[str]) -> list[str]:
    """
    Return the ordered list of toll-zone codes ("mainland", "island") that a
    route actually passes through — excluding the origin and destination
    themselves when they are not zone hubs the player is merely transiting.
    A toll only triggers when the journey reaches that checkpoint, so we
    only count a zone hub as a toll if the player is *passing through* it
    (i.e. it's not their literal origin location or literal destination).
    """
    tolls = []
    for node in path:
        if node in TOLL_ZONES and node != path[0] and node != path[-1]:
            tolls.append(node)
    return tolls
