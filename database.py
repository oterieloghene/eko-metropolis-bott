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

BRT data includes:
    - BRT card ownership
    - BRT card balance

Bus data includes:
    - Purchased buses
    - Bus route
    - Bus status

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
    Create all database tables if they do not already exist.
    """

    with _lock:

        # ----------------------------------------------------
        # PLAYERS
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # TAXI DRIVERS
        # ----------------------------------------------------
        #
        # One row per registered taxi driver.
        #
        # tier   -> "standard" or "premium"
        # online -> 1 while visible/bookable via !taxistart,
        #           0 after !taxistop (default when registered)
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS taxi_drivers (
                user_id TEXT PRIMARY KEY,

                tier TEXT NOT NULL,

                online INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # ----------------------------------------------------
        # BRT CARDS
        # ----------------------------------------------------
        #
        # One player can have one BRT card.
        #
        # The card balance is stored here.
        #
        # The Discord role is handled by brt_card.py.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brt_cards (
                user_id TEXT PRIMARY KEY,

                balance INTEGER NOT NULL DEFAULT 0,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # BUSES
        # ----------------------------------------------------
        #
        # Buses are purchased by the Mayor of Eko.
        #
        # Purchase price is currently ₦0.
        #
        # route:
        #     B1
        #     B2
        #     B3
        #
        # status:
        #     available
        #     active
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buses (
                bus_id INTEGER PRIMARY KEY AUTOINCREMENT,

                route TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'available',

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # FLIGHTS
        # ----------------------------------------------------
        #
        # One active flight per player.
        #
        # status:
        #     booked      -> paid, waiting to check in at agency
        #     in_transit  -> checked in, flying, not yet arrived
        #     on_vacation -> arrived at destination
        #     returning   -> flying back (kept for symmetry;
        #                    return is currently instant)
        #
        # missed_count -> 0 normally, 1 after first missed
        #                 check-in (rescheduled), forfeited
        #                 (row deleted) after the second miss.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flights (
                user_id TEXT PRIMARY KEY,

                destination TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'booked',

                price_paid INTEGER NOT NULL,

                stay_seconds INTEGER NOT NULL,

                departure_at TIMESTAMP NOT NULL,

                arrival_at TIMESTAMP,

                return_at TIMESTAMP,

                missed_count INTEGER NOT NULL DEFAULT 0,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
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

    if (
        "vehicles" in fields
        and isinstance(fields["vehicles"], list)
    ):

        fields["vehicles"] = json.dumps(
            fields["vehicles"]
        )

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
# BRT CARD FUNCTIONS
# ============================================================

def has_brt_card(
    user_id: int
) -> bool:

    """
    Check whether a player owns a BRT card.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT user_id
            FROM brt_cards
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        return cur.fetchone() is not None


def create_brt_card(
    user_id: int
) -> bool:

    """
    Create a BRT card for a player.

    Returns:
        True  = card created
        False = player already has a card
    """

    with _lock:

        cur = _conn.execute(
            """
            INSERT OR IGNORE INTO brt_cards (
                user_id,
                balance
            )
            VALUES (
                ?,
                0
            )
            """,
            (str(user_id),)
        )

        _conn.commit()

        return cur.rowcount > 0


def get_brt_balance(
    user_id: int
) -> int:

    """
    Return BRT card balance.

    Players without a card have ₦0 balance.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT balance
            FROM brt_cards
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        row = cur.fetchone()

        if row is None:
            return 0

        return int(row["balance"])


def set_brt_balance(
    user_id: int,
    amount: int
) -> None:

    """
    Set the BRT card balance.
    """

    amount = max(
        0,
        int(amount)
    )

    with _lock:

        _conn.execute(
            """
            UPDATE brt_cards
            SET balance = ?
            WHERE user_id = ?
            """,
            (
                amount,
                str(user_id)
            )
        )

        _conn.commit()


def add_brt_balance(
    user_id: int,
    amount: int
) -> bool:

    """
    Add money to a BRT card.

    Returns False if the player does not own
    a BRT card.
    """

    amount = int(amount)

    if amount <= 0:
        return False

    with _lock:

        cur = _conn.execute(
            """
            UPDATE brt_cards
            SET balance = balance + ?
            WHERE user_id = ?
            """,
            (
                amount,
                str(user_id)
            )
        )

        _conn.commit()

        return cur.rowcount > 0


def deduct_brt_balance(
    user_id: int,
    amount: int
) -> bool:

    """
    Deduct money from a BRT card.

    Deduction only succeeds when the card has
    sufficient funds.

    Returns:
        True  = deduction successful
        False = insufficient funds / no card
    """

    amount = int(amount)

    if amount <= 0:
        return False

    with _lock:

        cur = _conn.execute(
            """
            UPDATE brt_cards
            SET balance = balance - ?
            WHERE user_id = ?
              AND balance >= ?
            """,
            (
                amount,
                str(user_id),
                amount
            )
        )

        _conn.commit()

        return cur.rowcount > 0

# ============================================================
# BUS DATABASE FUNCTIONS
# ============================================================

def purchase_bus(
    route: str
) -> int:

    """
    Purchase a bus.

    Bus purchase currently costs ₦0.

    Returns the new bus ID.
    """

    route = str(
        route
    ).strip().upper()

    with _lock:

        cur = _conn.execute(
            """
            INSERT INTO buses (
                route,
                status
            )
            VALUES (
                ?,
                'available'
            )
            """,
            (
                route
            )
        )

        _conn.commit()

        return int(
            cur.lastrowid
        )


def get_bus(
    bus_id: int
) -> sqlite3.Row | None:

    """
    Return one bus.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM buses
            WHERE bus_id = ?
            """,
            (
                int(bus_id)
            )
        )

        return cur.fetchone()


def get_all_buses() -> list[sqlite3.Row]:

    """
    Return every purchased bus.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM buses
            ORDER BY bus_id ASC
            """
        )

        return cur.fetchall()


def get_buses_by_route(
    route: str
) -> list[sqlite3.Row]:

    """
    Return all buses assigned to a route.
    """

    route = str(
        route
    ).strip().upper()

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM buses
            WHERE route = ?
            ORDER BY bus_id ASC
            """,
            (
                route
            )
        )

        return cur.fetchall()


