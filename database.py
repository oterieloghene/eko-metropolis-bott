"""
Database layer. SQLite file by default (DB_PATH env var), authoritative
source for player location (requirements #19, #26).

NOTE ON RENDER DEPLOYMENT:
Render's free/standard web & background workers have EPHEMERAL disks —
a redeploy or restart can wipe a local SQLite file. For a persistent RP
economy bot, either:
  (a) attach a Render Disk (paid, persistent volume) and point DB_PATH at it, or
  (b) swap this module for Postgres (Render offers free/managed Postgres).
This module is written so swapping the storage backend later only means
rewriting this one file — nothing else in the bot touches SQL directly.
"""

import json
import os
import sqlite3
import threading

from config import STARTING_BALANCE, STARTING_LOCATION

DB_PATH = os.environ.get("DB_PATH", "ekobot.db")

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def init_db() -> None:
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


def get_player(user_id: int) -> sqlite3.Row | None:
    with _lock:
        cur = _conn.execute("SELECT * FROM players WHERE user_id = ?", (str(user_id),))
        return cur.fetchone()


def create_player(user_id: int) -> sqlite3.Row:
    """Create a new player with the configured defaults. No-op if they exist."""
    existing = get_player(user_id)
    if existing:
        return existing
    with _lock:
        _conn.execute(
            """
            INSERT INTO players
                (user_id, balance, location, vehicle, vehicle_location,
                 fuel, vehicle_condition, vehicles, traveling)
            VALUES (?, ?, ?, NULL, NULL, 0, 100, '[]', 0)
            """,
            (str(user_id), STARTING_BALANCE, STARTING_LOCATION),
        )
        _conn.commit()
    return get_player(user_id)


def get_or_create_player(user_id: int) -> sqlite3.Row:
    player = get_player(user_id)
    if player is None:
        player = create_player(user_id)
    return player


def update_player(user_id: int, **fields) -> None:
    """
    Update arbitrary columns for a player. `vehicles` should be passed as a
    Python list (it will be JSON-encoded automatically).
    """
    if not fields:
        return
    if "vehicles" in fields and isinstance(fields["vehicles"], list):
        fields["vehicles"] = json.dumps(fields["vehicles"])

    set_clause = ", ".join(f"{col} = ?" for col in fields)
    values = list(fields.values()) + [str(user_id)]
    with _lock:
        _conn.execute(f"UPDATE players SET {set_clause} WHERE user_id = ?", values)
        _conn.commit()


def all_players() -> list[sqlite3.Row]:
    with _lock:
        cur = _conn.execute("SELECT * FROM players")
        return cur.fetchall()


def get_vehicles(user_id: int) -> list:
    player = get_player(user_id)
    if not player:
        return []
    try:
        return json.loads(player["vehicles"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def reset_all_vehicle_data() -> None:
    """
    Explicit, deliberate reset of stale vehicle data across ALL existing
    player records (requirements #15). Does NOT touch balance or location.
    Call manually (e.g. via an admin command) — never automatically.
    """
    with _lock:
        _conn.execute(
            """
            UPDATE players
            SET vehicle = NULL,
                vehicle_location = NULL,
                fuel = 0,
                vehicle_condition = 100,
                vehicles = '[]'
            """
        )
        _conn.commit()


def reset_all_locations(new_location: str = STARTING_LOCATION) -> int:
    """
    Explicit mass location reset for EXISTING players (requirements #21).
    Changing STARTING_LOCATION only affects new players — this must be run
    manually to move existing records. Returns the number of rows updated.
    """
    with _lock:
        cur = _conn.execute("UPDATE players SET location = ?", (new_location,))
        _conn.commit()
        return cur.rowcount
