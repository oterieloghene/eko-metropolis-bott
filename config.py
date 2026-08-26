"""
Eko Metropolis — central configuration.
"""

# ============================================================
# GENERAL
# ============================================================

STARTING_BALANCE = 20_000_000
STARTING_LOCATION = "dealership"


# ============================================================
# ZONES
# ============================================================

ZONE_HUBS = {
    "island",
    "mainland",
    "ghetto",
    "farmland",
}

OVERSEAS = {
    "dubai",
    "maldives",
}


# ============================================================
# TOLL ZONES
# ============================================================

TOLL_ZONES = {
    "mainland": {
        "name": "Third Mainland Bridge",
        "amount": 200,
    },

    "island": {
        "name": "Island Toll Gate",
        "amount": 200,
    },
}


# ============================================================
# LOCATIONS
# ============================================================

LOCATIONS = {

    # ========================================================
    # ISLAND
    # ========================================================

    "island": {
        "name": "Island",
        "channel": "island",
        "zone": "island",
        "roles": None,
    },

    "lekki": {
        "name": "Lekki Phase 1",
        "channel": "lekki-phase-1",
        "zone": "island",
        "roles": [
            "Lekki resident",
            "Island visitor",
        ],
    },

    "ikoyi": {
        "name": "Ikoyi Waterfall Villa",
        "channel": "ikoyi-waterfall-villa",
        "zone": "island",
        "roles": [
            "Ikoyi resident",
            "Island visitor",
        ],
    },

    "eko-atlantic": {
        "name": "Eko Atlantic Penthouse",
        "channel": "eko-atlantic-penthouse",
        "zone": "island",
        "roles": [
            "Eko Atlantic resident",
            "Island visitor",
        ],
    },

    "mayor-villa": {
        "name": "Mayor's Penthouse",
        "channel": "mayors-penthouse",
        "zone": "island",
        "roles": [
            "Presidential Villa resident",
            "Presidential Villa visitor",
        ],
    },

    "deputy-villa": {
        "name": "Deputy's Residence",
        "channel": "deputys-residence",
        "zone": "island",
        "roles": [
            "Presidential Villa resident",
            "Presidential Villa visitor",
        ],
    },

    "guesthouse1": {
        "name": "Villa Guesthouse 1",
        "channel": "villa-guesthouse-1",
        "zone": "island",
        "roles": [
            "Presidential Villa resident",
            "Presidential Villa visitor",
        ],
    },

    "guesthouse2": {
        "name": "Villa Guesthouse 2",
        "channel": "villa-guesthouse-2",
        "zone": "island",
        "roles": [
            "Presidential Villa resident",
            "Presidential Villa visitor",
        ],
    },

    "cos": {
        "name": "Chief of Staff",
        "channel": "chief-of-staff",
        "zone": "island",
        "roles": [
            "Eko chiefs",
            "Eko deputies",
            "government officials",
        ],
    },

    "council": {
        "name": "Eko Council",
        "channel": "eko-council",
        "zone": "island",
        "roles": [
            "Eko chiefs",
            "Eko deputies",
            "government officials",
        ],
    },

    "justice": {
        "name": "Ministry of Justice",
        "channel": "minister-of-justice",
        "zone": "island",
        "roles": [
            "Eko chiefs",
            "Eko deputies",
            "government officials",
        ],
    },

    "housing": {
        "name": "Ministry of Home Affairs & Housing",
        "channel": "minister-of-home-affairs-housing",
        "zone": "island",
        "roles": [
            "Eko chiefs",
            "Eko deputies",
            "government officials",
        ],
    },

    "agriculture": {
        "name": "Ministry of Agriculture",
        "channel": "minister-of-agriculture",
        "zone": "island",
        "roles": [
            "Eko chiefs",
            "Eko deputies",
            "government officials",
        ],
    },

    "clerk": {
        "name": "Clerk Office",
        "channel": "clerk-office",
        "zone": "island",
        "roles": None,
    },

    "bank": {
        "name": "Central Bank of Eko",
        "channel": "banking-hall",
        "zone": "island",
        "roles": None,
    },

    "hospital": {
        "name": "Eko Medical Service",
        "channel": "hospital-lobby",
        "zone": "island",
        "roles": None,
    },

    "police": {
        "name": "Eko Police Department",
        "channel": "precint-reception",
        "zone": "island",
        "roles": None,
    },

    "rental": {
        "name": "Property Development Department",
        "channel": "rental-desk",
        "zone": "island",
        "roles": None,
    },

    "university": {
        "name": "Eko Metropolis University",
        "channel": "vice-chancellors-office",
        "zone": "island",
        "roles": None,
    },

    "lobby": {
        "name": "Eko Lobby",
        "channel": "eko-lobby",
        "zone": "island",
        "roles": None,
    },

    "clubhouse": {
        "name": "Eko Clubhouse",
        "channel": "eko-clubhouse",
        "zone": "island",
        "roles": None,
    },

    "chapel": {
        "name": "Eko City Chapel",
        "channel": "eko-city-chapel",
        "zone": "island",
        "roles": None,
    },


    # ========================================================
    # MAINLAND
    # ========================================================

    "mainland": {
        "name": "Third Mainland Bridge",
        "channel": "3rd-mainland-bridge",
        "zone": "mainland",
        "roles": None,
    },

    "ikeja": {
        "name": "Ikeja Estate",
        "channel": "ikeja-estate",
        "zone": "mainland",
        "roles": [
            "Ikeja resident",
            "Mainland visitor",
        ],
    },

    "yaba": {
        "name": "Yaba Estate",
        "channel": "yaba-estate",
        "zone": "mainland",
        "roles": [
            "Yaba resident",
            "Mainland visitor",
        ],
    },

    "surulere": {
        "name": "Surulere Estate",
        "channel": "surulere-estate",
        "zone": "mainland",
        "roles": [
            "Surulere resident",
            "Mainland visitor",
        ],
    },

    "immigration": {
        "name": "Eko Immigration Office",
        "channel": "help-desk",
        "zone": "mainland",
        "roles": None,
    },

    "market": {
        "name": "Eko Market",
        "channel": "eko-market",
        "zone": "mainland",
        "roles": None,
    },

    "restaurant": {
        "name": "Eko Restaurant",
        "channel": "eko-restaurant",
        "zone": "mainland",
        "roles": None,
    },

    "fuel": {
        "name": "Eko Oil & Gas",
        "channel": "eko-oil-and-gas",
        "zone": "mainland",
        "roles": None,
    },

    "mall": {
        "name": "Eko Mall",
        "channel": "eko-mall",
        "zone": "mainland",
        "roles": None,
    },

    "depot": {
        "name": "Depot",
        "channel": "depot",
        "zone": "mainland",
        "roles": [
            "Supplier",
        ],
    },

    "dealership": {
        "name": "Vehicle Dealership",
        "channel": "dealership",
        "zone": "mainland",
        "roles": None,
    },

    "taxi": {
        "name": "Taxi Company",
        "channel": "taxi-company",
        "zone": "mainland",
        "roles": None,
    },

    "repair": {
        "name": "Automobile Repair",
        "channel": "auto-repair",
        "zone": "mainland",
        "roles": None,
    },

    "agency": {
        "name": "Travel Agency",
        "channel": "travel-agency",
        "zone": "mainland",
        "roles": None,
    },


    # ========================================================
    # GHETTO
    #
    # IMPORTANT:
    # EVERYTHING UNDER GHETTO IS FREE.
    #
    # No resident role is required for:
    # - Ghetto
    # - Makoko
    # - Ajegunle
    # - Face Me I Face You
    # ========================================================

    "ghetto": {
        "name": "Ghetto",
        "channel": "ghetto",
        "zone": "ghetto",
        "roles": None,
    },

    "makoko": {
        "name": "Makoko",
        "channel": "makoko",
        "zone": "ghetto",
        "roles": None,
    },

    "ajegunle": {
        "name": "Ajegunle",
        "channel": "ajegunle",
        "zone": "ghetto",
        "roles": None,
    },

    "tenement": {
        "name": "Face Me I Face You",
        "channel": "face-me-i-face-you",
        "zone": "ghetto",
        "roles": None,
    },


    # ========================================================
    # FARMLAND
    #
    # RESTRICTED.
    #
    # ONLY Streethustler can access this location.
    # ========================================================

    "farmland": {
        "name": "Farmland",
        "channel": "farmland",
        "zone": "farmland",
        "roles": [
            "Streethustler",
        ],
    },


    # ========================================================
    # OVERSEAS
    #
    # These are NOT road destinations.
    # ========================================================

    "dubai": {
        "name": "Dubai",
        "channel": "dubai",
        "zone": "overseas",
        "roles": [
            "On vacation",
        ],
    },

    "maldives": {
        "name": "Maldives",
        "channel": "maldives",
        "zone": "overseas",
        "roles": [
            "On vacation",
        ],
    },
}


