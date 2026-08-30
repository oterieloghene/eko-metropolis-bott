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

Locations data includes:
    - Dynamically registered locations (`locations` table) —
      created via !location-registration on top of the
      hand-authored LOCATIONS dict in config.py.
    - Sub-locations attached to a parent location
      (`sub_locations` table) — "rooms" like a bank's front-desk
      or ATM, each either public (anyone who arrives) or
      role-gated. Created via !create-sub-location.
    - Both tables are intentionally excluded from
      reset_database() — they persist across a full reset.

Current accounts data includes:
    - Government/office accounts (`current_accounts` table) —
      named ledger destinations created via
      !create-current-account, each linked to a Discord channel
      for receipts. Hold no balance of their own; transfers into
      them settle straight into institution_accounts["central_bank"]
      (see current_account_transfer()). Also excluded from
      reset_database() — persists across a full reset.
"""

import json
import os
import sqlite3
import threading
import uuid

from config import (
    STARTING_BALANCE,
    STARTING_LOCATION,
    EXCHANGE_STARTING_RATE,
    PLAYER_STAT_NAMES,
    STAT_STARTING_VALUE,
    STAT_MIN,
    STAT_MAX,
    LOCATIONS,
)


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

DB_PATH = os.environ.get(
    "DB_PATH",
    "ekobot.db"
)

_lock = threading.RLock()

_conn = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

_conn.row_factory = sqlite3.Row

# litestream replicates by tailing the WAL file, and requires the
# database to actually be in WAL journal mode to do that — SQLite's
# own default ("delete", a plain rollback journal) gives litestream
# nothing to tail, so backups either fail outright or can't keep up
# in near-real-time. Must be set before any other statements run.
_conn.execute("PRAGMA journal_mode=WAL")


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

                airtime_balance INTEGER NOT NULL DEFAULT 0,

                unconscious INTEGER NOT NULL DEFAULT 0,

                cash_balance INTEGER NOT NULL DEFAULT 0
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

        # Player stats (hunger/thirst/health/hygiene/breath/
        # happiness) — see config.PLAYER_STAT_NAMES. Older
        # databases won't have these columns, so add them the
        # same guarded way as every other upgrade above. Existing
        # players get filled to STAT_STARTING_VALUE below, not
        # left at 0, so nobody who already exists suddenly finds
        # themselves collapsed on the next patch.
        for _stat_name in PLAYER_STAT_NAMES:

            if _stat_name not in _existing_player_columns:

                _conn.execute(
                    f"""
                    ALTER TABLE players
                    ADD COLUMN {_stat_name} REAL NOT NULL DEFAULT {STAT_STARTING_VALUE}
                    """
                )

        # Unconscious/collapsed flag (cogs/walk.py). Same
        # missing-column-on-upgrade problem as everything else in
        # this block — older databases predate the collapse
        # feature entirely, so this needs the same guarded
        # ALTER TABLE treatment. Defaults to 0 (conscious) so no
        # existing player is retroactively marked unconscious.
        if "unconscious" not in _existing_player_columns:

            _conn.execute(
                "ALTER TABLE players ADD COLUMN unconscious INTEGER NOT NULL DEFAULT 0"
            )

        # Cash balance (Phase 2 banking overhaul) — separate from
        # `balance`, which is now specifically the player's BANK
        # balance. Older databases predate this split, so guard
        # it the same way as every other column above. Defaults
        # to 0 for existing players; nobody's money moves, it
        # just now lives entirely in the bank half of the split
        # until they !with(draw) some of it to cash.
        if "cash_balance" not in _existing_player_columns:

            _conn.execute(
                "ALTER TABLE players ADD COLUMN cash_balance INTEGER NOT NULL DEFAULT 0"
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
        # INVENTORY (personal item stacks — !give / !inv, filled
        # by cogs/business_shop.py's !sell)
        # ----------------------------------------------------
        #
        # One row per (user_id, item_name) — items stack via `qty`
        # instead of one row per unit, so e.g. "Sachet Water x36"
        # is a single row, matching how !inv/!give are meant to
        # display things. `category` is one of
        # cogs/business_admin.SHOP_CATEGORIES, so !inv/!give can
        # group a player's items the same way a business's own
        # !menu/!buy does.
        #
        # This replaces the old one-row-per-unit schema used by
        # the now-removed !mall-purchase/starter-item flows — an
        # existing old-shape table is migrated into the new
        # stacked shape below (qty = count of matching old rows;
        # category defaults to "food_drinks", since the old schema
        # never tracked one).
        # ----------------------------------------------------

        _inventory_exists = _conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='inventory'"
        ).fetchone() is not None

        _existing_inventory_columns = (
            {row["name"] for row in _conn.execute("PRAGMA table_info(inventory)")}
            if _inventory_exists else set()
        )

        if _inventory_exists and "qty" not in _existing_inventory_columns:

            _conn.execute("ALTER TABLE inventory RENAME TO inventory_old")

            _conn.execute(
                """
                CREATE TABLE inventory (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,

                    user_id TEXT NOT NULL,

                    category TEXT NOT NULL DEFAULT 'food_drinks',

                    item_name TEXT NOT NULL COLLATE NOCASE,

                    qty INTEGER NOT NULL DEFAULT 0,

                    UNIQUE(user_id, item_name)
                )
                """
            )

            _conn.execute(
                """
                INSERT INTO inventory (user_id, item_name, qty)
                SELECT user_id, item_name, COUNT(*)
                FROM inventory_old
                GROUP BY user_id, item_name
                """
            )

            _conn.execute("DROP TABLE inventory_old")

        else:

            _conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,

                    user_id TEXT NOT NULL,

                    category TEXT NOT NULL DEFAULT 'food_drinks',

                    item_name TEXT NOT NULL COLLATE NOCASE,

                    qty INTEGER NOT NULL DEFAULT 0,

                    UNIQUE(user_id, item_name)
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

        # ----------------------------------------------------
        # LOCATIONS (dynamic, admin-registered)
        # ----------------------------------------------------
        #
        # Locations created at runtime via !location-registration,
        # on top of the hand-authored LOCATIONS dict in config.py.
        # This is what lets new drivable places (starting with
        # registered businesses) get added without ever touching
        # code or redeploying.
        #
        # code            -> unique slug, same role config.LOCATIONS
        #                    keys play (e.g. "emirates-mall").
        # zone            -> which zone hub this location hangs
        #                    off of ("island", "mainland", "ghetto"),
        #                    matching the RAW_DISTANCES zone-hub
        #                    model in config.py/routing.py.
        # distance         -> distance FROM that zone's hub, same
        #                    unit RAW_DISTANCES already uses, so it
        #                    can be merged straight into the road
        #                    graph routing.py builds.
        # category        -> free-form tag ("business", "government",
        #                    "other") — not enforced, just useful
        #                    for !view-balances-style reporting later.
        #
        # IMPORTANT: this table is intentionally NEVER touched by
        # reset_database() or reset_all_locations() — registered
        # locations must survive a database reset.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS locations (
                code TEXT PRIMARY KEY,

                name TEXT NOT NULL,

                channel_name TEXT NOT NULL,

                zone TEXT NOT NULL,

                distance REAL NOT NULL DEFAULT 1,

                category TEXT NOT NULL DEFAULT 'other',

                created_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # SUB-LOCATIONS
        # ----------------------------------------------------
        #
        # A "room" attached to a parent location — either an
        # existing config.py LOCATIONS code (e.g. "bank") or a
        # dynamically registered one from the table above.
        #
        # access        -> "public"  = opens for anyone who
        #                    arrives at the parent location
        #                    (e.g. an ATM lobby).
        #                 "role"     = only members holding
        #                    role_name get access on arrival
        #                    (e.g. bank-manager, cbe-chairman).
        #
        # role_name is unused (NULL) when access = "public". When
        # access = "role", it may hold a comma-separated list of
        # role names (e.g. "cbe-chairman,cbe-deputy") — a member
        # qualifies if they hold ANY one of the listed roles. See
        # permissions.py's _set_sub_location_access for the check.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sub_locations (
                code TEXT PRIMARY KEY,

                parent_code TEXT NOT NULL,

                name TEXT NOT NULL,

                channel_name TEXT NOT NULL,

                access TEXT NOT NULL DEFAULT 'public',

                role_name TEXT,

                created_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # INSTITUTION ACCOUNTS (Phase 2 banking overhaul)
        # ----------------------------------------------------
        #
        # Fixed, singleton ledger accounts that aren't tied to any
        # one player:
        #
        #   central_bank -> Central Bank of Eko. Permanent sink
        #                    for government/office deposits (see
        #                    Phase 3's !create-current-account) —
        #                    money that lands here never
        #                    disappears, it just sits in the pool.
        #                    Also the source !cb-with draws from.
        #   treasury      -> Treasury. Government's own spending
        #                    pool, separate from the Central Bank.
        #
        # starting_balance is kept alongside balance so a full
        # !resetdatabase can restore the opening amount without
        # hardcoding it a second time anywhere else.
        #
        # IMPORTANT: unlike `locations`/`sub_locations` above,
        # this table's ROWS are never deleted by reset_database()
        # — but the BALANCES are reset back to starting_balance
        # (see reset_institution_accounts()), per spec ("resets
        # with the database").
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS institution_accounts (
                code TEXT PRIMARY KEY,

                name TEXT NOT NULL,

                balance INTEGER NOT NULL,

                starting_balance INTEGER NOT NULL
            )
            """
        )

        for _code, _name, _starting in (
            ("central_bank", "Central Bank of Eko", 300_000_000),
            ("treasury", "Treasury", 500_000_000),
        ):

            _conn.execute(
                """
                INSERT OR IGNORE INTO institution_accounts
                    (code, name, balance, starting_balance)
                VALUES (?, ?, ?, ?)
                """,
                (_code, _name, _starting, _starting)
            )

        # ----------------------------------------------------
        # CURRENT ACCOUNTS (Phase 3 — government/office accounts)
        # ----------------------------------------------------
        #
        # A "current account" is a named government/office ledger
        # destination (e.g. "Eko Memorial Hospital"), created via
        # !create-current-account and pointed at an existing
        # Discord channel where its receipts get posted.
        #
        # Unlike institution_accounts or business accounts (Phase
        # 4), a current account holds NO balance of its own —
        # every transfer into one settles immediately and
        # permanently into institution_accounts["central_bank"]
        # (see current_account_transfer() below). That's why there
        # is no `balance` column here and no per-org
        # !<org> balance command (per spec — !view-balances /
        # the Central Bank balance already covers it).
        #
        # code         -> unique slug used as the !transfer
        #                 recipient (e.g. "hospital").
        # name         -> display name (e.g. "Eko Memorial
        #                 Hospital").
        # channel_name -> the Discord channel receipts get posted
        #                 to. Does not have to be a registered
        #                 `locations`/`sub_locations` channel —
        #                 any existing text channel works.
        #
        # IMPORTANT: like `locations`/`sub_locations`, this table
        # is intentionally NEVER touched by reset_database() —
        # registered current accounts must survive a database
        # reset (there is no DELETE for this table anywhere).
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS current_accounts (
                code TEXT PRIMARY KEY,

                name TEXT NOT NULL,

                channel_name TEXT NOT NULL,

                created_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # BUSINESSES (Phase 4 — registration half)
        # ----------------------------------------------------
        #
        # Created via !business-registration (cogs/business_admin.py),
        # gated to the Minister of Justice / admin. This is the
        # bookkeeping row alongside the matching `locations` row
        # (same code) created in that same command — who owns the
        # business, what type it is (fixes its licensed inventory
        # categories in Phase 5), and which shared "owner-type"
        # Discord role was granted.
        #
        # code            -> same slug as its `locations` row.
        # owner_id        -> the Discord member registered as owner.
        # business_type   -> "mall" / "mamaput" / "club" / "gasstation".
        # owner_role_name -> the shared role granted for that type
        #                    (e.g. "mallowner") — NOT unique per
        #                    business, see business_admin.py.
        #
        # IMPORTANT: like `locations`/`sub_locations`/`current_accounts`,
        # this table is intentionally NEVER touched by
        # reset_database() — a registered business is a standing
        # government registration, not round-based player state.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS businesses (
                code TEXT PRIMARY KEY,

                name TEXT NOT NULL,

                owner_id TEXT NOT NULL,

                business_type TEXT NOT NULL,

                owner_role_name TEXT NOT NULL,

                created_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # BUSINESS ACCOUNTS (Phase 4 — financial half)
        # ----------------------------------------------------
        #
        # Deliberately separate from the `businesses` row above —
        # per spec, registration (the government paperwork) and
        # opening the actual money-holding account
        # (!create-business-account) are two distinct steps. A
        # business can exist (and its channel/location be live)
        # with no account open yet; database.has_business_account()
        # is what every balance-touching command checks.
        #
        # Unlike `current_accounts`, this table DOES hold a real
        # balance directly — business revenue is private, and per
        # spec is "never swept into Central Bank of Eko."
        #
        # receipt_channel_name -> where itemized/incoming-payment
        #                         receipts post. Set at account
        #                         creation and NOT required to be
        #                         the same channel as the business's
        #                         own registered location channel.
        #
        # IMPORTANT: never touched by reset_database(), same
        # reasoning as `businesses` above — private business
        # balances are standing player-owned assets, not round
        # state that resets.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_accounts (
                code TEXT PRIMARY KEY
                    REFERENCES businesses (code),

                balance INTEGER NOT NULL DEFAULT 0,

                receipt_channel_name TEXT NOT NULL,

                created_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ----------------------------------------------------
        # BUSINESS ITEMS (Phase 5 — shop catalog / stock)
        # ----------------------------------------------------
        #
        # One row per catalog line for a business, created/topped
        # up via !add (cogs/business_shop.py) and restocked via
        # !order. `category` is one of SHOP_CATEGORIES
        # (cogs/business_admin.py) and must be one the owning
        # business's business_type is licensed to sell — enforced
        # by the calling cog, not here.
        #
        # item_name is COLLATE NOCASE so "!add ... Suya" and a
        # later "!add ... suya" restock/reprice the same line
        # instead of creating a duplicate — same forgiving lookup
        # !sell/!buy/!order use to find an existing item by name.
        #
        # IMPORTANT: like `businesses`/`business_accounts`, never
        # touched by reset_database() — a shop's stock is standing
        # business state, not round data.
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,

                business_code TEXT NOT NULL
                    REFERENCES businesses (code),

                category TEXT NOT NULL,

                item_name TEXT NOT NULL
                    COLLATE NOCASE,

                price INTEGER NOT NULL,

                stock INTEGER NOT NULL DEFAULT 0,

                created_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                UNIQUE (business_code, item_name)
            )
            """
        )

        # ----------------------------------------------------
        # CASH REGISTERS (Phase 5 — !buy tabs)
        # ----------------------------------------------------
        #
        # One row per open (or since-settled) tab between a
        # customer and a business, per spec's "one per (owner,
        # customer)" rule. !buy creates/adds to the OPEN register
        # for (business_code, customer_id) — there is never more
        # than one open register for the same pair at once (see
        # add_to_register()'s lookup-or-create below). Paying the
        # exact `total` (via !pay or !transfer — cogs/banking.py)
        # flips status to "paid"; !sell then fulfills it (status
        # "fulfilled") or the owner cancels an unpaid one via
        # !close-register (status "cancelled").
        #
        # items -> JSON list of {"item_name", "price", "qty"}
        #          lines, same shape !sell/!buy already expect.
        # status -> "open" | "paid" | "cancelled" | "fulfilled".
        #
        # IMPORTANT: like `business_items` above, never touched by
        # reset_database() — a tab reflects standing business
        # state, not round data. (Stale open tabs are cleared
        # manually via !close-register, not by a reset.)
        # ----------------------------------------------------

        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cash_registers (
                register_id INTEGER PRIMARY KEY AUTOINCREMENT,

                business_code TEXT NOT NULL
                    REFERENCES businesses (code),

                customer_id TEXT NOT NULL,

                items TEXT NOT NULL DEFAULT '[]',

                total INTEGER NOT NULL DEFAULT 0,

                status TEXT NOT NULL DEFAULT 'open',

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
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
        Stats (hunger/thirst/health/hygiene/breath/happiness)
            = STAT_STARTING_VALUE each
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
                traveling,
                hunger,
                thirst,
                health,
                hygiene,
                breath,
                happiness
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
                0,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
            """,
            (
                str(user_id),
                STARTING_BALANCE,
                STARTING_LOCATION,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
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
        "unconscious",
        "cash_balance",
        *PLAYER_STAT_NAMES,
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
# PLAYER STATS (hunger/thirst/health/hygiene/breath/happiness)
# ============================================================
#
# All reads/writes are clamped to [STAT_MIN, STAT_MAX] here, so
# no caller anywhere else in the codebase needs to remember to
# clamp — a walk-distance drain, a background decay tick, or an
# admin !setstat can all just pass a raw delta.
# ============================================================

def get_stats(
    user_id: int
) -> dict:

    """
    Return a player's current stats as a plain dict, e.g.:

        {
            "hunger": 87.0,
            "thirst": 62.5,
            "health": 100.0,
            "hygiene": 74.0,
            "breath": 91.0,
            "happiness": 55.0,
        }

    Creates the player (with default stats) if they don't exist
    yet, same as get_or_create_player.
    """

    player = get_or_create_player(user_id)

    return {
        stat_name: float(player[stat_name])
        for stat_name in PLAYER_STAT_NAMES
    }


def set_stats(
    user_id: int,
    **stat_values
) -> dict:

    """
    Set one or more stats to an absolute value (clamped to
    [STAT_MIN, STAT_MAX]).

    Example:

        set_stats(user_id, hunger=100, thirst=100)
    """

    invalid = set(stat_values) - set(PLAYER_STAT_NAMES)

    if invalid:
        raise ValueError(
            f"Invalid stat name(s): {', '.join(sorted(invalid))}"
        )

    clamped = {
        stat_name: max(STAT_MIN, min(STAT_MAX, float(value)))
        for stat_name, value in stat_values.items()
    }

    if clamped:
        update_player(user_id, **clamped)

    return get_stats(user_id)


def adjust_stats(
    user_id: int,
    **stat_deltas
) -> dict:

    """
    Apply relative deltas to one or more stats (positive to
    restore, negative to drain), clamped to [STAT_MIN, STAT_MAX].

    Example:

        # walking drained these
        adjust_stats(user_id, thirst=-4.2, hunger=-1.9)

        # eating restored this
        adjust_stats(user_id, hunger=+30)
    """

    invalid = set(stat_deltas) - set(PLAYER_STAT_NAMES)

    if invalid:
        raise ValueError(
            f"Invalid stat name(s): {', '.join(sorted(invalid))}"
        )

    if not stat_deltas:
        return get_stats(user_id)

    current = get_stats(user_id)

    new_values = {
        stat_name: max(
            STAT_MIN,
            min(
                STAT_MAX,
                current[stat_name] + float(delta)
            )
        )
        for stat_name, delta in stat_deltas.items()
    }

    update_player(user_id, **new_values)

    return get_stats(user_id)


def all_player_ids() -> list[str]:

    """
    Return every registered player's user_id (as strings). Used
    by background stat-decay loops to tick every player without
    pulling full row data for each one.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT user_id
            FROM players
            """
        )

        return [row["user_id"] for row in cur.fetchall()]


