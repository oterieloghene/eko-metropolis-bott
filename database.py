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
    - One-time first-card starter bonus claimed status

Bank account data includes:
    - Whether a player has opened a bank account
      (the Bank App requires one; balance itself is the same
      Naira pool as players.balance, not a separate ledger)

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
import uuid

from config import STARTING_BALANCE, STARTING_LOCATION, EXCHANGE_STARTING_RATE


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

                traveling INTEGER NOT NULL DEFAULT 0,

                current_area TEXT,

                aed_balance INTEGER NOT NULL DEFAULT 0,

                mvr_balance INTEGER NOT NULL DEFAULT 0,

                airtime_balance INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # Older databases created before the overseas-areas/wallet
        # feature existed won't have these columns — add them if
        # missing so upgrades don't crash on startup (same pattern
        # used below for flights.return_reminded).
        _existing_player_columns = {
            row["name"]
            for row in _conn.execute("PRAGMA table_info(players)")
        }

        if "current_area" not in _existing_player_columns:
            _conn.execute("ALTER TABLE players ADD COLUMN current_area TEXT")

        if "aed_balance" not in _existing_player_columns:
            _conn.execute(
                "ALTER TABLE players ADD COLUMN aed_balance INTEGER NOT NULL DEFAULT 0"
            )

        if "mvr_balance" not in _existing_player_columns:
            _conn.execute(
                "ALTER TABLE players ADD COLUMN mvr_balance INTEGER NOT NULL DEFAULT 0"
            )

        if "airtime_balance" not in _existing_player_columns:
            _conn.execute(
                "ALTER TABLE players ADD COLUMN airtime_balance INTEGER NOT NULL DEFAULT 0"
            )

        # ----------------------------------------------------
        # HOTELS
        # ----------------------------------------------------
        #
        # One row per active hotel room booking. A room is only
        # ever active while the player is on vacation, so this
        # table is small/short-lived by design.
        #
        # tier          -> "standard" or "luxury"
        # room_number   -> 1..HOTEL_ROOMS_PER_TIER, unique per
        #                  (destination, tier) while active
        # thread_id     -> the Discord thread backing the room
        # guest_id      -> set only for an accepted luxury guest
        # service_index -> how many of the 3 room-service
        #                  deliveries have already gone out
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hotels (
                booker_id TEXT PRIMARY KEY,

                destination TEXT NOT NULL,

                tier TEXT NOT NULL,

                room_number INTEGER NOT NULL,

                thread_id TEXT NOT NULL,

                guest_id TEXT,

                price_paid INTEGER NOT NULL,

                checked_in_at TIMESTAMP NOT NULL,

                stay_seconds INTEGER NOT NULL,

                service_index INTEGER NOT NULL DEFAULT 0,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
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
        # DISPATCH RIDERS
        # ----------------------------------------------------
        #
        # One row per registered dispatch rider. Same shape as
        # taxi_drivers on purpose (see cogs/dispatch.py, which
        # mirrors cogs/taxi.py's matching/queue engine).
        #
        # tier   -> "standard" (bicycle) or "premium" (motorcycle)
        # online -> 1 while visible/bookable via !dispatchstart,
        #           0 after !dispatchstop (default when registered)
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispatch_riders (
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

        # Older databases created before the BRT first-card bonus
        # existed won't have this column — add it if missing.
        _existing_brt_columns = {
            row["name"]
            for row in _conn.execute("PRAGMA table_info(brt_cards)")
        }

        if "starter_bonus_given" not in _existing_brt_columns:
            _conn.execute(
                "ALTER TABLE brt_cards ADD COLUMN "
                "starter_bonus_given INTEGER NOT NULL DEFAULT 0"
            )

        # ----------------------------------------------------
        # BANK ACCOUNTS
        # ----------------------------------------------------
        #
        # A bank account is required before a player can open the
        # Bank App on their phone. The bank balance is NOT a
        # separate ledger — it is the same Naira pool as
        # players.balance. This table only tracks whether an
        # account exists (and when it was opened); all deposits,
        # withdrawals, and transfers read/write players.balance
        # directly.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_accounts (
                user_id TEXT PRIMARY KEY,

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
        #     pending_approval -> booked but not yet reviewed by
        #                 an Immigration Officer. No charge has
        #                 been taken and departure_at is just a
        #                 placeholder (ignored) until approved.
        #     booked      -> approved + paid, waiting to check in
        #                 at agency before the real departure_at
        #                 deadline (which only starts counting
        #                 once approved).
        #     in_transit  -> checked in, flying, not yet arrived
        #     on_vacation -> arrived at destination
        #     returning   -> flying back (kept for symmetry;
        #                    return is currently instant)
        #
        # missed_count -> 0 normally, 1 after first missed
        #                 check-in (rescheduled), forfeited
        #                 (row deleted) after the second miss.
        #                 Only counted once status is "booked" —
        #                 an unapproved pending request can never
        #                 be "missed".
        #
        # return_reminded -> 0 normally, set to 1 once the
        #                 "your vacation is ending soon" warning
        #                 has been sent, so it's never sent twice.
        #
        # officer_notified -> 0 normally, set to 1 once the
        #                 pending request has been posted to the
        #                 immigration office for review, so it's
        #                 never posted twice.
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

                return_reminded INTEGER NOT NULL DEFAULT 0,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Older databases created before return_reminded existed
        # won't have the column — add it if it's missing so
        # upgrades don't crash on startup.
        _existing_columns = {
            row["name"]
            for row in _conn.execute("PRAGMA table_info(flights)")
        }

        if "return_reminded" not in _existing_columns:

            _conn.execute(
                """
                ALTER TABLE flights
                ADD COLUMN return_reminded INTEGER NOT NULL DEFAULT 0
                """
            )

        if "officer_notified" not in _existing_columns:

            _conn.execute(
                """
                ALTER TABLE flights
                ADD COLUMN officer_notified INTEGER NOT NULL DEFAULT 0
                """
            )

        # ----------------------------------------------------
        # MECHANICS
        # ----------------------------------------------------
        #
        # One row per player with the Mechanic role who has ever
        # gone online. online -> 1 while bookable via
        # !mechanicstart, 0 after !mechanicstop.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mechanics (
                user_id TEXT PRIMARY KEY,

                online INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # ----------------------------------------------------
        # CONTACTS
        # ----------------------------------------------------
        #
        # One row per (owner, contact) pair. One-directional —
        # if A adds B, B does not automatically have A, mirroring
        # a real phone contact list.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                owner_id TEXT NOT NULL,

                contact_id TEXT NOT NULL,

                label TEXT,

                added_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (owner_id, contact_id)
            )
            """
        )

        # ----------------------------------------------------
        # LAST TEXT SENDER
        # ----------------------------------------------------
        #
        # Tracks who last texted whom through the phone, so the
        # recipient's phone can offer a "Reply" option even if
        # they don't have the sender saved as a contact — mirrors
        # a real phone showing an unsaved number's texts.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS last_text_sender (
                user_id TEXT PRIMARY KEY,

                sender_id TEXT NOT NULL,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # AREAS (Dubai / Maldives sub-locations)
        # ----------------------------------------------------
        #
        # One row per area code (see config.AREAS). thread_id is
        # NULL until the area's private thread is first created,
        # lazily, the first time anyone enters it. Threads are
        # never deleted — only archived when they become empty —
        # so archived tracks whether the thread currently needs
        # to be unarchived before reuse.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS areas (
                area_code TEXT PRIMARY KEY,

                country TEXT NOT NULL,

                thread_id TEXT,

                archived INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        # ----------------------------------------------------
        # INVENTORY (!mall purchases)
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_id TEXT NOT NULL,

                area_code TEXT NOT NULL,

                item_name TEXT NOT NULL,

                price_paid INTEGER NOT NULL,

                currency TEXT NOT NULL,

                acquired_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # EVENT POOLS / ENTRIES (!compete)
        # ----------------------------------------------------
        #
        # At most one 'open' pool per (area_code, event_code) at
        # a time. status moves open -> resolving (claimed by the
        # scan loop so it isn't double-processed) -> the row is
        # deleted once resolved/refunded/cancelled.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_pools (
                pool_id INTEGER PRIMARY KEY AUTOINCREMENT,

                area_code TEXT NOT NULL,

                event_code TEXT NOT NULL,

                currency TEXT NOT NULL,

                entry_fee INTEGER NOT NULL,

                status TEXT NOT NULL DEFAULT 'open',

                closes_at TIMESTAMP NOT NULL,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_entries (
                pool_id INTEGER NOT NULL,

                user_id TEXT NOT NULL,

                joined_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (pool_id, user_id)
            )
            """
        )

        # ----------------------------------------------------
        # EXCHANGE RATES (wallet)
        # ----------------------------------------------------
        #
        # One row per foreign currency. previous_rate is kept
        # alongside rate so !wallet can show a "since last check"
        # arrow without needing a full history table.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exchange_rates (
                currency TEXT PRIMARY KEY,

                rate REAL NOT NULL,

                previous_rate REAL,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        for _currency, _starting_rate in EXCHANGE_STARTING_RATE.items():

            _conn.execute(
                """
                INSERT OR IGNORE INTO exchange_rates (currency, rate, previous_rate)
                VALUES (?, ?, ?)
                """,
                (_currency, float(_starting_rate), float(_starting_rate))
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
        "current_area",
        "aed_balance",
        "mvr_balance",
        "airtime_balance",
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
# VEHICLE RECORDS (multi-vehicle ownership)
# ============================================================
#
# Each vehicle owned by a player is a dict stored in the
# player's "vehicles" JSON list:
#
#   {
#       "id": "<short uuid>",
#       "name": "Toyota Camry",       # matches VEHICLES / a
#                                      # dispatch/police fleet name
#       "type": "personal",           # personal | taxi |
#                                      # dispatch_bicycle |
#                                      # dispatch_motorcycle | police
#       "location": "dealership",     # location code
#       "condition": 100.0,
#       "fuel": 60.0,
#       "selected": True              # the vehicle currently
#                                      # "in use" for driving
#   }
#
# For backward compatibility, the flat players.vehicle /
# vehicle_location / fuel / vehicle_condition columns are kept
# in sync with whichever vehicle record has "selected": True.
# Every existing cog that reads those flat columns therefore
# keeps working unchanged and always reflects the player's
# currently selected vehicle.
# ============================================================

def get_vehicles(
    user_id: int
) -> list:

    """
    Return the complete owned-vehicle list as a list of dicts.

    Players can own multiple vehicles. Legacy entries stored as
    plain strings (pre-multi-vehicle data) are normalized into
    the dict shape on read, but not rewritten to disk here.
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

        if not isinstance(vehicles, list):
            return []

    except (
        json.JSONDecodeError,
        TypeError
    ):

        return []

    normalized = []

    for entry in vehicles:

        if isinstance(entry, dict):
            normalized.append(entry)
            continue

        if isinstance(entry, str):
            # Legacy string-only vehicle name — best-effort
            # upgrade to the dict shape using the player's
            # current flat vehicle columns if it matches.
            normalized.append({
                "id": uuid.uuid4().hex[:8],
                "name": entry,
                "type": "personal",
                "location": player["vehicle_location"],
                "condition": player["vehicle_condition"],
                "fuel": player["fuel"],
                "selected": entry == player["vehicle"],
            })

    return normalized


def _sync_selected_vehicle_to_flat_columns(
    user_id: int,
    vehicles: list,
) -> None:

    """
    Mirror whichever vehicle record is currently selected onto
    the legacy flat columns (vehicle, vehicle_location, fuel,
    vehicle_condition) so old code paths keep working.
    """

    selected = next(
        (v for v in vehicles if v.get("selected")),
        None,
    )

    if selected is None:

        update_player(
            user_id,
            vehicle=None,
            vehicle_location=None,
            fuel=0,
            vehicle_condition=100,
            vehicles=vehicles,
        )
        return

    update_player(
        user_id,
        vehicle=selected.get("name"),
        vehicle_location=selected.get("location"),
        fuel=selected.get("fuel", 0),
        vehicle_condition=selected.get("condition", 100),
        vehicles=vehicles,
    )


def add_vehicle(
    user_id: int,
    name: str,
    vehicle_type: str = "personal",
    location: str = None,
    condition: float = 100,
    fuel: float = 0,
    select: bool = True,
) -> dict:

    """
    Add a new vehicle record to a player's owned-vehicle list.

    By default the newly purchased/acquired vehicle becomes the
    selected (in-use) vehicle. Pass select=False to add it
    without switching the player's active vehicle (e.g. a taxi
    company retrieving/assigning a commercial vehicle on a
    player's behalf).
    """

    vehicles = get_vehicles(user_id)

    record = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "type": vehicle_type,
        "location": location,
        "condition": condition,
        "fuel": fuel,
        "selected": False,
    }

    if select:
        for v in vehicles:
            v["selected"] = False
        record["selected"] = True

    vehicles.append(record)

    _sync_selected_vehicle_to_flat_columns(user_id, vehicles)

    return record


def select_vehicle(
    user_id: int,
    identifier: str,
) -> dict | None:

    """
    Select one of the player's owned vehicles as the active one
    (used by !usevehicle). Matches by vehicle id first, then by
    case-insensitive name (first match).

    Returns the newly selected vehicle dict, or None if no
    matching vehicle is owned.
    """

    vehicles = get_vehicles(user_id)

    target = next(
        (v for v in vehicles if v.get("id") == identifier),
        None,
    )

    if target is None:
        target = next(
            (
                v for v in vehicles
                if str(v.get("name", "")).lower()
                == identifier.strip().lower()
            ),
            None,
        )

    if target is None:
        return None

    for v in vehicles:
        v["selected"] = (v is target)

    _sync_selected_vehicle_to_flat_columns(user_id, vehicles)

    return target


def get_selected_vehicle(
    user_id: int,
) -> dict | None:

    """
    Return the player's currently selected/active vehicle
    record, or None if they have no vehicles.
    """

    vehicles = get_vehicles(user_id)

    return next(
        (v for v in vehicles if v.get("selected")),
        None,
    )


def update_vehicle(
    user_id: int,
    vehicle_id: str,
    **fields,
) -> dict | None:

    """
    Update fields (location, condition, fuel, type) on a single
    owned vehicle record by id. If that vehicle is currently
    selected, the legacy flat columns are re-synced too.
    """

    vehicles = get_vehicles(user_id)

    target = next(
        (v for v in vehicles if v.get("id") == vehicle_id),
        None,
    )

    if target is None:
        return None

    target.update(fields)

    _sync_selected_vehicle_to_flat_columns(user_id, vehicles)

    return target


def remove_vehicle(
    user_id: int,
    vehicle_id: str,
) -> bool:

    """
    Remove a vehicle from a player's owned list (e.g. a
    commercial taxi/dispatch/police vehicle being retrieved by
    the issuing company/department). If the removed vehicle was
    selected, no vehicle is auto-selected afterward — the player
    must !usevehicle another one of their remaining vehicles.

    Returns True if a vehicle was removed.
    """

    vehicles = get_vehicles(user_id)

    remaining = [
        v for v in vehicles if v.get("id") != vehicle_id
    ]

    if len(remaining) == len(vehicles):
        return False

    _sync_selected_vehicle_to_flat_columns(user_id, remaining)

    return True


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


def has_claimed_brt_starter_bonus(
    user_id: int
) -> bool:

    """
    Check whether a player has already received the one-time
    ₦5,000 first-card starter bonus.

    A player without a card at all is treated as not having
    claimed it yet.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT starter_bonus_given
            FROM brt_cards
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        row = cur.fetchone()

        if row is None:
            return False

        return bool(row["starter_bonus_given"])


