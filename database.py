"""
Database layer.

SQLite file by default (DB_PATH env var).
The database is the authoritative source for player data.

Player data includes:
    - Balance
    - Current location
    - Current vehicle
    - Vehicle location
    - Fuel
    - Vehicle condition
    - Owned vehicles
    - Traveling status

A complete database reset can be triggered through the admin command.
"""

import json
import os
import sqlite3
import threading

from config import STARTING_BALANCE, STARTING_LOCATION


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

DB_PATH = os.environ.get(
    "DB_PATH",
    "ekobot.db"
)


_lock = threading.Lock()

_conn = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

_conn.row_factory = sqlite3.Row


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db() -> None:
    """
    Create the players table if it does not already exist.
    """

    with _lock:

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id TEXT PRIMARY KEY,

                balance INTEGER NOT NULL,

                location TEXT NOT NULL,

                vehicle TEXT,

                vehicle_location TEXT,

                fuel REAL NOT NULL DEFAULT 0,

                vehicle_condition REAL NOT NULL DEFAULT 100,

                vehicles TEXT NOT NULL DEFAULT '[]',

                traveling INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        _conn.commit()


# ============================================================
# GET PLAYER
# ============================================================

def get_player(
    user_id: int
) -> sqlite3.Row | None:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM players
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        return cur.fetchone()


# ============================================================
# CREATE PLAYER
# ============================================================

def create_player(
    user_id: int
) -> sqlite3.Row:

    """
    Create a completely fresh player.

    New players receive:

        Balance = STARTING_BALANCE
        Location = STARTING_LOCATION
        Vehicle = None
        Vehicle location = None
        Fuel = 0
        Vehicle condition = 100
        Vehicles = []
        Traveling = 0
    """

    existing = get_player(user_id)

    if existing:
        return existing

    with _lock:

        _conn.execute(
            """
            INSERT INTO players
            (
                user_id,
                balance,
                location,
                vehicle,
                vehicle_location,
                fuel,
                vehicle_condition,
                vehicles,
                traveling
            )
            VALUES
            (
                ?,
                ?,
                ?,
                NULL,
                NULL,
                0,
                100,
                '[]',
                0
            )
            """,
            (
                str(user_id),
                STARTING_BALANCE,
                STARTING_LOCATION
            )
        )

        _conn.commit()

    return get_player(user_id)


# ============================================================
# GET OR CREATE PLAYER
# ============================================================

def get_or_create_player(
    user_id: int
) -> sqlite3.Row:

    player = get_player(user_id)

    if player is None:

        player = create_player(
            user_id
        )

    return player


# ============================================================
# UPDATE PLAYER
# ============================================================

def update_player(
    user_id: int,
    **fields
) -> None:

    """
    Update one or more player fields.

    Example:

        update_player(
            user_id,
            balance=500000,
            location="dealership"
        )

    The vehicles field can be supplied as a Python list.
    It will automatically be converted to JSON.
    """

    if not fields:
        return

    # --------------------------------------------------------
    # Convert vehicle list to JSON before storing.
    # --------------------------------------------------------

    if (
        "vehicles" in fields
        and isinstance(fields["vehicles"], list)
    ):

        fields["vehicles"] = json.dumps(
            fields["vehicles"]
        )

    # --------------------------------------------------------
    # Build SQL update.
    # --------------------------------------------------------

    set_clause = ", ".join(
        f"{column} = ?"
        for column in fields
    )

    values = list(
        fields.values()
    )

    values.append(
        str(user_id)
    )

    with _lock:

        _conn.execute(
            f"""
            UPDATE players
            SET {set_clause}
            WHERE user_id = ?
            """,
            values
        )

        _conn.commit()


# ============================================================
# GET ALL PLAYERS
# ============================================================

def all_players() -> list[sqlite3.Row]:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM players
            """
        )

        return cur.fetchall()


# ============================================================
# GET PLAYER VEHICLES
# ============================================================

def get_vehicles(
    user_id: int
) -> list:

    """
    Return the player's complete owned-vehicle list.

    A player can own multiple vehicles.
    """

    player = get_player(
        user_id
    )

    if not player:
        return []

    try:

        vehicles = json.loads(
            player["vehicles"] or "[]"
        )

        if isinstance(
            vehicles,
            list
        ):
            return vehicles

        return []

    except (
        json.JSONDecodeError,
        TypeError
    ):

        return []


# ============================================================
# RESET ALL VEHICLE DATA
# ============================================================

def reset_all_vehicle_data() -> None:

    """
    Reset vehicle information for ALL existing players.

    This clears:

        vehicle
        vehicle_location
        fuel
        vehicle_condition
        vehicles

    It does NOT change:

        balance
        location
        traveling
    """

    with _lock:

        _conn.execute(
            """
            UPDATE players
            SET
                vehicle = NULL,
                vehicle_location = NULL,
                fuel = 0,
                vehicle_condition = 100,
                vehicles = '[]'
            """
        )

        _conn.commit()


# ============================================================
# RESET ALL LOCATIONS
# ============================================================

def reset_all_locations(
    new_location: str = STARTING_LOCATION
) -> int:

    """
    Move ALL existing players to the specified location.

    Example:

        reset_all_locations("dealership")

    Returns the number of player records updated.
    """

    with _lock:

        cur = _conn.execute(
            """
            UPDATE players
            SET
                location = ?,
                traveling = 0
            """,
            (
                new_location,
            )
        )

        _conn.commit()

        return cur.rowcount


# ============================================================
# COMPLETE DATABASE RESET
# ============================================================

def reset_database() -> int:

    """
    Completely wipe ALL player records.

    IMPORTANT:

    This does NOT delete the SQLite database file.
    It only deletes every row from the players table.

    The table itself remains intact.

    After this reset, players are treated as completely new
    players the next time get_or_create_player() is called.

    New players will receive:

        STARTING_BALANCE
        STARTING_LOCATION

    And:

        vehicle = None
        vehicle_location = None
        fuel = 0
        vehicle_condition = 100
        vehicles = []
        traveling = 0

    Returns:
        Number of player records deleted.
    """

    with _lock:

        # ----------------------------------------------------
        # Count existing players first.
        # ----------------------------------------------------

        cur = _conn.execute(
            """
            SELECT COUNT(*)
            FROM players
            """
        )

        row = cur.fetchone()

        count = (
            int(row[0])
            if row
            else 0
        )

        # ----------------------------------------------------
        # Delete every player record.
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM players
            """
        )

        # ----------------------------------------------------
        # Reset SQLite auto bookkeeping where applicable.
        # ----------------------------------------------------

        _conn.commit()

        return count


# ============================================================
# CLOSE DATABASE
# ============================================================

def close_db() -> None:

    """
    Safely close the SQLite connection.
    """

    with _lock:

        try:
            _conn.close()

        except Exception:
            pass