# ============================================================
# UNCONSCIOUS / COLLAPSE (see cogs/walk.py)
# ============================================================

def is_unconscious(
    user_id: int
) -> bool:

    """
    Check whether a player is currently collapsed/unconscious.
    """

    player = get_or_create_player(user_id)

    return bool(player["unconscious"])


def set_unconscious(
    user_id: int,
    unconscious: bool
) -> None:

    """
    Mark a player unconscious (collapsed) or conscious again
    (resuscitated/treated). Does NOT touch location, stats, or
    Discord roles — callers (cogs/walk.py, cogs/ambulance.py)
    handle those alongside this, since exactly what happens
    differs between collapsing, on-the-spot resuscitation, and
    hospital treatment.
    """

    update_player(
        user_id,
        unconscious=1 if unconscious else 0,
    )


def get_unconscious_players() -> list[sqlite3.Row]:

    """
    Return every player row currently marked unconscious. Used
    by cogs/walk.py's collapse-timeout scan and by admin/status
    tooling.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM players
            WHERE unconscious = 1
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


def all_players_with_bank_accounts() -> list[sqlite3.Row]:

    """
    Return every player row that has an OPEN bank account (i.e. a
    matching row in bank_accounts) — used by !view-balances so
    players who exist in the `players` table (registered via
    !registerplayers) but never had !create-account run for them
    don't show up as if they had a Savings balance.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT p.*
            FROM players p
            JOIN bank_accounts b ON b.user_id = p.user_id
            """
        )

        return cur.fetchall()


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


# ============================================================
# LOCATIONS (dynamic, admin-registered)
# ============================================================

def create_location(
    code: str,
    name: str,
    channel_name: str,
    zone: str,
    distance: float,
    category: str,
    created_by: int
) -> bool:

    """
    Register a new location.

    Returns:
        True  = location created
        False = a location with this code already exists
    """

    with _lock:

        cur = _conn.execute(
            """
            INSERT OR IGNORE INTO locations (
                code, name, channel_name, zone,
                distance, category, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                name,
                channel_name,
                zone,
                float(distance),
                category,
                str(created_by),
            )
        )

        _conn.commit()

        return cur.rowcount > 0