def claim_brt_starter_bonus(
    user_id: int,
    amount: int = 5000
) -> bool:

    """
    Grant the one-time first-card ₦5,000 BRT balance bonus.

    Eligibility (e.g. the player holding the Lagosians role) is
    the caller's responsibility to check before calling this —
    this function only enforces that the bonus is claimed at
    most once per player, and that the player already owns a
    BRT card.

    Returns:
        True  = bonus granted
        False = player has no card, or already claimed it
    """

    with _lock:

        cur = _conn.execute(
            """
            UPDATE brt_cards
            SET balance = balance + ?,
                starter_bonus_given = 1
            WHERE user_id = ?
              AND starter_bonus_given = 0
            """,
            (
                int(amount),
                str(user_id)
            )
        )

        _conn.commit()

        return cur.rowcount > 0


# ============================================================
# BANK ACCOUNT FUNCTIONS
# ============================================================
#
# The Bank App requires a bank account before it can be used.
# The bank balance is NOT a separate ledger — it is the same
# Naira pool as players.balance, so deposits/withdrawals/
# transfers below read and write players.balance directly.
# ============================================================

def has_bank_account(
    user_id: int
) -> bool:

    """
    Check whether a player has an open bank account.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT user_id
            FROM bank_accounts
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        return cur.fetchone() is not None


def create_bank_account(
    user_id: int
) -> bool:

    """
    Open a bank account for a player.

    Returns:
        True  = account created
        False = player already has an account
    """

    with _lock:

        cur = _conn.execute(
            """
            INSERT OR IGNORE INTO bank_accounts (
                user_id
            )
            VALUES (
                ?
            )
            """,
            (str(user_id),)
        )

        _conn.commit()

        return cur.rowcount > 0


def bank_transfer(
    sender_id: int,
    recipient_id: int,
    amount: int
) -> tuple[bool, str]:

    """
    Transfer Naira from one player's bank balance to another's.

    Both players must already have a bank account. The transfer
    is atomic — the sender's balance is only ever debited if
    they have sufficient funds.

    Returns:
        (True, "ok")            on success
        (False, "<reason code>") on failure, one of:
            "no_sender_account"
            "no_recipient_account"
            "invalid_amount"
            "insufficient_funds"
    """

    amount = int(amount)

    if amount <= 0:
        return (False, "invalid_amount")

    with _lock:

        if not has_bank_account(sender_id):
            return (False, "no_sender_account")

        if not has_bank_account(recipient_id):
            return (False, "no_recipient_account")

        cur = _conn.execute(
            """
            UPDATE players
            SET balance = balance - ?
            WHERE user_id = ?
              AND balance >= ?
            """,
            (
                amount,
                str(sender_id),
                amount
            )
        )

        if cur.rowcount == 0:
            _conn.commit()
            return (False, "insufficient_funds")

        _conn.execute(
            """
            UPDATE players
            SET balance = balance + ?
            WHERE user_id = ?
            """,
            (
                amount,
                str(recipient_id)
            )
        )

        _conn.commit()

        return (True, "ok")


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
        This also removes BRT cards, bank accounts, purchased
        buses, taxi driver registrations, dispatch rider
        registrations, and mechanic online status, because the
        entire database is reset. Vehicles themselves live in the
        `vehicles` JSON column on `players`, so deleting the
        player rows already clears those — no separate vehicles
        table to wipe.
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
        # Delete bank accounts
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM bank_accounts
            """
        )

        # ----------------------------------------------------
        # Delete taxi driver registrations
        #
        # Otherwise a wiped player who re-registers via
        # !registerplayers would still show up as an existing
        # (and possibly still "online") taxi driver from before
        # the reset, with no player row to back it.
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM taxi_drivers
            """
        )

        # ----------------------------------------------------
        # Delete dispatch rider registrations
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM dispatch_riders
            """
        )

        # ----------------------------------------------------
        # Delete mechanic online/offline status
        #
        # The Mechanic ROLE itself is staff-assigned (not
        # granted/removed by the bot, so resetdatabase leaves it
        # alone) — but this table's online flag is bot-managed
        # state tied to a user_id, and would otherwise leave a
        # mechanic stuck "online" from before the reset.
        # ----------------------------------------------------

        _conn.execute(
            """
            DELETE FROM mechanics
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


def get_dispatch_rider(
    user_id: int
) -> sqlite3.Row | None:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM dispatch_riders
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        return cur.fetchone()


def register_dispatch_rider(
    user_id: int,
    tier: str
) -> None:

    """
    Register a new dispatch rider, or overwrite an existing
    registration with a new tier. Always starts offline —
    the player must !dispatchstart to become bookable.
    """

    with _lock:

        _conn.execute(
            """
            INSERT INTO dispatch_riders (user_id, tier, online)
            VALUES (?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                tier = excluded.tier,
                online = 0
            """,
            (str(user_id), tier)
        )

        _conn.commit()


def set_dispatch_online(
    user_id: int,
    online: bool
) -> None:

    with _lock:

        _conn.execute(
            """
            UPDATE dispatch_riders
            SET online = ?
            WHERE user_id = ?
            """,
            (1 if online else 0, str(user_id))
        )

        _conn.commit()


def get_online_dispatch_riders(
    tier: str
) -> list[sqlite3.Row]:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM dispatch_riders
            WHERE tier = ? AND online = 1
            """,
            (tier,)
        )

        return cur.fetchall()


def remove_dispatch_rider(
    user_id: int
) -> None:

    """
    Used by resetdatabase-style cleanup and by the taxi company's
    vehicle-retrieval command — deletes the rider's registration
    row entirely (unlike set_dispatch_online, which just takes
    them offline while keeping the registration).
    """

    with _lock:

        _conn.execute(
            """
            DELETE FROM dispatch_riders
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        _conn.commit()


# ============================================================
# MECHANICS
# ============================================================

def set_mechanic_online(
    user_id: int,
    online: bool
) -> None:
    """
    Marks a player online/offline as a mechanic. Creates their
    row on first use — there's no separate "become a mechanic"
    registration step, since the Mechanic role itself (granted
    by an admin) is what makes someone eligible.
    """

    with _lock:

        _conn.execute(
            """
            INSERT INTO mechanics (user_id, online)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                online = excluded.online
            """,
            (str(user_id), 1 if online else 0)
        )

        _conn.commit()


def get_online_mechanics() -> list[sqlite3.Row]:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM mechanics
            WHERE online = 1
            """
        )

        return cur.fetchall()


# ============================================================
# CONTACTS
# ============================================================

def add_contact(
    owner_id: int,
    contact_id: int,
    label: str | None = None
) -> None:

    with _lock:

        _conn.execute(
            """
            INSERT INTO contacts (owner_id, contact_id, label)
            VALUES (?, ?, ?)
            ON CONFLICT(owner_id, contact_id) DO UPDATE SET
                label = excluded.label
            """,
            (str(owner_id), str(contact_id), label)
        )

        _conn.commit()


def remove_contact(
    owner_id: int,
    contact_id: int
) -> None:

    with _lock:

        _conn.execute(
            """
            DELETE FROM contacts
            WHERE owner_id = ? AND contact_id = ?
            """,
            (str(owner_id), str(contact_id))
        )

        _conn.commit()


def get_contacts(
    owner_id: int
) -> list[sqlite3.Row]:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM contacts
            WHERE owner_id = ?
            ORDER BY added_at ASC
            """,
            (str(owner_id),)
        )

        return cur.fetchall()


def is_contact(
    owner_id: int,
    contact_id: int
) -> bool:

    with _lock:

        cur = _conn.execute(
            """
            SELECT 1
            FROM contacts
            WHERE owner_id = ? AND contact_id = ?
            """,
            (str(owner_id), str(contact_id))
        )

        return cur.fetchone() is not None


# ============================================================
# LAST TEXT SENDER (lets a recipient reply even if the sender
# isn't saved in their contacts yet)
# ============================================================

def set_last_text_sender(
    user_id: int,
    sender_id: int
) -> None:

    with _lock:

        _conn.execute(
            """
            INSERT INTO last_text_sender (user_id, sender_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                sender_id = excluded.sender_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(user_id), str(sender_id))
        )

        _conn.commit()