# ============================================================
# ZONE LABELS
# ============================================================

ZONE_LABELS = {
    "island": "Island",
    "mainland": "Mainland",
    "ghetto": "Ghetto",
    "farmland": "Farmland",
    "overseas": "Overseas",
}


# ============================================================
# ROAD DESTINATIONS
# ============================================================

ROAD_DESTINATIONS = {
    code
    for code in LOCATIONS
    if code not in OVERSEAS
}


# ============================================================
# ROAD DISTANCES
# ============================================================

RAW_DISTANCES = {

    # --------------------------------------------------------
    # ISLAND
    # --------------------------------------------------------

    ("island", "lekki"): 5,
    ("island", "ikoyi"): 4,
    ("island", "eko-atlantic"): 7,
    ("island", "mayor-villa"): 3,
    ("island", "deputy-villa"): 3,
    ("island", "guesthouse1"): 3,
    ("island", "guesthouse2"): 3.5,
    ("island", "cos"): 2,
    ("island", "council"): 2,
    ("island", "justice"): 2,
    ("island", "housing"): 2,
    ("island", "agriculture"): 2.5,
    ("island", "clerk"): 2.5,
    ("island", "bank"): 2,
    ("island", "hospital"): 3,
    ("island", "police"): 2.5,
    ("island", "rental"): 2,
    ("island", "university"): 4,
    ("island", "lobby"): 1,
    ("island", "clubhouse"): 2,
    ("island", "chapel"): 2,


    # --------------------------------------------------------
    # MAINLAND
    # --------------------------------------------------------

    ("mainland", "ikeja"): 10,
    ("mainland", "yaba"): 6,
    ("mainland", "surulere"): 8,
    ("mainland", "immigration"): 3,
    ("mainland", "market"): 4,
    ("mainland", "restaurant"): 4,
    ("mainland", "fuel"): 5,
    ("mainland", "mall"): 5,
    ("mainland", "depot"): 6,
    ("mainland", "dealership"): 5,
    ("mainland", "taxi"): 4,
    ("mainland", "repair"): 5,
    ("mainland", "agency"): 3,


    # --------------------------------------------------------
    # GHETTO
    # --------------------------------------------------------

    ("ghetto", "makoko"): 4,
    ("ghetto", "ajegunle"): 8,
    ("ghetto", "tenement"): 5,


    # --------------------------------------------------------
    # FARMLAND
    # --------------------------------------------------------

    ("mainland", "farmland"): 15,
    ("ghetto", "farmland"): 10,
    ("island", "farmland"): 22,


    # --------------------------------------------------------
    # INTER-ZONE ROUTES
    # --------------------------------------------------------

    ("island", "mainland"): 12,
    ("island", "ghetto"): 20,
    ("mainland", "ghetto"): 12,
}