def get_location(
    code: str
):

    """Return a single registered location row, or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM locations
            WHERE code = ?
            """,
            (code,)
        )

        return cur.fetchone()


def get_all_dynamic_locations():

    """
    Return every registered location (dict keyed by code, same
    shape callers expect from iterating config.LOCATIONS), so
    routing.py / permissions.py can merge these in alongside the
    hand-authored LOCATIONS dict.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM locations
            """
        )

        rows = cur.fetchall()

    return {
        row["code"]: dict(row)
        for row in rows
    }


def location_exists(
    code: str
) -> bool:
    """
    True if `code` is a real location — either hand-authored in
    config.LOCATIONS, or registered at runtime via
    !location-registration/!business-registration (the `locations`
    table).

    Every travel-related "is this a valid destination" check
    (cogs/walk.py, cogs/travel.py, cogs/taxi.py, cogs/dispatch.py)
    should use this instead of `code in LOCATIONS` directly, or
    dynamically registered locations/businesses will always read
    as nonexistent.
    """

    return (
        code in LOCATIONS
        or get_location(code) is not None
    )


def get_location_data(
    code: str
):
    """
    Return a LOCATIONS-shaped dict (name/channel/zone/roles) for
    `code`, checking config.LOCATIONS first and falling back to a
    dynamically registered `locations` row.

    Dynamically registered locations never carry a `roles`
    restriction (it's always None = open to everyone) — role-based
    access only applies to hand-authored LOCATIONS entries; a
    locked business is gated separately via Discord channel
    overwrites, not this field.

    Returns None if `code` isn't registered anywhere.
    """

    static_location = LOCATIONS.get(code)

    if static_location is not None:
        return static_location

    row = get_location(code)

    if row is None:
        return None

    return {
        "name": row["name"],
        "channel": row["channel_name"],
        "zone": row["zone"],
        "roles": None,
    }