def get_last_text_sender(
    user_id: int
) -> int | None:

    with _lock:

        cur = _conn.execute(
            """
            SELECT sender_id
            FROM last_text_sender
            WHERE user_id = ?
            """,
            (str(user_id),)
        )

        row = cur.fetchone()

        return int(row["sender_id"]) if row else None


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
    departure_at: str,
    status: str = "booked"
) -> None:

    """
    Create a new flight row for a player.

    Assumes the player has no existing flight row —
    callers should check get_flight() first.

    status defaults to "booked" for backward compatibility, but
    cogs/flight.py's booking flow now passes "pending_approval" —
    no charge has happened yet at that point, and departure_at is
    just a placeholder timestamp, ignored until an Immigration
    Officer approves the request (which is what actually sets a
    real departure_at and flips status to "booked").
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
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(user_id),
                destination,
                status,
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
        "return_reminded",
        "officer_notified",
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
# HOTELS
# ============================================================

def get_hotel_room(booker_id: int) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM hotels WHERE booker_id = ?",
            (str(booker_id),)
        )
        return cur.fetchone()


def get_hotel_room_as_guest(guest_id: int) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM hotels WHERE guest_id = ?",
            (str(guest_id),)
        )
        return cur.fetchone()


def get_hotel_room_by_thread(thread_id: int) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM hotels WHERE thread_id = ?",
            (str(thread_id),)
        )
        return cur.fetchone()


def rooms_in_use(destination: str, tier: str) -> list[sqlite3.Row]:
    """All active rooms of one tier at one destination — used to find a free room_number."""
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM hotels WHERE destination = ? AND tier = ?",
            (destination, tier)
        )
        return cur.fetchall()


def book_hotel_room(
    booker_id: int,
    destination: str,
    tier: str,
    room_number: int,
    thread_id: int,
    price_paid: int,
    checked_in_at: str,
    stay_seconds: int,
) -> None:
    """Create a new hotel room row. Assumes booker_id has no existing room — check get_hotel_room() first."""
    with _lock:
        _conn.execute(
            """
            INSERT INTO hotels (
                booker_id, destination, tier, room_number,
                thread_id, guest_id, price_paid,
                checked_in_at, stay_seconds, service_index
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 0)
            """,
            (
                str(booker_id), destination, tier, int(room_number),
                str(thread_id), int(price_paid),
                checked_in_at, int(stay_seconds),
            )
        )
        _conn.commit()


def update_hotel_room(booker_id: int, **fields) -> None:
    if not fields:
        return

    allowed_columns = {
        "guest_id", "service_index", "thread_id",
    }

    invalid_columns = set(fields) - allowed_columns
    if invalid_columns:
        raise ValueError(f"Invalid hotel field(s): {', '.join(sorted(invalid_columns))}")

    set_clause = ", ".join(f"{column} = ?" for column in fields)
    values = list(fields.values())
    values.append(str(booker_id))

    with _lock:
        _conn.execute(
            f"UPDATE hotels SET {set_clause} WHERE booker_id = ?",
            values
        )
        _conn.commit()


def delete_hotel_room(booker_id: int) -> None:
    with _lock:
        _conn.execute("DELETE FROM hotels WHERE booker_id = ?", (str(booker_id),))
        _conn.commit()


def all_hotel_rooms() -> list[sqlite3.Row]:
    with _lock:
        cur = _conn.execute("SELECT * FROM hotels")
        return cur.fetchall()


# ============================================================
# AREAS (Dubai / Maldives sub-locations)
# ============================================================

def get_area(area_code: str) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM areas WHERE area_code = ?",
            (area_code,)
        )
        return cur.fetchone()


def get_area_by_thread(thread_id: int) -> sqlite3.Row | None:
    """Look up which area a given Discord thread belongs to (only
    matches while that thread is the area's CURRENT thread_id —
    i.e. not archived-and-abandoned)."""
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM areas WHERE thread_id = ? AND archived = 0",
            (str(thread_id),)
        )
        return cur.fetchone()


def upsert_area_thread(area_code: str, country: str, thread_id: int, archived: bool) -> None:
    """Create or update an area's row with its current thread_id/
    archived state."""
    with _lock:
        _conn.execute(
            """
            INSERT INTO areas (area_code, country, thread_id, archived)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(area_code) DO UPDATE SET
                country = excluded.country,
                thread_id = excluded.thread_id,
                archived = excluded.archived
            """,
            (area_code, country, str(thread_id), 1 if archived else 0)
        )
        _conn.commit()


def set_area_archived(area_code: str, archived: bool) -> None:
    with _lock:
        _conn.execute(
            "UPDATE areas SET archived = ? WHERE area_code = ?",
            (1 if archived else 0, area_code)
        )
        _conn.commit()


# ============================================================
# INVENTORY (!mall purchases)
# ============================================================

def add_inventory_item(
    user_id: int,
    area_code: str,
    item_name: str,
    price_paid: int,
    currency: str,
) -> None:
    with _lock:
        _conn.execute(
            """
            INSERT INTO inventory (user_id, area_code, item_name, price_paid, currency)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(user_id), area_code, item_name, int(price_paid), currency)
        )
        _conn.commit()