# ============================================================
# TRAVEL TIMING
# ============================================================

MIN_TRAVEL_TIME_SECONDS = 10

MAX_TRAVEL_TIME_SECONDS = 90

TRAVEL_SECONDS_PER_KM = 3.0

TRAVEL_MESSAGE_DELETE_DELAY_SECONDS = 15


# ============================================================
# VEHICLE CONDITION / REPAIR
# ============================================================

CONDITION_LOSS_PER_KM = 0.5

REPAIR_COST_PER_POINT = 5_000

REPAIR_CODE = "repair"

MECHANIC_ROLE = "Mechanic"


# ============================================================
# VEHICLES
# ============================================================

VEHICLES = {

    "Toyota Camry": {
        "price": 8_000_000,
        "quantity": 20,
        "role": "Toyota Camry",
        "fuel_capacity": 60,
        "fuel_consumption": 0.12,
        "condition": 100,
        "passenger_capacity": 2,
    },

    "Lexus": {
        "price": 15_000_000,
        "quantity": 10,
        "role": "Lexus",
        "fuel_capacity": 70,
        "fuel_consumption": 0.14,
        "condition": 100,
        "passenger_capacity": 3,
    },
}


# ============================================================
# CARPOOL / RIDESHARE (private vehicle multi-passenger trips)
# ============================================================
#
# Lets a private vehicle owner queue up to their vehicle's
# passenger_capacity worth of passengers, each with their own
# drop-off destination, before starting the trip with !drive.
#
# See cogs/carpool.py for the full flow.
# ============================================================