def delete_location(
    code: str
) -> bool:

    """
    Permanently remove a registered location.

    Callers are responsible for enforcing !close-account first —
    this function does not check for an open balance itself.

    Returns:
        True  = a location was deleted
        False = no location with this code existed
    """

    with _lock:

        cur = _conn.execute(
            """
            DELETE FROM locations
            WHERE code = ?
            """,
            (code,)
        )

        _conn.commit()

        return cur.rowcount > 0


# ============================================================
# SUB-LOCATIONS
# ============================================================

def create_sub_location(
    code: str,
    parent_code: str,
    name: str,
    channel_name: str,
    access: str,
    role_name: str | None,
    created_by: int
) -> bool:

    """
    Attach a sub-location (a "room") to an existing parent
    location — either a config.py LOCATIONS code or a dynamically
    registered one.

    access is "public" (opens for anyone who arrives at the
    parent) or "role" (opens only for role_name holders — a
    comma-separated list of role names is accepted; a member
    qualifies by holding ANY one of them).

    Returns:
        True  = sub-location created
        False = a sub-location with this code already exists
    """

    with _lock:

        cur = _conn.execute(
            """
            INSERT OR IGNORE INTO sub_locations (
                code, parent_code, name, channel_name,
                access, role_name, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                parent_code,
                name,
                channel_name,
                access,
                role_name,
                str(created_by),
            )
        )

        _conn.commit()

        return cur.rowcount > 0


def get_sub_locations_for_parent(
    parent_code: str
):

    """Return every sub-location attached to a parent location code."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM sub_locations
            WHERE parent_code = ?
            """,
            (parent_code,)
        )

        return cur.fetchall()


def get_all_sub_locations():

    """Return every sub-location row that exists, regardless of parent."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM sub_locations
            """
        )

        return cur.fetchall()


def get_sub_location(
    code: str
):

    """Return a single sub-location row, or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM sub_locations
            WHERE code = ?
            """,
            (code,)
        )

        return cur.fetchone()


def delete_sub_location(
    code: str
) -> bool:

    """
    Permanently remove a sub-location.

    Returns:
        True  = a sub-location was deleted
        False = no sub-location with this code existed
    """

    with _lock:

        cur = _conn.execute(
            """
            DELETE FROM sub_locations
            WHERE code = ?
            """,
            (code,)
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
# INSTITUTION ACCOUNTS (Central Bank of Eko / Treasury)
# ============================================================

def get_institution_account(
    code: str
) -> sqlite3.Row | None:

    """Return a single institution account row, or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM institution_accounts
            WHERE code = ?
            """,
            (code,)
        )

        return cur.fetchone()


def all_institution_accounts() -> list[sqlite3.Row]:

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM institution_accounts
            """
        )

        return cur.fetchall()


def adjust_institution_balance(
    code: str,
    delta: int
) -> tuple[bool, str]:

    """
    Add (positive delta) or subtract (negative delta) Naira from
    an institution account's balance. Atomic — a negative delta
    is only ever applied if the account has sufficient funds.

    Returns:
        (True, "ok")             on success
        (False, "no_such_account") if code isn't registered
        (False, "insufficient_funds") on an over-large debit
    """

    delta = int(delta)

    with _lock:

        if get_institution_account(code) is None:
            return (False, "no_such_account")

        if delta < 0:

            cur = _conn.execute(
                """
                UPDATE institution_accounts
                SET balance = balance + ?
                WHERE code = ?
                  AND balance >= ?
                """,
                (delta, code, -delta)
            )

            if cur.rowcount == 0:
                _conn.commit()
                return (False, "insufficient_funds")

        else:

            _conn.execute(
                """
                UPDATE institution_accounts
                SET balance = balance + ?
                WHERE code = ?
                """,
                (delta, code)
            )

        _conn.commit()

        return (True, "ok")