def set_bus_status(
    bus_id: int,
    status: str
) -> None:

    """
    Change the status of a bus.

    Examples:

        available
        active
    """

    with _lock:

        _conn.execute(
            """
            UPDATE buses
            SET status = ?
            WHERE bus_id = ?
            """,
            (
                str(status),
                int(bus_id)
            )
        )

        _conn.commit()


def count_buses() -> int:

    """
    Return the total number of purchased buses.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT COUNT(*)
            FROM buses
            """
        )

        row = cur.fetchone()

        return int(
            row[0]
        )


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

    BRT cards are NOT deleted here.
    Bus purchases are NOT deleted here.

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

    NOTE:
        This also removes BRT cards and purchased buses
        because the entire database is reset.
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

        # ----------------------------------------------------
        # Delete player data
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM players
            """
        )

        # ----------------------------------------------------
        # Delete BRT cards
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM brt_cards
            """
        )

        # ----------------------------------------------------
        # Delete purchased buses
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM buses
            """
        )

        # ----------------------------------------------------
        # Reset bus auto-increment counter
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM sqlite_sequence
            WHERE name = 'buses'
            """
        )

        _conn.commit()

        return count


# ============================================================
# TAXI DRIVERS
# ============================================================

def get_taxi_driver(
    user_id: int
) -> sqlite3.Row | None:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM taxi_drivers
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        return cur.fetchone()


def register_taxi_driver(
    user_id: int,
    tier: str
) -> None:

    """
    Register a new taxi driver, or overwrite an existing
    registration with a new tier. Always starts offline —
    the player must !taxistart to become bookable.
    """

    with _lock:

        _conn.execute(
            """
            INSERT INTO taxi_drivers (user_id, tier, online)
            VALUES (?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                tier = excluded.tier,
                online = 0
            """,
            (str(user_id), tier)
        )

        _conn.commit()


def set_taxi_online(
    user_id: int,
    online: bool
) -> None:

    with _lock:

        _conn.execute(
            """
            UPDATE taxi_drivers
            SET online = ?
            WHERE user_id = ?
            """,
            (1 if online else 0, str(user_id))
        )

        _conn.commit()


def get_online_taxi_drivers(
    tier: str
) -> list[sqlite3.Row]:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM taxi_drivers
            WHERE tier = ? AND online = 1
            """,
            (tier,)
        )

        return cur.fetchall()


# ============================================================
# FLIGHTS
# ============================================================

def get_flight(
    user_id: int
) -> sqlite3.Row | None:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM flights
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        return cur.fetchone()


def book_flight(
    user_id: int,
    destination: str,
    price_paid: int,
    stay_seconds: int,
    departure_at: str
) -> None:

    """
    Create a new booked flight for a player.

    Assumes the player has no existing flight row —
    callers should check get_flight() first.
    """

    with _lock:

        _conn.execute(
            """
            INSERT INTO flights (
                user_id,
                destination,
                status,
                price_paid,
                stay_seconds,
                departure_at
            )
            VALUES (?, ?, 'booked', ?, ?, ?)
            """,
            (
                str(user_id),
                destination,
                int(price_paid),
                int(stay_seconds),
                departure_at
            )
        )

        _conn.commit()


def update_flight(
    user_id: int,
    **fields
) -> None:

    if not fields:
        return

    allowed_columns = {
        "destination",
        "status",
        "price_paid",
        "stay_seconds",
        "departure_at",
        "arrival_at",
        "return_at",
        "missed_count",
    }

    invalid_columns = set(fields) - allowed_columns

    if invalid_columns:

        raise ValueError(
            f"Invalid flight field(s): "
            f"{', '.join(sorted(invalid_columns))}"
        )

    set_clause = ", ".join(
        f"{column} = ?"
        for column in fields
    )

    values = list(fields.values())
    values.append(str(user_id))

    with _lock:

        _conn.execute(
            f"""
            UPDATE flights
            SET {set_clause}
            WHERE user_id = ?
            """,
            values
        )

        _conn.commit()


def delete_flight(
    user_id: int
) -> None:

    with _lock:

        _conn.execute(
            """
            DELETE FROM flights
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        _conn.commit()


def flights_by_status(
    status: str
) -> list[sqlite3.Row]:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM flights
            WHERE status = ?
            """,
            (status,)
        )

        return cur.fetchall()


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