# How long a passenger has to !accept a drop-off request
# before it automatically expires.
CARPOOL_CONFIRM_TIMEOUT_SECONDS = 60

# Default passenger capacity for any vehicle that doesn't
# explicitly set "passenger_capacity" above.
CARPOOL_DEFAULT_PASSENGER_CAPACITY = 1


# ============================================================
# BRT / PUBLIC BUS SYSTEM
# ============================================================
#
# BRT is completely separate from private vehicle travel.
#
# Private vehicles:
#     travel.py
#
# Public buses:
#     brt.py
#     cogs/bus.py
#
# BRT passengers:
#     - do not pay road tolls
#     - do not use vehicle fuel
#     - do not control the bus
#     - use a BRT Card
#     - are served on a first-come-first-served basis
#     - choose an exact destination using the existing
#       location code names
#
# Maximum passengers per bus = 10.
# ============================================================

BRT_CAPACITY = 10


# ============================================================
# BRT ROLES
# ============================================================

# Role required to purchase/add public buses to the fleet.
BRT_OPERATOR_ROLE = "Mayor of Eko"

# Role given to a player after purchasing a BRT Card.
BRT_CARD_ROLE = "BRT Card"


# ============================================================
# BRT CARD
# ============================================================

# Starting price of a BRT Card.
#
# This is intentionally separate from the player's normal bank
# balance. The BRT Card is its own stored balance.
BRT_CARD_PRICE = 0

# Minimum amount that can be added during a recharge.
BRT_MIN_RECHARGE = 1_000

# Maximum amount allowed in a single recharge.
BRT_MAX_RECHARGE = 1_000_000

# Maximum balance a BRT Card can hold.
BRT_CARD_MAX_BALANCE = 5_000_000


# ============================================================
# BRT FARES
# ============================================================
#
# Fares are based on the number of road kilometres travelled.
#
# The bus does NOT charge tolls.
#
# The passenger's BRT Card is charged only after the passenger
# successfully boards the bus.
#
# These are the default fare settings. The BRT engine will use
# the actual shortest road distance between the passenger's
# current location and destination.
# ============================================================

BRT_FARE_PER_KM = 100

BRT_MIN_FARE = 100

BRT_MAX_FARE = 5_000


# ============================================================
# BRT MESSAGE CLEANUP
# ============================================================
#
# BRT buses will generate many messages while moving.
# Messages therefore disappear automatically so location
# channels do not become flooded.
# ============================================================

BRT_MESSAGE_DELETE_DELAY_SECONDS = 10


# ============================================================
# BRT TIMING
# ============================================================
#
# Buses are autonomous.
#
# No player controls the driver.
#
# A bus moves according to its configured route and schedule.
# ============================================================

BRT_MIN_TRAVEL_TIME_SECONDS = 10

BRT_MAX_TRAVEL_TIME_SECONDS = 90

BRT_SECONDS_PER_KM = 3.0


# ------------------------------------------------------------
# BRT ROUTE ONE-WAY TRAVEL TIME
#
# Fixed one-way trip length for each route, from its starting
# zone to the end/last location of its final zone. This is the
# total time a bus takes to drive every stop on the outbound
# leg once (the return leg takes the same total time, mirrored).
# This total is split evenly across every stop-to-stop hop on
# that route, overriding the distance-based calculation above
# for these three routes.
#
#   B1: Farmland  -> end/last location of Mainland = 1.2 min
#   B3: Farmland  -> end/last location of Island   = 2.0 min
#   B2: Mainland  -> end/last location of Island    = 1.5 min
# ------------------------------------------------------------