def reset_institution_accounts() -> None:

    """
    Reset every institution account's balance back to its
    starting_balance. Called from reset_database() — the rows
    themselves are never deleted, only the balances restored,
    per spec ("Central Bank of Eko / Treasury ... resets with the
    database").
    """

    with _lock:

        _conn.execute(
            """
            UPDATE institution_accounts
            SET balance = starting_balance
            """
        )

        _conn.commit()


# ============================================================
# PHASE 2 BANKING — CASH BALANCE / WITHDRAWALS / ADJUSTMENTS
# ============================================================
#
# `players.balance` is the BANK balance (gated behind
# has_bank_account/create_bank_account above). `players.cash_balance`
# is physical Naira in hand — usable anywhere, no account needed.
# ============================================================

def withdraw_to_cash(
    user_id: int,
    amount: int
) -> tuple[bool, str]:

    """
    Move Naira from a player's bank balance to their cash balance
    (!with, ATM sub-location only — the location gate is enforced
    by the calling cog, not here).

    Returns:
        (True, "ok")             on success
        (False, "no_account")    if the player has no bank account
        (False, "invalid_amount") amount <= 0
        (False, "insufficient_funds")
    """

    amount = int(amount)

    if amount <= 0:
        return (False, "invalid_amount")

    with _lock:

        if not has_bank_account(user_id):
            return (False, "no_account")

        cur = _conn.execute(
            """
            UPDATE players
            SET balance = balance - ?,
                cash_balance = cash_balance + ?
            WHERE user_id = ?
              AND balance >= ?
            """,
            (amount, amount, str(user_id), amount)
        )

        if cur.rowcount == 0:
            _conn.commit()
            return (False, "insufficient_funds")

        _conn.commit()

        return (True, "ok")


def cash_transfer(
    sender_id: int,
    recipient_id: int,
    amount: int
) -> tuple[bool, str]:

    """
    Move Naira from one player's cash balance to another's
    (!pay, once the receiver accepts). No bank account required —
    cash is usable by anyone.

    Returns:
        (True, "ok")             on success
        (False, "invalid_amount") amount <= 0
        (False, "insufficient_funds")
    """

    amount = int(amount)

    if amount <= 0:
        return (False, "invalid_amount")

    with _lock:

        cur = _conn.execute(
            """
            UPDATE players
            SET cash_balance = cash_balance - ?
            WHERE user_id = ?
              AND cash_balance >= ?
            """,
            (amount, str(sender_id), amount)
        )

        if cur.rowcount == 0:
            _conn.commit()
            return (False, "insufficient_funds")

        _conn.execute(
            """
            UPDATE players
            SET cash_balance = cash_balance + ?
            WHERE user_id = ?
            """,
            (amount, str(recipient_id))
        )

        _conn.commit()

        return (True, "ok")


def cb_withdraw_to_player(
    user_id: int,
    amount: int
) -> tuple[bool, str]:

    """
    Central Bank of Eko -> a player's BANK balance (!cb-with,
    cbe-chairman only — role/location gate enforced by the
    calling cog).

    The player does not need a bank account for this — the
    chairman is depositing INTO the bank on their behalf, same as
    a real central bank crediting an account that may not yet be
    "open" for self-service use. Their bank balance still exists
    as a Naira pool regardless of has_bank_account (see
    create_player docstring).

    Returns:
        (True, "ok")             on success
        (False, "invalid_amount") amount <= 0
        (False, "insufficient_funds") Central Bank itself is short
    """

    amount = int(amount)

    if amount <= 0:
        return (False, "invalid_amount")

    with _lock:

        debited, reason = adjust_institution_balance(
            "central_bank",
            -amount
        )

        if not debited:
            return (False, reason)

        _conn.execute(
            """
            UPDATE players
            SET balance = balance + ?
            WHERE user_id = ?
            """,
            (amount, str(user_id))
        )

        _conn.commit()

        return (True, "ok")


def adjust_player_balance(
    user_id: int,
    delta: int
) -> tuple[bool, str]:

    """
    Manual correction to a player's BANK balance (!adjust). delta
    may be positive (credit) or negative (debit) — a debit only
    applies if the player has sufficient bank balance.

    Returns:
        (True, "ok")             on success
        (False, "insufficient_funds") on an over-large debit
    """

    delta = int(delta)

    with _lock:

        if delta < 0:

            cur = _conn.execute(
                """
                UPDATE players
                SET balance = balance + ?
                WHERE user_id = ?
                  AND balance >= ?
                """,
                (delta, str(user_id), -delta)
            )

            if cur.rowcount == 0:
                _conn.commit()
                return (False, "insufficient_funds")

        else:

            _conn.execute(
                """
                UPDATE players
                SET balance = balance + ?
                WHERE user_id = ?
                """,
                (delta, str(user_id))
            )

        _conn.commit()

        return (True, "ok")


# ============================================================
# CURRENT ACCOUNTS (Phase 3 — government/office accounts)
# ============================================================
#
# Created via !create-current-account (cogs/banking.py). See
# the `current_accounts` table docstring in init_db() — these
# hold no balance of their own; every transfer into one sweeps
# straight into institution_accounts["central_bank"].
# ============================================================

def create_current_account(
    code: str,
    name: str,
    channel_name: str,
    created_by: int
) -> bool:

    """
    Register a new government/office current account.

    Returns:
        True  = account created
        False = a current account with this code already exists
    """

    with _lock:

        cur = _conn.execute(
            """
            INSERT OR IGNORE INTO current_accounts (
                code, name, channel_name, created_by
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                code,
                name,
                channel_name,
                str(created_by),
            )
        )

        _conn.commit()

        return cur.rowcount > 0


def get_current_account(
    code: str
) -> sqlite3.Row | None:

    """Return a single current account row, or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM current_accounts
            WHERE code = ?
            """,
            (code,)
        )

        return cur.fetchone()


def all_current_accounts() -> list[sqlite3.Row]:

    """Return every registered current account."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM current_accounts
            """
        )

        return cur.fetchall()


def current_account_transfer(
    sender_id: int,
    code: str,
    amount: int
) -> tuple[bool, str]:

    """
    Transfer Naira from a player's BANK balance into a current
    account (!transfer, once `code` resolves to a registered
    current account instead of a player mention).

    The current account itself never holds the money — it settles
    straight into institution_accounts["central_bank"], atomically
    with the sender's debit (per spec: "Transfers into these
    settle permanently into Central Bank of Eko"). The caller is
    responsible for posting the public receipt to the account's
    linked channel and logging to cbe-log — this function only
    moves the money.

    Returns:
        (True, "ok")             on success
        (False, "invalid_amount") amount <= 0
        (False, "no_such_account") no current account with this code
        (False, "no_sender_account") sender has no bank account
        (False, "insufficient_funds")
    """

    amount = int(amount)

    if amount <= 0:
        return (False, "invalid_amount")

    with _lock:

        if get_current_account(code) is None:
            return (False, "no_such_account")

        if not has_bank_account(sender_id):
            return (False, "no_sender_account")

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

        _conn.commit()

        # Always succeeds: a positive delta on adjust_institution_balance
        # never fails on funds, and "central_bank" is a fixed row
        # guaranteed to exist by init_db().
        adjust_institution_balance("central_bank", amount)

        return (True, "ok")


# ============================================================
# BUSINESSES (Phase 4 — registration half)
# ============================================================
#
# Created via !business-registration (cogs/business_admin.py).
# See the `businesses` table docstring in init_db().
# ============================================================

def create_business(
    code: str,
    name: str,
    owner_id: int,
    business_type: str,
    owner_role_name: str,
    created_by: int
) -> bool:

    """
    Register the bookkeeping row for a new business, alongside its
    matching `locations` row (created separately by the caller).

    Returns:
        True  = business created
        False = a business with this code already exists
    """

    with _lock:

        cur = _conn.execute(
            """
            INSERT OR IGNORE INTO businesses (
                code, name, owner_id, business_type,
                owner_role_name, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                name,
                str(owner_id),
                business_type,
                owner_role_name,
                str(created_by),
            )
        )

        _conn.commit()

        return cur.rowcount > 0


