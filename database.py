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

Admin reset functions:
    - reset_all_player_data()
    - reset_all_vehicle_data()
    - reset_all_locations()
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
    Create a fresh player.

    New player defaults:

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
            INSERT INTO players (
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
            VALUES (
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
        player = create_player(user_id)

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
    """

    if not fields:
        return

    # --------------------------------------------------------
    # Prevent accidental updates to unknown database columns.
    # --------------------------------------------------------

    allowed_columns = {
        "user_id",
        "balance",
        "location",
        "vehicle",
        "vehicle_location",
        "fuel",
        "vehicle_condition",
        "vehicles",
        "traveling",
    }

    invalid_columns = set(fields) - allowed_columns

    if invalid_columns:

        raise ValueError(
            f"Invalid player field(s): "
            f"{', '.join(sorted(invalid_columns))}"
        )

    # --------------------------------------------------------
    # Convert vehicle list to JSON.
    # --------------------------------------------------------

    if (
        "vehicles" in fields
        and isinstance(fields["vehicles"], list)
    ):

        fields["vehicles"] = json.dumps(
            fields["vehicles"]
        )

    # --------------------------------------------------------
    # Build UPDATE statement.
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
    Return the complete owned-vehicle list.

    Players can own multiple vehicles.
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
# RESET ALL PLAYER DATA
# ============================================================

def reset_all_player_data() -> int:

    """
    COMPLETE RESET OF EXISTING PLAYER DATA.

    Player records are KEPT.

    Every existing player is reset to:

        Balance          = STARTING_BALANCE
        Location         = STARTING_LOCATION
        Vehicle          = None
        Vehicle location = None
        Fuel             = 0
        Condition        = 100
        Vehicles         = []
        Traveling        = 0

    This is the function used by:

        !resetdatabase

    Returns the number of players reset.
    """

    with _lock:

        cur = _conn.execute(
            """
            UPDATE players
            SET
                balance = ?,
                location = ?,
                vehicle = NULL,
                vehicle_location = NULL,
                fuel = 0,
                vehicle_condition = 100,
                vehicles = '[]',
                traveling = 0
            """,
            (
                STARTING_BALANCE,
                STARTING_LOCATION
            )
        )

        _conn.commit()

        return cur.rowcount


# ============================================================
# RESET ALL VEHICLE DATA
# ============================================================

def reset_all_vehicle_data() -> None:

    """
    Remove all vehicle data from every existing player.

    Does NOT change:

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
    Move ALL existing players to a specified location.

    Example:

        reset_all_locations("dealership")

    Also stops any active journey.

    Returns the number of players updated.
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
                new_location
            )
        )

        _conn.commit()

        return cur.rowcount


# ============================================================
# COMPLETE DATABASE WIPE
# ============================================================

def reset_database() -> int:

    """
    Completely DELETE all player records.

    This is different from reset_all_player_data().

    reset_all_player_data():
        Keeps player records and resets their data.

    reset_database():
        Deletes every player record completely.

    Returns the number of deleted records.
    """

    with _lock:

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

        _conn.execute(
            """
            DELETE FROM players
            """
        )

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