BRT_ROUTE_ONE_WAY_SECONDS = {
    "B1": 1.2 * 60,
    "B3": 2.0 * 60,
    "B2": 1.5 * 60,
}


# ============================================================
# BRT ROUTES
# ============================================================
#
# There are three public BRT routes.
#
# B1:
#     Ghetto ↔ Mainland
#
# B2:
#     Mainland ↔ Island
#
# B3:
#     Ghetto ↔ Island
#
# IMPORTANT:
#
# These route names describe the zones served by the bus.
#
# Passengers do NOT simply travel from one zone hub to another.
#
# A passenger registers the exact destination code.
#
# Example:
#
#     !bus B1 mall
#
# If the player is currently at Makoko:
#
#     Makoko → Ghetto → Mainland → Mall
#
# The bus can therefore drop the passenger at `mall`.
#
# The BRT system will use the existing routing system to verify
# that the requested destination is actually reachable.
#
# No tolls are charged to BRT passengers.
# ============================================================

BRT_ROUTES = {

    "B1": {
        "name": "B1 — Ghetto ↔ Mainland",
        "code": "B1",
        "zones": (
            "ghetto",
            "mainland",
        ),
        "start_zone": "ghetto",
        "end_zone": "mainland",
    },

    "B2": {
        "name": "B2 — Mainland ↔ Island",
        "code": "B2",
        "zones": (
            "mainland",
            "island",
        ),
        "start_zone": "mainland",
        "end_zone": "island",
    },

    "B3": {
        "name": "B3 — Ghetto ↔ Island",
        "code": "B3",
        "zones": (
            "ghetto",
            "island",
        ),
        "start_zone": "ghetto",
        "end_zone": "island",
    },
}


# ============================================================
# BRT ROUTE DIRECTIONS
# ============================================================
#
# Buses operate in both directions.
#
# B1:
#     Ghetto → Mainland
#     Mainland → Ghetto
#
# B2:
#     Mainland → Island
#     Island → Mainland
#
# B3:
#     Ghetto → Island
#     Island → Ghetto
#
# The actual individual stops are determined by the BRT engine
# from the existing LOCATIONS and ROAD_DISTANCES.
# ============================================================

BRT_ROUTE_DIRECTIONS = {

    "B1": [
        ("ghetto", "mainland"),
        ("mainland", "ghetto"),
    ],

    "B2": [
        ("mainland", "island"),
        ("island", "mainland"),
    ],

    "B3": [
        ("ghetto", "island"),
        ("island", "ghetto"),
    ],
}


# ============================================================
# BRT BUS FLEET
# ============================================================
#
# Buses cost ₦0 for now.
#
# Only the player with the "Mayor of Eko" role can purchase
# buses.
#
# Multiple buses can exist at the same time.
#
# Example:
#
#     !busbuy 2
#
# The Mayor of Eko can purchase two buses.
# ============================================================

BRT_BUS_PURCHASE_PRICE = 0

BRT_DEFAULT_BUS_COUNT = 0


# ============================================================
# BRT SCHEDULE
# ============================================================
#
# The buses are autonomous.
#
# A bus should not wait indefinitely for passengers.
#
# Buses operate on a repeating schedule. The BRT engine will
# use these intervals to determine when buses depart from their
# route starting points.
#
# The values are in seconds.
# ============================================================

BRT_DEPARTURE_INTERVAL_SECONDS = 120

BRT_BOARDING_WINDOW_SECONDS = 20


# ============================================================
# BRT QUEUE
# ============================================================
#
# Passengers are handled strictly first-come-first-served.
#
# Maximum passengers on one bus:
#
#     10
#
# If more than 10 people are waiting:
#
#     first 10 eligible passengers board
#
# remaining passengers stay in the queue for the next bus.
#
# A passenger with insufficient BRT Card funds is NOT added to
# the active passenger list and does not block other passengers.
# ============================================================

BRT_QUEUE_MAX_DISPLAY = 10