def get_business(
    code: str
) -> sqlite3.Row | None:

    """Return a single registered business row, or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM businesses
            WHERE code = ?
            """,
            (code,)
        )

        return cur.fetchone()


def get_businesses_by_owner(
    owner_id: int
) -> list[sqlite3.Row]:

    """Return every business a given member owns."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM businesses
            WHERE owner_id = ?
            """,
            (str(owner_id),)
        )

        return cur.fetchall()


def all_businesses() -> list[sqlite3.Row]:

    """Return every registered business."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM businesses
            """
        )

        return cur.fetchall()


# ============================================================
# BUSINESS ACCOUNTS (Phase 4 — financial half)
# ============================================================
#
# Created via !create-business-account (cogs/banking.py), a
# deliberately separate step from !business-registration above.
# See the `business_accounts` table docstring in init_db() — these
# hold a real, standalone balance that is never swept into
# institution_accounts["central_bank"].
# ============================================================

def has_business_account(
    code: str
) -> bool:

    """True if `code` is a registered business AND has an open account."""

    return get_business_account(code) is not None


def create_business_account(
    code: str,
    receipt_channel_name: str,
    created_by: int
) -> tuple[bool, str]:

    """
    Open the financial account for an already-registered business.

    Returns:
        (True, "ok")                on success
        (False, "no_such_business") no business with this code exists
        (False, "already_open")     the account already exists
    """

    with _lock:

        if get_business(code) is None:
            return (False, "no_such_business")

        cur = _conn.execute(
            """
            INSERT OR IGNORE INTO business_accounts (
                code, balance, receipt_channel_name, created_by
            )
            VALUES (?, 0, ?, ?)
            """,
            (
                code,
                receipt_channel_name,
                str(created_by),
            )
        )

        _conn.commit()

        if cur.rowcount == 0:
            return (False, "already_open")

        return (True, "ok")


def get_business_account(
    code: str
) -> sqlite3.Row | None:

    """Return a single business account row, or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT *
            FROM business_accounts
            WHERE code = ?
            """,
            (code,)
        )

        return cur.fetchone()


def all_business_accounts() -> list[sqlite3.Row]:

    """
    Return every open business account, each row carrying its
    parent business's name/owner_id/business_type alongside the
    account's own balance/receipt_channel_name — convenient for
    !view-balances-style reporting without a second lookup per row.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT
                ba.code            AS code,
                ba.balance         AS balance,
                ba.receipt_channel_name AS receipt_channel_name,
                b.name             AS name,
                b.owner_id         AS owner_id,
                b.business_type    AS business_type
            FROM business_accounts ba
            JOIN businesses b ON b.code = ba.code
            """
        )

        return cur.fetchall()


def business_account_transfer(
    sender_id: int,
    code: str,
    amount: int
) -> tuple[bool, str]:

    """
    Transfer Naira from a player's BANK balance into a business
    account (!transfer, once `code` resolves to a registered
    business with an open account instead of a player mention or
    current account).

    Unlike current_account_transfer(), the money settles directly
    into that business's OWN balance — never into the Central Bank
    of Eko (private revenue, per spec).

    Returns:
        (True, "ok")             on success
        (False, "invalid_amount") amount <= 0
        (False, "no_such_account") no business with an open account
                                    for this code
        (False, "no_sender_account") sender has no bank account
        (False, "insufficient_funds")
    """

    amount = int(amount)

    if amount <= 0:
        return (False, "invalid_amount")

    with _lock:

        if get_business_account(code) is None:
            return (False, "no_such_account")

        if not has_bank_account(sender_id):
            return (False, "no_sender_account")

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
            UPDATE business_accounts
            SET balance = balance + ?
            WHERE code = ?
            """,
            (amount, code)
        )

        _conn.commit()

        return (True, "ok")


def adjust_business_balance(
    code: str,
    delta: int
) -> tuple[bool, str]:

    """
    Manual correction to a business account's balance (!adjust).
    delta may be positive (credit) or negative (debit) — a debit
    only applies if the account has sufficient balance.

    Returns:
        (True, "ok")               on success
        (False, "no_such_account") no open business account for this code
        (False, "insufficient_funds") on an over-large debit
    """

    delta = int(delta)

    with _lock:

        if get_business_account(code) is None:
            return (False, "no_such_account")

        if delta < 0:

            cur = _conn.execute(
                """
                UPDATE business_accounts
                SET balance = balance + ?
                WHERE code = ?
                  AND balance >= ?
                """,
                (delta, code, -delta)
            )

            if cur.rowcount == 0:
                _conn.commit()
                return (False, "insufficient_funds")

        else:

            _conn.execute(
                """
                UPDATE business_accounts
                SET balance = balance + ?
                WHERE code = ?
                """,
                (delta, code)
            )

        _conn.commit()

        return (True, "ok")


# ============================================================
# BUSINESS ITEMS (Phase 5 — shop catalog / stock)
# ============================================================
#
# Backs cogs/business_shop.py's !add / !menu / !buy / !sell /
# !order. Licensing (is `category` one this business_type may
# sell) is enforced by the calling cog against
# BUSINESS_TYPE_CATEGORIES — not here.
# ============================================================

def add_business_item(
    business_code: str,
    category: str,
    item_name: str,
    price: int,
    qty: int,
    created_by: int
) -> sqlite3.Row:

    """
    Add (or restock/reprice, if an item with this name already
    exists for this business — case-insensitively) a catalog line.

    A re-!add tops up `stock` by `qty` (rather than replacing it)
    and updates `price`/`category` to whatever was just given, so
    re-running !add is always safe.

    Returns the resulting business_items row.
    """

    with _lock:

        existing = _conn.execute(
            """
            SELECT * FROM business_items
            WHERE business_code = ?
              AND item_name = ?
            """,
            (business_code, item_name)
        ).fetchone()

        if existing is not None:

            _conn.execute(
                """
                UPDATE business_items
                SET category = ?,
                    price = ?,
                    stock = stock + ?
                WHERE item_id = ?
                """,
                (category, price, qty, existing["item_id"])
            )

        else:

            _conn.execute(
                """
                INSERT INTO business_items (
                    business_code, category, item_name,
                    price, stock, created_by
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    business_code,
                    category,
                    item_name,
                    price,
                    qty,
                    str(created_by),
                )
            )

        _conn.commit()

        return _conn.execute(
            """
            SELECT * FROM business_items
            WHERE business_code = ?
              AND item_name = ?
            """,
            (business_code, item_name)
        ).fetchone()