def get_inventory(user_id: int) -> list[sqlite3.Row]:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM inventory WHERE user_id = ? ORDER BY acquired_at DESC",
            (str(user_id),)
        )
        return cur.fetchall()


# ============================================================
# EVENT POOLS / ENTRIES (!compete)
# ============================================================

def get_open_pool(area_code: str, event_code: str) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute(
            """
            SELECT * FROM event_pools
            WHERE area_code = ? AND event_code = ? AND status = 'open'
            """,
            (area_code, event_code)
        )
        return cur.fetchone()


def create_event_pool(
    area_code: str,
    event_code: str,
    currency: str,
    entry_fee: int,
    closes_at: str,
) -> int:
    """Create a new open pool and return its pool_id."""
    with _lock:
        cur = _conn.execute(
            """
            INSERT INTO event_pools (area_code, event_code, currency, entry_fee, status, closes_at)
            VALUES (?, ?, ?, ?, 'open', ?)
            """,
            (area_code, event_code, currency, int(entry_fee), closes_at)
        )
        _conn.commit()
        return cur.lastrowid


def get_pool(pool_id: int) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM event_pools WHERE pool_id = ?",
            (pool_id,)
        )
        return cur.fetchone()


def pools_due(now_iso: str) -> list[sqlite3.Row]:
    """Open pools whose registration window has already closed."""
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM event_pools WHERE status = 'open' AND closes_at <= ?",
            (now_iso,)
        )
        return cur.fetchall()


