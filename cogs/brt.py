"""
BRT CARD SYSTEM.

This file contains the core BRT card helper functions.

The Discord commands are handled separately by:

    cogs/brt_card.py

This file handles:

    - BRT card creation
    - BRT card balance
    - BRT card recharge
    - BRT fare checking
    - BRT fare deduction

BRT transportation money is kept separate from the
normal player balance.

A player must have:

    BRT card database record
    BRT Card Discord role

before they can use the BRT system.
"""

from config import BRT_FARES
import database


# ============================================================
# BRT CARD
# ============================================================

def create_card(
    user_id: int
) -> bool:
    """
    Create a BRT card for a player.

    Returns:

        True  = card successfully created
        False = player already has a card
    """

    return database.create_brt_card(
        user_id
    )


def has_card(
    user_id: int
) -> bool:
    """
    Check whether a player has a BRT card.
    """

    return database.has_brt_card(
        user_id
    )


# ============================================================
# BALANCE
# ============================================================

def get_balance(
    user_id: int
) -> int:
    """
    Return the player's BRT card balance.
    """

    return database.get_brt_balance(
        user_id
    )


def recharge(
    user_id: int,
    amount: int
) -> bool:
    """
    Recharge a BRT card.

    Returns False when:

        - amount is invalid
        - player does not have a BRT card
    """

    amount = int(amount)

    if amount <= 0:
        return False

    if not has_card(user_id):
        return False

    return database.add_brt_balance(
        user_id,
        amount
    )


# ============================================================
# FARES
# ============================================================

def get_fare(
    origin_zone: str,
    destination_zone: str
) -> int:
    """
    Return the BRT fare for a journey.

    The BRT system uses zone-to-zone fares.

    Same-zone travel:
        0

    Ghetto -> Mainland:
        B1 fare

    Mainland -> Island:
        B2 fare

    Ghetto -> Island:
        B3 fare

    The reverse direction uses the same fare.
    """

    origin_zone = str(
        origin_zone
    ).strip().lower()

    destination_zone = str(
        destination_zone
    ).strip().lower()

    if origin_zone == destination_zone:
        return 0

    key = frozenset({
        origin_zone,
        destination_zone
    })

    return int(
        BRT_FARES.get(
            key,
            0
        )
    )


# ============================================================
# BALANCE CHECK
# ============================================================

def has_enough_funds(
    user_id: int,
    fare: int
) -> bool:
    """
    Check whether the BRT card has enough money.
    """

    if not has_card(user_id):
        return False

    return (
        get_balance(user_id)
        >= int(fare)
    )


# ============================================================
# PAY FARE
# ============================================================

def pay_fare(
    user_id: int,
    fare: int
) -> bool:
    """
    Deduct a BRT fare.

    The deduction happens only after the passenger
    has successfully been accepted onto the bus.

    Returns:

        True  = payment successful
        False = insufficient funds / no card
    """

    fare = int(fare)

    if fare < 0:
        return False

    if not has_card(user_id):
        return False

    if not has_enough_funds(
        user_id,
        fare
    ):
        return False

    return database.deduct_brt_balance(
        user_id,
        fare
    )


# ============================================================
# ROUTE FARE
# ============================================================

def fare_for_route(
    origin_zone: str,
    destination_zone: str
) -> int:
    """
    Convenience function for the bus system.

    Example:

        fare_for_route(
            "ghetto",
            "mainland"
        )

    Returns the applicable BRT fare.
    """

    return get_fare(
        origin_zone,
        destination_zone
  )