def get_business_items(
    business_code: str
) -> list[sqlite3.Row]:

    """Return every catalog line for a business (!menu)."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT * FROM business_items
            WHERE business_code = ?
            ORDER BY item_name COLLATE NOCASE
            """,
            (business_code,)
        )

        return cur.fetchall()


def get_business_item(
    business_code: str,
    item_name: str
) -> sqlite3.Row | None:

    """Return a single catalog line by name (case-insensitive), or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT * FROM business_items
            WHERE business_code = ?
              AND item_name = ?
            """,
            (business_code, item_name)
        )

        return cur.fetchone()


def adjust_business_item_stock(
    business_code: str,
    item_name: str,
    delta: int
) -> tuple[bool, str]:

    """
    Adjust a catalog line's stock. `delta` may be positive
    (!order restock) or negative (!sell fulfillment/walk-up sale).

    Returns:
        (True, "ok")                 on success
        (False, "no_such_item")      no catalog line with this
                                      name exists for this business
        (False, "insufficient_stock") a negative delta would take
                                       stock below 0
    """

    delta = int(delta)

    with _lock:

        item = _conn.execute(
            """
            SELECT * FROM business_items
            WHERE business_code = ?
              AND item_name = ?
            """,
            (business_code, item_name)
        ).fetchone()

        if item is None:
            return (False, "no_such_item")

        if delta < 0 and item["stock"] + delta < 0:
            return (False, "insufficient_stock")

        _conn.execute(
            """
            UPDATE business_items
            SET stock = stock + ?
            WHERE item_id = ?
            """,
            (delta, item["item_id"])
        )

        _conn.commit()

        return (True, "ok")


# ============================================================
# CASH REGISTERS (Phase 5 — !buy tabs)
# ============================================================
#
# Backs the !buy -> !pay/!transfer -> !sell pipeline in
# cogs/business_shop.py and the register-settlement path in
# cogs/banking.py's !pay/!transfer. See the `cash_registers`
# table docstring in init_db() for the status lifecycle.
# ============================================================

def get_open_register(
    business_code: str,
    customer_id: int
) -> sqlite3.Row | None:

    """Return the OPEN (unpaid) register for this pair, or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT * FROM cash_registers
            WHERE business_code = ?
              AND customer_id = ?
              AND status = 'open'
            """,
            (business_code, str(customer_id))
        )

        return cur.fetchone()


def get_paid_register(
    business_code: str,
    customer_id: int
) -> sqlite3.Row | None:

    """
    Return the PAID (settled, awaiting fulfillment) register for
    this pair, or None. This is what !sell <@customer> looks for.
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT * FROM cash_registers
            WHERE business_code = ?
              AND customer_id = ?
              AND status = 'paid'
            """,
            (business_code, str(customer_id))
        )

        return cur.fetchone()


def get_register(
    register_id: int
) -> sqlite3.Row | None:

    """Return a single register row by id, or None."""

    with _lock:

        cur = _conn.execute(
            """
            SELECT * FROM cash_registers
            WHERE register_id = ?
            """,
            (register_id,)
        )

        return cur.fetchone()


def add_to_register(
    business_code: str,
    customer_id: int,
    item_name: str,
    price: int,
    qty: int
) -> sqlite3.Row:

    """
    Add a line to the OPEN register between this customer and this
    business — creating one if none exists yet (per spec, "one per
    (owner, customer)"). If the same item_name is already on the
    open register, its qty is summed and price updated to the
    current price rather than adding a duplicate line.

    Does NOT touch stock — !buy only runs a tab; !sell deducts
    stock once the tab is paid and fulfilled.

    Returns the resulting cash_registers row.
    """

    customer_id = str(customer_id)

    with _lock:

        register = _conn.execute(
            """
            SELECT * FROM cash_registers
            WHERE business_code = ?
              AND customer_id = ?
              AND status = 'open'
            """,
            (business_code, customer_id)
        ).fetchone()

        if register is None:

            cur = _conn.execute(
                """
                INSERT INTO cash_registers (
                    business_code, customer_id, items, total, status
                )
                VALUES (?, ?, '[]', 0, 'open')
                """,
                (business_code, customer_id)
            )

            register_id = cur.lastrowid
            lines = []

        else:

            register_id = register["register_id"]
            lines = json.loads(register["items"])

        line = next(
            (l for l in lines if l["item_name"] == item_name),
            None
        )

        if line is not None:
            line["qty"] += qty
            line["price"] = price
        else:
            lines.append({
                "item_name": item_name,
                "price": price,
                "qty": qty,
            })

        total = sum(l["price"] * l["qty"] for l in lines)

        _conn.execute(
            """
            UPDATE cash_registers
            SET items = ?,
                total = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE register_id = ?
            """,
            (json.dumps(lines), total, register_id)
        )

        _conn.commit()

        return _conn.execute(
            """
            SELECT * FROM cash_registers
            WHERE register_id = ?
            """,
            (register_id,)
        ).fetchone()


def cancel_register(
    register_id: int
) -> bool:

    """
    Cancel an OPEN register (!close-register). Returns False (no-op)
    if the register isn't currently open — a paid or already-
    fulfilled/cancelled register can't be re-cancelled.
    """

    with _lock:

        cur = _conn.execute(
            """
            UPDATE cash_registers
            SET status = 'cancelled',
                updated_at = CURRENT_TIMESTAMP
            WHERE register_id = ?
              AND status = 'open'
            """,
            (register_id,)
        )

        _conn.commit()

        return cur.rowcount > 0


def fulfill_register(
    register_id: int
) -> bool:

    """
    Mark a PAID register as fulfilled, once !sell has deducted
    stock for every line. Returns False (no-op) if the register
    isn't currently paid.
    """

    with _lock:

        cur = _conn.execute(
            """
            UPDATE cash_registers
            SET status = 'fulfilled',
                updated_at = CURRENT_TIMESTAMP
            WHERE register_id = ?
              AND status = 'paid'
            """,
            (register_id,)
        )

        _conn.commit()

        return cur.rowcount > 0