def set_pool_status(pool_id: int, status: str) -> None:
    with _lock:
        _conn.execute(
            "UPDATE event_pools SET status = ? WHERE pool_id = ?",
            (status, pool_id)
        )
        _conn.commit()


def delete_pool(pool_id: int) -> None:
    with _lock:
        _conn.execute("DELETE FROM event_pools WHERE pool_id = ?", (pool_id,))
        _conn.execute("DELETE FROM event_entries WHERE pool_id = ?", (pool_id,))
        _conn.commit()


def add_event_entry(pool_id: int, user_id: int) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO event_entries (pool_id, user_id) VALUES (?, ?)",
            (pool_id, str(user_id))
        )
        _conn.commit()


def get_event_entry(pool_id: int, user_id: int) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM event_entries WHERE pool_id = ? AND user_id = ?",
            (pool_id, str(user_id))
        )
        return cur.fetchone()


def get_pool_entries(pool_id: int) -> list[sqlite3.Row]:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM event_entries WHERE pool_id = ?",
            (pool_id,)
        )
        return cur.fetchall()


# ============================================================
# EXCHANGE RATES (wallet)
# ============================================================

def get_exchange_rate(currency: str) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM exchange_rates WHERE currency = ?",
            (currency,)
        )
        return cur.fetchone()


def all_exchange_rates() -> list[sqlite3.Row]:
    with _lock:
        cur = _conn.execute("SELECT * FROM exchange_rates")
        return cur.fetchall()


def set_exchange_rate(currency: str, rate: float) -> None:
    """
    Internal use ONLY — called exclusively by the background
    drift loop in cogs/wallet.py. No player-facing command may
    call this; there is deliberately no way for anyone, including
    admins, to set a rate directly.
    """
    with _lock:
        current = _conn.execute(
            "SELECT rate FROM exchange_rates WHERE currency = ?",
            (currency,)
        ).fetchone()

        previous = current["rate"] if current else rate

        _conn.execute(
            """
            UPDATE exchange_rates
            SET previous_rate = ?, rate = ?, updated_at = CURRENT_TIMESTAMP
            WHERE currency = ?
            """,
            (previous, rate, currency)
        )
        _conn.commit()


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