# ============================================================
# BRT BUS STOP RULE
# ============================================================
#
# There is NO separate "Bus Station" channel.
#
# Existing location channels are used as bus stops.
#
# Therefore:
#
#     Makoko       → bus stop
#     Mall         → bus stop
#     Bank         → bus stop
#     Lobby        → bus stop
#     etc.
#
# The BRT system uses the existing LOCATIONS dictionary.
# ============================================================

BRT_USE_EXISTING_LOCATION_CHANNELS = True


# ============================================================
# BRT ROAD / ACCESS RULES
# ============================================================
#
# BRT uses the existing road network for route validation.
#
# Therefore:
#
#     Restricted location
#         → access denied
#
#     Dubai / Maldives
#         → not a road route
#
#     Invalid route for selected B1/B2/B3
#         → rejected
#
#     Valid road destination
#         → passenger may register
#
# Toll gates are deliberately ignored by BRT.
# ============================================================

BRT_USE_ROAD_ROUTING = True

BRT_CHARGE_TOLLS = False

BRT_ALLOW_OVERSEAS = False

BRT_CHECK_LOCATION_ACCESS = True


# ============================================================
# TAXI SYSTEM
# ============================================================
#
# A taxi ride always has exactly ONE destination shared by the
# driver and every rider — no multi-stop reordering like carpool.
#
# FLOW:
#
#   1. Player: !becometaxidriver standard | premium
#      -> only works with TAXI_ELIGIBLE_ROLE, at
#         TAXI_REGISTRATION_CODE. Removes that role, grants the
#         matching TAXI_DRIVER_ROLES entry, registers them in
#         the database at that tier.
#
#   2. Driver: !taxistart / !taxistop
#      -> toggles visibility to !book.
#
#   3. Rider: !book standard | premium <destination>
#      -> pings the nearest online, idle driver of that tier.
#
#   4. Booker (only): !addrider <@user>
#      -> up to TAXI_MAX_RIDERS total, all going to the SAME
#         destination as the original booking. No individual
#         confirmation needed from added riders — only the
#         driver has to accept.
#
#   5. Driver: !taxiaccept / !taxidecline
#      -> expires automatically after
#         TAXI_REQUEST_TIMEOUT_SECONDS.
#
#   6. Driver: !drive <destination>
#      -> travel.py picks up the confirmed ride exactly like it
#         does carpool passengers, and drives it using the
#         SAME toll/fuel/condition logic as a normal private
#         trip. Fare is charged to the booker, and the taxi
#         company's cut is deducted from the driver's payout,
#         on arrival.
# ============================================================

# Discord role a player must hold to register as a taxi driver.
# Rename this to match whatever "jobless"/starter role already
# exists on your server.
TAXI_ELIGIBLE_ROLE = "Jobless"

# Role granted on registration — one shared role for every taxi
# driver regardless of tier (tier is still tracked in the
# database and shown in bot messages).
TAXI_DRIVER_ROLE = "Taxi Driver"

# Where a player registers as a taxi driver. Reuses the Vehicle
# Dealership channel/location so no new road node is needed. If
# you'd rather have a dedicated Taxi HQ, add it to LOCATIONS and
# RAW_DISTANCES first (same as any other drivable location),
# then point this at its code.
TAXI_REGISTRATION_CODE = "dealership"

# Maximum riders per taxi trip (booker + added riders), all
# sharing the same destination.
TAXI_MAX_RIDERS = 3

# ₦ per km, before the tier multiplier below.
TAXI_BASE_FARE_PER_KM = 100

# Multiplies the base per-km rate above. Also more expensive
# than BRT_FARE_PER_KM on its own, as intended.
TAXI_TIER_MULTIPLIER = {
    "standard": 1.5,
    "premium": 2.5,
}

# Percentage of every completed fare kept by the taxi company,
# deducted from the driver's payout (not added on top of the
# rider's fare).
TAXI_COMPANY_CUT = {
    "standard": 0.15,
    "premium": 0.25,
}

# How long a driver has to !taxiaccept / !taxidecline a ride
# ping before it auto-expires and the booker is notified.
TAXI_REQUEST_TIMEOUT_SECONDS = 60

# How long system messages (ping, accept/decline confirmations)
# stick around before auto-deleting, same pattern used by
# carpool/travel.
TAXI_MESSAGE_DELETE_DELAY_SECONDS = 15