def get_open_registers_for_owner_customer(
    owner_id: int,
    customer_id: int
) -> list[sqlite3.Row]:

    """
    Return every OPEN register between `customer_id` and any
    business owned by `owner_id` — each row carries the parent
    business's code/name alongside the register's own
    items/total, for cogs/banking.py's !pay/!transfer register-
    settlement lookup (a customer's payment to a business owner
    should settle a matching open tab instead of landing as a
    plain transfer — see spec).
    """

    with _lock:

        cur = _conn.execute(
            """
            SELECT
                cr.register_id     AS register_id,
                cr.business_code   AS business_code,
                cr.customer_id     AS customer_id,
                cr.items           AS items,
                cr.total           AS total,
                cr.status          AS status,
                b.name             AS business_name
            FROM cash_registers cr
            JOIN businesses b ON b.code = cr.business_code
            WHERE b.owner_id = ?
              AND cr.customer_id = ?
              AND cr.status = 'open'
            """,
            (str(owner_id), str(customer_id))
        )

        return cur.fetchall()


def settle_register(
    register_id: int,
    amount: int
) -> tuple[bool, str]:

    """
    Flip an OPEN register to "paid" once its exact `total` has
    been handed over via !pay or !transfer (cogs/banking.py).
    Re-checks the match atomically under the lock — the calling
    cog should already have compared amounts, but this guards
    against the tab changing (another item added) in between.

    Returns:
        (True, "ok")                on success
        (False, "not_found")        no register with this id
        (False, "not_open")         register isn't open (already
                                     paid/cancelled/fulfilled)
        (False, "amount_mismatch")  amount != the register's
                                     current total
    """

    amount = int(amount)

    with _lock:

        register = _conn.execute(
            """
            SELECT * FROM cash_registers
            WHERE register_id = ?
            """,
            (register_id,)
        ).fetchone()

        if register is None:
            return (False, "not_found")

        if register["status"] != "open":
            return (False, "not_open")

        if register["total"] != amount:
            return (False, "amount_mismatch")

        _conn.execute(
            """
            UPDATE cash_registers
            SET status = 'paid',
                updated_at = CURRENT_TIMESTAMP
            WHERE register_id = ?
            """,
            (register_id,)
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
        Stats            = STAT_STARTING_VALUE each
        Cash balance     = 0

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
                traveling = 0,
                hunger = ?,
                thirst = ?,
                health = ?,
                hygiene = ?,
                breath = ?,
                happiness = ?,
                cash_balance = 0
            """,
            (
                STARTING_BALANCE,
                STARTING_LOCATION,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
                STAT_STARTING_VALUE,
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

    NOT reset by this function, deliberately:
        `locations`, `sub_locations`, `current_accounts`,
        `businesses`, `business_accounts`, `business_items`, and
        `cash_registers` — registered locations (including
        businesses), their attached sub-locations, registered
        government/office current accounts, registered businesses
        with their standalone (never-swept-to-Central-Bank)
        balances, their shop catalogs/stock, and any open/settled
        tabs must all survive a database reset. There is no DELETE
        for any of these tables anywhere in this function.

    ALSO reset by this function (added in Phase 2):
        `institution_accounts` — Central Bank of Eko and Treasury
        balances are restored to their starting_balance (rows are
        kept, never deleted — see reset_institution_accounts()).
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

    # --------------------------------------------------------
    # Restore Central Bank of Eko / Treasury to their starting
    # balances. Done outside the `with _lock:` block above since
    # reset_institution_accounts() takes the lock itself.
    # --------------------------------------------------------

    reset_institution_accounts()

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
# INVENTORY (personal item stacks — !give / !inv)
# ============================================================

def add_inventory_item(
    user_id: int,
    category: str,
    item_name: str,
    qty: int = 1,
) -> sqlite3.Row:
    """
    Add `qty` of `item_name` (in `category`) to user_id's
    inventory — stacks onto an existing row for that item name
    (case-insensitive) if one exists, creates a new row otherwise.
    Returns the resulting row.
    """
    with _lock:
        _conn.execute(
            """
            INSERT INTO inventory (user_id, category, item_name, qty)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, item_name)
            DO UPDATE SET qty = qty + excluded.qty
            """,
            (str(user_id), category, item_name, int(qty))
        )
        _conn.commit()

        cur = _conn.execute(
            """
            SELECT * FROM inventory
            WHERE user_id = ? AND item_name = ? COLLATE NOCASE
            """,
            (str(user_id), item_name)
        )
        return cur.fetchone()


def get_inventory(user_id: int) -> list[sqlite3.Row]:
    """Every item stack a player currently holds (qty > 0), grouped
    for display by ordering on category then item_name."""
    with _lock:
        cur = _conn.execute(
            """
            SELECT * FROM inventory
            WHERE user_id = ? AND qty > 0
            ORDER BY category, item_name
            """,
            (str(user_id),)
        )
        return cur.fetchall()


def get_inventory_item(user_id: int, item_name: str) -> "sqlite3.Row | None":
    with _lock:
        cur = _conn.execute(
            """
            SELECT * FROM inventory
            WHERE user_id = ? AND item_name = ? COLLATE NOCASE
            """,
            (str(user_id), item_name)
        )
        return cur.fetchone()


def remove_inventory_item(
    user_id: int,
    item_name: str,
    qty: int,
) -> tuple[bool, str]:
    """
    Remove `qty` of `item_name` from user_id's inventory. Returns
    (False, "not_found") if they don't have that item at all, or
    (False, "insufficient_qty") if they have less than `qty`. The
    row is deleted entirely once its qty reaches 0.
    """
    with _lock:
        row = _conn.execute(
            """
            SELECT * FROM inventory
            WHERE user_id = ? AND item_name = ? COLLATE NOCASE
            """,
            (str(user_id), item_name)
        ).fetchone()

        if row is None:
            return False, "not_found"

        if row["qty"] < qty:
            return False, "insufficient_qty"

        remaining = row["qty"] - qty

        if remaining <= 0:
            _conn.execute(
                "DELETE FROM inventory WHERE item_id = ?", (row["item_id"],)
            )
        else:
            _conn.execute(
                "UPDATE inventory SET qty = ? WHERE item_id = ?",
                (remaining, row["item_id"])
            )

        _conn.commit()
        return True, "ok"


def transfer_inventory_item(
    giver_id: int,
    recipient_id: int,
    item_name: str,
    qty: int,
) -> tuple[bool, str]:
    """
    Move `qty` of `item_name` from giver_id to recipient_id's
    inventory. Same (False, reason) shape as
    remove_inventory_item — "not_found" or "insufficient_qty" —
    for a giver who can't cover the transfer.
    """
    with _lock:
        row = _conn.execute(
            """
            SELECT category FROM inventory
            WHERE user_id = ? AND item_name = ? COLLATE NOCASE
            """,
            (str(giver_id), item_name)
        ).fetchone()

        if row is None:
            return False, "not_found"

        ok, reason = remove_inventory_item(giver_id, item_name, qty)

        if not ok:
            return False, reason

        add_inventory_item(recipient_id, row["category"], item_name, qty)
        return True, "ok"


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
