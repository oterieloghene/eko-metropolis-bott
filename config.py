"""
Eko Metropolis — central configuration.
"""

# ============================================================
# GENERAL
# ============================================================

STARTING_BALANCE = 0

# New registered players now start at the "immigration" location
# (its channel is #help-desk), not the dealership.
STARTING_LOCATION = "immigration"


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
# FLIGHTS
# ============================================================
#
# Round-trip price = price_one_way * 2.
#
#   Maldives -> N2,000,000 each way  = N4,000,000 round trip
#   Dubai    -> N1,500,000 each way  = N3,000,000 round trip
#
# TEST-RUN TIMING:
# These are short (seconds) so the whole flow — book, check in,
# fly, arrive, vacation, return — can be tested quickly. Swap
# these for real day-based scheduling later.
# ============================================================

FLIGHT_DESTINATIONS = {
    "maldives": {
        "price_one_way": 2_000_000,
    },
    "dubai": {
        "price_one_way": 1_500_000,
    },
}

FLIGHT_VACATION_ROLE = "On vacation"

FLIGHT_AGENCY_LOCATION = "agency"

# Every !bookflight request now sits in a "pending_approval"
# state until a person with this role approves or denies it —
# see cogs/flight.py. No charge is taken and no deadline exists
# until that happens.
IMMIGRATION_OFFICER_ROLE = "Immigration Officer"

# Where pending travel requests are posted for officers to
# review (existing "immigration" LOCATIONS entry -> #help-desk,
# also the server's default starting location).
FLIGHT_IMMIGRATION_LOCATION = "immigration"

# How long after booking the player has to check in at the
# travel agency before they are considered "late".
FLIGHT_CHECKIN_WINDOW_SECONDS = 300

# Extra time granted after the FIRST missed check-in.
# Missing a SECOND time forfeits the ticket (no refund, no
# further reschedule).
FLIGHT_RESCHEDULE_WINDOW_SECONDS = 300

# How long the flight itself takes once checked in, before the
# player actually arrives at the destination.
FLIGHT_DURATION_SECONDS = 60

# How long a player can choose to stay on vacation.
FLIGHT_MIN_STAY_SECONDS = 120
FLIGHT_MAX_STAY_SECONDS = 1800

# How often the background task scans for missed check-ins /
# due arrivals / due returns.
FLIGHT_SCAN_INTERVAL_SECONDS = 15

# How long before a vacation actually ends that the player gets a
# heads-up warning in the destination channel. The return itself
# still happens automatically at return_at — this just makes sure
# it isn't a silent surprise.
FLIGHT_RETURN_REMINDER_SECONDS = 30


# ============================================================
# HOTELS (Dubai / Maldives room booking)
# ============================================================
#
# Only bookable while the player's DB location is already an
# overseas destination (i.e. mid-vacation via flight.py).
#
# Price scales linearly with the player's REMAINING vacation
# time (not the original full stay), between the same
# FLIGHT_MIN_STAY_SECONDS/FLIGHT_MAX_STAY_SECONDS bounds flights
# use, so a room booked partway through a trip only charges for
# the time actually used.
#
#   Standard: N100,000 (2 min remaining) -> N1,000,000 (30 min)
#   Luxury:   1.5x the Standard price at the same remaining time
# ============================================================

HOTEL_ROOMS_PER_TIER = 2  # per destination, per tier (Standard / Luxury)

HOTEL_STANDARD_MIN_PRICE = 100_000
HOTEL_STANDARD_MAX_PRICE = 1_000_000
HOTEL_LUXURY_MULTIPLIER = 1.5

HOTEL_GUEST_RESPONSE_TIMEOUT_SECONDS = 120

# Room service delivery points, as a fraction of the room's
# stay length: check-in, 1/3 through, 2/3 through.
HOTEL_ROOM_SERVICE_FRACTIONS = [0.0, 1 / 3, 2 / 3]

HOTEL_DISHES = [
    "Continental Breakfast Platter (croissants, eggs, fresh fruit)",
    "Grilled Salmon & Herb Rice",
    "Beef Wellington & Roasted Vegetables",
]

# How often the background task checks for due room-service
# deliveries. Mirrors FLIGHT_SCAN_INTERVAL_SECONDS.
HOTEL_SCAN_INTERVAL_SECONDS = 15


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

    "driving-school": {
        "name": "Driving School",
        "channel": "driving-school",
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
    ("mainland", "driving-school"): 4,


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
# MECHANIC DISPATCH (book-a-mechanic, mirrors the taxi flow)
# ============================================================

MECHANIC_REQUEST_TIMEOUT_SECONDS = 60

MECHANIC_MESSAGE_DELETE_DELAY_SECONDS = 15


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

    # ------------------------------------------------------------
    # DISPATCH COMPANY VEHICLES — not for sale at the dealership
    # (handed out free via !becomedispatchrider, same as the taxi
    # company cars above). fuel_capacity/fuel_consumption are
    # unused for the Bicycle — see DISPATCH_FUEL_EXEMPT_VEHICLES
    # and DISPATCH_TOLL_EXEMPT_VEHICLES in the DISPATCH JOB block.
    # ------------------------------------------------------------

    "Bicycle": {
        "price": None,
        "quantity": None,
        "role": None,
        "fuel_capacity": 0,
        "fuel_consumption": 0,
        "condition": 100,
        "passenger_capacity": 0,
    },

    "Motorcycle": {
        "price": None,
        "quantity": None,
        "role": None,
        "fuel_capacity": 40,
        "fuel_consumption": 0.10,
        "condition": 100,
        "passenger_capacity": 0,
    },

    # ------------------------------------------------------------
    # POLICE PATROL CAR — not for sale at the dealership. Bought
    # in bulk by the Mayor (!purchasepd), then assigned to
    # individual officers by the Chief/Deputy Chief of Police
    # (!assignpd). See the POLICE SYSTEM block below and
    # cogs/police.py.
    # ------------------------------------------------------------

    "Police Patrol Car": {
        "price": None,
        "quantity": None,
        "role": None,
        "fuel_capacity": 60,
        "fuel_consumption": 0.12,
        "condition": 100,
        "passenger_capacity": 1,
    },

    # ------------------------------------------------------------
    # AMBULANCE — not for sale at the dealership. Bought in bulk
    # by the Mayor (!purchaseambulance), then assigned to
    # individual Medic Staff by the Chief/Deputy Chief Medical
    # Officer (!assignambulance). See the AMBULANCE SYSTEM block
    # below and cogs/ambulance.py. passenger_capacity here is
    # crew only (1 ride-along medic) — patient slots are tracked
    # separately (AMBULANCE_PATIENT_CAPACITY), since an
    # unconscious patient isn't a normal "passenger".
    # ------------------------------------------------------------

    "Ambulance": {
        "price": None,
        "quantity": None,
        "role": None,
        "fuel_capacity": 60,
        "fuel_consumption": 0.12,
        "condition": 100,
        "passenger_capacity": 1,
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
#         the database at that tier, and immediately hands them
#         a company-owned car (TAXI_COMPANY_VEHICLE) — it works
#         exactly like a dealership vehicle (!vehicle, !map,
#         fuel, condition, !fixcar all treat it identically).
#
#   2. Driver: !taxistart / !taxistop
#      -> toggles visibility to !book. Stays in effect until the
#         driver explicitly toggles it again — it is NOT reset
#         after each trip.
#
#   3. Rider: !book standard | premium <destination>
#      -> BROADCASTS the request to EVERY online, idle driver of
#         that tier — each gets pinged in their OWN current
#         location channel, not the booker's. Whichever driver
#         runs !taxiaccept first gets the ride; the ping is
#         pulled from every other notified driver's channel. If
#         nobody qualifies, the booker is placed in a FIFO queue
#         for that tier (TAXI_QUEUE_TIMEOUT_SECONDS wait limit)
#         and is auto-matched (broadcast again) the moment a
#         driver of that tier comes online, finishes a trip,
#         declines, or times out.
#
#   4. Booker (only): !addrider <@user>
#      -> up to TAXI_MAX_RIDERS total, all going to the SAME
#         destination as the original booking. Works whether the
#         booking is queued or already sent to a driver. No
#         individual confirmation needed from added riders —
#         only the driver has to accept.
#
#   5. Driver: !taxiaccept / !taxidecline
#      -> can be typed from wherever the driver actually is; the
#         ping itself lives in their current-location channel.
#         The whole request expires automatically after
#         TAXI_REQUEST_TIMEOUT_SECONDS if nobody accepts. Every
#         notified driver declining, or the timeout hitting,
#         both trigger an automatic re-broadcast/re-queue, same
#         as if nobody had been found in the first place.
#
#   6. Driver: !drive <destination>
#      -> travel.py picks up the confirmed ride exactly like it
#         does carpool passengers, and drives it using the
#         SAME toll/fuel/condition logic as a normal private
#         trip. Fare is charged to the booker, and the taxi
#         company's cut is deducted from the driver's payout,
#         on arrival.
#
# MESSAGE CLEANUP: every taxi message auto-deletes after
# TAXI_MESSAGE_DELETE_DELAY_SECONDS, EXCEPT the live ride-request
# ping sitting in the booker's channel awaiting the driver's
# !taxiaccept / !taxidecline — that one stays up until the driver
# actually responds (or it times out), then is deleted right away.
# ============================================================

# Discord role a player must hold to register as a taxi driver.
# Rename this to match whatever "jobless"/starter role already
# exists on your server.
TAXI_ELIGIBLE_ROLE = "Jobless"

# Role granted on registration — one shared role for every taxi
# driver regardless of tier (tier is still tracked in the
# database and shown in bot messages).
TAXI_DRIVER_ROLE = "Taxi Driver"

# Where a player registers as a taxi driver — the dedicated Taxi
# Company location (#taxi-company), not the Vehicle Dealership.
TAXI_REGISTRATION_CODE = "taxi"

# Maximum riders per taxi trip (booker + added riders), all
# sharing the same destination.
TAXI_MAX_RIDERS = 3

# ₦ per km, before the tier multiplier below.
TAXI_BASE_FARE_PER_KM = 100

# Multiplies the base per-km rate above. Also more expensive
# than BRT_FARE_PER_KM on its own, as intended.
#
# FARE INCREASE: standard is now the old standard rate × 2.5
# (1.5 × 2.5 = 3.75), and premium is standard × 1.68 (3.75 ×
# 1.68 = 6.3) — replacing the old flat 2.5 premium multiplier.
TAXI_TIER_MULTIPLIER = {
    "standard": 3.75,
    "premium": 6.3,
}

# Flat floor under every fare, regardless of distance — keeps a
# short taxi hop from ever undercutting the bus (BRT_MIN_FARE is
# ₦100). Deliberately well above that: a taxi is a private,
# on-demand, door-to-door ride and should always cost more than
# public transit.
#
# Scaled by the same 2.5× / 1.68× factors as the tier
# multipliers above, so a short hop's floor stays consistent
# with the new per-km rates.
TAXI_MIN_FARE = {
    "standard": 1250,
    "premium": 2100,
}

# Percentage of every completed fare kept by the taxi company,
# deducted from the driver's payout (not added on top of the
# rider's fare). The company owns the car, so it takes the
# majority share.
TAXI_COMPANY_CUT = {
    "standard": 0.50,
    "premium": 0.60,
}

# Which VEHICLES entry a driver is handed, for free, the moment
# they register with !becometaxidriver <tier>. This REPLACES
# whatever is in their "vehicle" slot — it's a company car, not
# a purchase, so dealership stock is untouched.
TAXI_COMPANY_VEHICLE = {
    "standard": "Toyota Camry",
    "premium": "Lexus",
}

# How long a driver has to !taxiaccept / !taxidecline a ride
# ping before it auto-expires and the booker is notified.
TAXI_REQUEST_TIMEOUT_SECONDS = 60

# How long a booker waits in the queue for a driver to become
# available before the request is auto-cancelled.
TAXI_QUEUE_TIMEOUT_SECONDS = 300

# Extra seconds added on top of the calculated drive time when
# quoting how long a driver will take to reach the pickup point
# — a small buffer so the estimate doesn't read as a hard
# deadline and the rider isn't left waiting past what they were
# told.
TAXI_PICKUP_BUFFER_SECONDS = 20

# How long system messages (ping, accept/decline confirmations)
# stick around before auto-deleting, same pattern used by
# carpool/travel.
TAXI_MESSAGE_DELETE_DELAY_SECONDS = 15

# Role allowed to retrieve a commercial vehicle (taxi OR
# dispatch bicycle/motorcycle) back from a player — e.g. the
# player quit the job, went inactive, or the company needs the
# car back. See cogs/dispatch.py's !retrievevehicle.
COMMERCIAL_VEHICLE_MANAGER_ROLE = "Taxi Company Manager"


# ============================================================
# DISPATCH JOB (delivery riders — bicycle/motorcycle)
# ============================================================
#
# Registered the same way as a taxi driver, at the same Taxi
# Company location (TAXI_REGISTRATION_CODE above), but with its
# own role/table/queue-matching engine — see cogs/dispatch.py,
# modeled closely on cogs/taxi.py.
#
# "standard" tier = bicycle: no fuel, no toll fee, but takes
#                   longer than a private car to complete a trip.
# "premium"  tier = motorcycle: uses fuel, pays toll, and needs
#                   repair like any other motor vehicle.
# ============================================================

# A player needs ONE of these roles to register with
# !becomedispatchrider — additive job, doesn't replace/remove
# whichever of these roles they already hold.
DISPATCH_ELIGIBLE_ROLES = [
    "Campus Resident",
    "Student",
]

# Role granted on registration — one shared role for every
# dispatch rider regardless of tier, kept ON TOP of whichever
# DISPATCH_ELIGIBLE_ROLES role got them in (never removed).
DISPATCH_RIDER_ROLE = "Dispatch Rider"

# Which VEHICLES entry a rider is handed, for free, the moment
# they register with !becomedispatchrider <tier>. Same pattern
# as TAXI_COMPANY_VEHICLE — a company vehicle, not a purchase.
DISPATCH_COMPANY_VEHICLE = {
    "standard": "Bicycle",
    "premium": "Motorcycle",
}

# Bicycles never need fuel/repair/toll — checked by name in
# travel.py-facing helpers wherever a vehicle's tier decides
# whether fuel/toll apply.
DISPATCH_FUEL_EXEMPT_VEHICLES = {"Bicycle"}
DISPATCH_TOLL_EXEMPT_VEHICLES = {"Bicycle"}

# Maximum delivery orders a rider can be actively carrying (i.e.
# accepted but not yet dropped off) at once.
DISPATCH_MAX_ORDERS = 3

# Dispatch fares intentionally use the OLD, pre-2.5x taxi rate —
# NOT the current TAXI_BASE_FARE_PER_KM/TAXI_TIER_MULTIPLIER
# above. Deliveries are cheaper cargo runs, not passenger fares.
DISPATCH_BASE_FARE_PER_KM = 100
DISPATCH_TIER_MULTIPLIER = {
    "standard": 1.5,
    "premium": 2.5,
}
DISPATCH_MIN_FARE = {
    "standard": 500,
    "premium": 800,
}

# Split of every completed delivery fare — same 25/75 pattern as
# MECHANIC_CUT in repair.py (the rider keeps 25%, the remaining
# 75% goes to the taxi company and simply vanishes, same as the
# auto shop's cut — no company ledger for either).
DISPATCH_RIDER_CUT = 0.25

# A bicycle leg takes noticeably longer than a private car over
# the same distance; multiplies the normal travel-time formula.
DISPATCH_BICYCLE_TIME_MULTIPLIER = 1.6

# How long a rider has to !dispatchaccept / !dispatchdecline an
# order ping before it auto-expires.
DISPATCH_REQUEST_TIMEOUT_SECONDS = 60

# How long an order waits in the queue for a rider to become
# free before it's auto-cancelled.
DISPATCH_QUEUE_TIMEOUT_SECONDS = 300

# Same auto-delete pattern as TAXI_MESSAGE_DELETE_DELAY_SECONDS.
DISPATCH_MESSAGE_DELETE_DELAY_SECONDS = 15


# ============================================================
# POLICE SYSTEM (patrol cars + checkpoints)
# ============================================================
#
# See cogs/police.py.
#
# FLEET MODEL (mirrors dispatch's commercial-vehicle pattern,
# NOT the autonomous bus fleet in cogs/bus.py):
#
#   1. Mayor buys N patrol cars with !purchasepd <qty>. They sit
#      in an UNASSIGNED POOL (police_fleet.json — same on-disk
#      JSON pattern as bus_fleet.json) until assigned.
#   2. Chief/Deputy Chief hand one to a specific Officer with
#      !assignpd <@officer>, which pulls a car out of the pool
#      and adds it to that officer's owned-vehicle list via
#      database.add_vehicle(..., vehicle_type="police") — same
#      call dispatch.py uses for bicycles/motorcycles.
#   3. !retrievepd <@officer> reverses that — database.
#      remove_vehicle() takes it off the officer, and it goes
#      back into the unassigned pool.
#
# This is deliberately NOT built on cogs/bus.py's Bus dataclass/
# route-loop — patrol cars are player-driven (via !patrol),
# not autonomously simulated like buses.
# ============================================================

OFFICER_ROLE = "Officer"
POLICE_CHIEF_ROLE = "Chief Police Officer"
POLICE_DEPUTY_CHIEF_ROLE = "Deputy Chief of Police"

# Roles allowed to !assignpd / !retrievepd a patrol car
# to/from an officer.
POLICE_ASSIGNMENT_ROLES = [
    POLICE_CHIEF_ROLE,
    POLICE_DEPUTY_CHIEF_ROLE,
]

# Role allowed to !purchasepd — same role that gates
# !purchasebus in cogs/bus.py.
POLICE_PURCHASE_ROLE = "Mayor of Eko"

# The existing "Eko Police Department" LOCATIONS entry (see
# LOCATIONS["police"] above) — !purchasepd/!assignpd/!retrievepd
# must all be used from its channel.
POLICE_STATION_LOCATION = "police"

# The one patrol-car vehicle type. Defined in VEHICLES below so
# !vehicle/!refuel/!fixcar all read it like any other vehicle.
# passenger_capacity 2 = driver + 1 ride-along officer, same key
# carpool.py reads for private cars.
POLICE_PATROL_VEHICLE = "Police Patrol Car"

# On-disk pool of purchased-but-unassigned patrol cars. Same
# format/purpose as cogs/bus.py's bus_fleet.json.
POLICE_FLEET_FILE = "police_fleet.json"

# Same auto-delete pattern as TAXI_MESSAGE_DELETE_DELAY_SECONDS.
POLICE_MESSAGE_DELETE_DELAY_SECONDS = 15

# ------------------------------------------------------------
# CHECKPOINTS (wired up in a follow-up pass, alongside the
# !patrol <-> travel.py journey-pause integration). Defined
# here now so both pieces share the same constants.
# ------------------------------------------------------------

# !checkpoints can only be mounted on these major-route zones,
# and only where a police car is physically present. Subset of
# ZONE_HUBS above — farmland is deliberately excluded.
CHECKPOINT_ELIGIBLE_ZONES = {
    "island",
    "mainland",
    "ghetto",
}

# Vehicle types a mounted checkpoint pauses. Private cars (type
# "personal"), taxis, and dispatch motorcycles (premium tier) —
# NOT dispatch bicycles (standard tier), which are exempt.
CHECKPOINT_AFFECTED_VEHICLE_TYPES = {
    "personal",
    "taxi",
    "dispatch_motorcycle",
}


# ============================================================
# OVERSEAS AREAS (Dubai / Maldives sub-locations)
# ============================================================
#
# Each area is a private Discord THREAD inside the destination's
# main channel (dubai/maldives) — not a channel of its own. A
# player is only ever a member of the thread for their CURRENT
# area; !goto removes them from the old one and adds them to the
# new one. Threads are never deleted, only archived when they
# become empty, so history is preserved for reuse.
#
# kind:
#   "shop"  -> !mall / !fastfood / !spa work here
#   "event" -> !compete / !try work here
#
# guide:
#   The name used by the "tour guide" arrival message in
#   cogs/areas.py — a local name matching the area's country.
# ============================================================

AREAS = {
    "downtown-dubai": {
        "name": "Downtown Dubai",
        "country": "dubai",
        "kind": "shop",
        "emoji": "\U0001f3d9\ufe0f",  # 🏙️
        "guide": "Rashid",
    },
    "dubai-desert": {
        "name": "Dubai Desert",
        "country": "dubai",
        "kind": "event",
        "emoji": "\U0001f3dc\ufe0f",  # 🏜️
        "guide": "Khalid",
    },
    "dubai-marina": {
        "name": "Dubai Marina",
        "country": "dubai",
        "kind": "event",
        "emoji": "\U0001f6a4",  # 🚤
        "guide": "Tariq",
    },
    "paradise-resort": {
        "name": "Paradise Resort",
        "country": "maldives",
        "kind": "shop",
        "emoji": "\U0001f334",  # 🌴
        "guide": "Ibrahim",
    },
    "blue-lagoon": {
        "name": "Blue Lagoon",
        "country": "maldives",
        "kind": "event",
        "emoji": "\U0001f30a",  # 🌊
        "guide": "Shahid",
    },
    "ocean-excursion": {
        "name": "Ocean Excursion",
        "country": "maldives",
        "kind": "event",
        "emoji": "\U0001f41f",  # 🐟
        "guide": "Naail",
    },
}

# Areas grouped by country, in menu-display order.
AREAS_BY_COUNTRY = {
    "dubai": [code for code, a in AREAS.items() if a["country"] == "dubai"],
    "maldives": [code for code, a in AREAS.items() if a["country"] == "maldives"],
}

# Local wallet currency used by each country's areas.
COUNTRY_CURRENCY = {
    "dubai": "aed",
    "maldives": "mvr",
}

CURRENCY_SYMBOL = {
    "aed": "AED",
    "mvr": "MVR",
}


# ============================================================
# SHOPS — Downtown Dubai / Paradise Resort
# ============================================================
#
# !mall prices are in the area's local currency (AED or MVR).
# ============================================================

MALL_ITEMS = {
    "downtown-dubai": [
        {"name": "Luxury Watch", "price": 450},
        {"name": "Designer Handbag", "price": 320},
        {"name": "Gold Cufflinks", "price": 180},
        {"name": "Perfume Gift Set", "price": 95},
        {"name": "Silk Scarf", "price": 60},
    ],
    "paradise-resort": [
        {"name": "Handwoven Sarong", "price": 900},
        {"name": "Black Pearl Necklace", "price": 3200},
        {"name": "Coconut Wood Carving", "price": 650},
        {"name": "Reef-Safe Sunscreen Set", "price": 280},
        {"name": "Straw Beach Hat", "price": 150},
    ],
}

FASTFOOD_MENU = {
    "downtown-dubai": [
        {"name": "Shawarma Wrap", "price": 25},
        {"name": "Falafel Plate", "price": 20},
        {"name": "Grilled Kebab Platter", "price": 45},
        {"name": "Mango Lassi", "price": 15},
    ],
    "paradise-resort": [
        {"name": "Grilled Reef Fish", "price": 220},
        {"name": "Coconut Rice Bowl", "price": 150},
        {"name": "Tropical Fruit Platter", "price": 120},
        {"name": "Fresh Coconut Water", "price": 60},
    ],
}

SPA_SERVICES = {
    "downtown-dubai": [
        {"name": "Desert Rose Massage", "price": 300},
        {"name": "Gold Facial Treatment", "price": 450},
        {"name": "Hot Stone Therapy", "price": 380},
    ],
    "paradise-resort": [
        {"name": "Overwater Bungalow Massage", "price": 1800},
        {"name": "Coconut Body Scrub", "price": 1200},
        {"name": "Sunset Couples Spa", "price": 2600},
    ],
}


# ============================================================
# EVENTS — Dubai Desert / Dubai Marina / Blue Lagoon /
# Ocean Excursion
# ============================================================
#
# Each event area has exactly two activities:
#
#   compete -> !compete <event>
#       Pay entry_fee (local currency) to join a pool. A
#       registration window opens on the first entrant; the
#       window closes EVENT_REGISTRATION_WINDOW_SECONDS after
#       it opened. On close, every entrant is rolled against
#       the event's metric and the best result wins the whole
#       pool. Fewer than 2 entrants when the window closes ->
#       round is cancelled and everyone is refunded.
#
#   try -> !try <event>
#       Free. Instant flavor outcome, no payout.
#
# metric fields (non-fishing events):
#   metric_name / unit -> just for display
#   min / max          -> range each entrant's roll is drawn from
#   higher_wins         -> True if the highest roll wins,
#                          False if the lowest roll wins
#
# Deep Sea Fishing Challenge is special-cased ("fishing": True)
# — each entrant is rolled a random species + weight instead of
# a plain number, and the heaviest catch wins.
# ============================================================

AREA_EVENTS = {
    "dubai-desert": {
        "compete": {
            "code": "desert_rally",
            "name": "Desert Rally",
            "entry_fee": 40,
            "metric_name": "finish time",
            "unit": "s",
            "min": 45,
            "max": 120,
            "higher_wins": False,
        },
        "try": {
            "code": "sandboarding",
            "name": "Sandboarding",
            "flavors": [
                "\U0001fa82 You carve down a dune in a cloud of sand — smooth landing!",
                "\U0001fa82 You wipe out halfway down and end up with sand in places sand should never be.",
                "\U0001fa82 A perfect run! A nearby guide gives you a thumbs up.",
                "\U0001fa82 You board down slower than expected but enjoy the view of the dunes.",
            ],
        },
    },
    "dubai-marina": {
        "compete": {
            "code": "speedboat_race",
            "name": "Speedboat Race",
            "entry_fee": 60,
            "metric_name": "finish time",
            "unit": "s",
            "min": 30,
            "max": 90,
            "higher_wins": False,
        },
        "try": {
            "code": "sunset_yacht_cruise",
            "name": "Sunset Yacht Cruise",
            "flavors": [
                "\U0001f6e5\ufe0f The marina skyline glows gold as the sun sets over the water.",
                "\U0001f6e5\ufe0f You spot dolphins trailing the yacht's wake.",
                "\U0001f6e5\ufe0f Champagne on the deck, skyline views — pure luxury.",
                "\U0001f6e5\ufe0f A light breeze, calm water, and a perfect sunset. Ten out of ten.",
            ],
        },
    },
    "blue-lagoon": {
        "compete": {
            "code": "kayak_race",
            "name": "Kayak Race",
            "entry_fee": 500,
            "metric_name": "finish time",
            "unit": "s",
            "min": 60,
            "max": 150,
            "higher_wins": False,
        },
        "try": {
            "code": "snorkeling_tour",
            "name": "Snorkeling Tour",
            "flavors": [
                "\U0001f930 You drift over a coral garden bursting with color.",
                "\U0001f930 A sea turtle glides right past your mask.",
                "\U0001f930 The visibility is incredible today — you can see forever.",
                "\U0001f930 A curious reef shark circles at a distance, then swims off.",
            ],
        },
    },
    "ocean-excursion": {
        "compete": {
            "code": "deep_sea_fishing",
            "name": "Deep Sea Fishing Challenge",
            "entry_fee": 700,
            "fishing": True,
            "species": [
                {"name": "Yellowfin Tuna", "min_weight": 15, "max_weight": 80},
                {"name": "Sailfish", "min_weight": 20, "max_weight": 60},
                {"name": "Marlin", "min_weight": 50, "max_weight": 300},
                {"name": "Grouper", "min_weight": 5, "max_weight": 40},
                {"name": "Mahi-Mahi", "min_weight": 8, "max_weight": 35},
            ],
        },
        "try": {
            "code": "sunset_cruise",
            "name": "Sunset Cruise",
            "flavors": [
                "\U0001f6a4 The boat glides through calm water as the sky turns orange.",
                "\U0001f6a4 Flying fish leap alongside the boat for a moment.",
                "\U0001f6a4 A pod of dolphins escorts the boat for a stretch.",
                "\U0001f6a4 The crew hands out fresh coconuts as the sun dips below the horizon.",
            ],
        },
    },
}

# How long a competitive pool stays open for entrants once the
# first person joins.
EVENT_REGISTRATION_WINDOW_SECONDS = 60

# How often the background task checks for pools whose
# registration window has closed.
EVENT_SCAN_INTERVAL_SECONDS = 10


# ============================================================
# WALLET — foreign currency (AED / MVR) + fluctuating exchange
# rate
# ============================================================
#
# Rate is expressed as "how many Naira (₦) one unit of the
# foreign currency costs" — e.g. an AED rate of 1375 means
# ₦1,375 buys 1 AED.
#
# A background loop (see cogs/wallet.py) nudges each rate by a
# small random percentage every tick, clamped inside its band so
# it can't drift out of control. Nobody — including admins — can
# set a rate directly; !exchange always reads whatever the loop
# last calculated.
# ============================================================

EXCHANGE_STARTING_RATE = {
    "aed": 1375,
    "mvr": 100,
}

EXCHANGE_RATE_BOUNDS = {
    "aed": (1200, 1550),
    "mvr": (90, 115),
}

# Maximum absolute percentage a rate can move in a single tick
# (e.g. 0.03 = up to +/-3%).
EXCHANGE_MAX_TICK_PERCENT = 0.03

# How often the background loop nudges the rates.
EXCHANGE_TICK_INTERVAL_SECONDS = 30


# ============================================================
# GOOGLE MAP APP (see cogs/map.py)
# ============================================================
#
# Purely presentational — reads LOCATIONS/ZONE_LABELS the same
# way !location and driving_school.py's tutorial text already
# do. No DB writes; the player's current DB location is only
# read so it can be pinned in the listing.
# ============================================================

# The reference art shown by !map, alongside every zone-grouped
# location code. Lives in cogs/assets like phone_home.png.
MAP_IMAGE_FILENAME = "eko_metropolis_map_corrected.webp"

MAP_COLOR_RGB = (61, 220, 132)  # matches PHONE_COLOR (Android green)

# Zones shown, in display order. Mirrors the order location.py's
# ZONE_LABELS/driving_school.py's tutorial listing already use.
MAP_ZONE_ORDER = [
    "island",
    "mainland",
    "ghetto",
    "farmland",
    "overseas",
]


# ============================================================
# EMERGENCY SYSTEM (see cogs/emergency.py)
# ============================================================
#
# !emergency police / !emergency hospital (also reachable from
# the phone's Emergency button — Call Police / Call Ambulance)
# posts a role-ping alert into the existing "police"/"hospital"
# LOCATIONS channels (precint-reception / hospital-lobby),
# naming the caller's current location so responders know where
# to go. Whoever holds the matching role can Accept it from a
# button on the alert itself.
#
# The alert is live for EMERGENCY_RESPONSE_WINDOW_SECONDS. If
# nobody accepts in that window, it deletes itself and the
# button stops working (the message is simply gone, so there's
# nothing left to click). If someone accepts, the alert is
# resolved immediately and nobody else can accept it afterward.
# ============================================================

# Existing "Officer" role (see OFFICER_ROLE above) doubles as
# the police responder role — reused here, not redefined.

# New role for hospital-side responders. Create this Discord
# role to match (same as OFFICER_ROLE/MECHANIC_ROLE above).
MEDIC_ROLE = "Medic Staff"

# Which LOCATIONS entry each emergency service alert is posted
# to. Both already exist (see LOCATIONS["police"] /
# LOCATIONS["hospital"] above) — no new channels needed.
EMERGENCY_SERVICES = {
    "police": {
        "location": "police",
        "role": OFFICER_ROLE,
        "label": "Police",
    },
    "hospital": {
        "location": "hospital",
        "role": MEDIC_ROLE,
        "label": "Ambulance",
    },
}

# How long an alert stays open for a response before it
# auto-deletes and can no longer be accepted.
EMERGENCY_RESPONSE_WINDOW_SECONDS = 120

# Same auto-delete pattern as POLICE_MESSAGE_DELETE_DELAY_SECONDS
# — used for the caller's own ephemeral-style confirmations.
EMERGENCY_MESSAGE_DELETE_DELAY_SECONDS = 15


# ============================================================
# AIRTIME METERING (see cogs/contacts.py)
# ============================================================
#
# players.airtime_balance is a separate ₦ pool from
# players.balance (same idea as aed_balance/mvr_balance above)
# — !airtime <amount> converts bank cash into airtime credit,
# and every !text/!reply/connected !call meters against THAT
# balance, never the bank balance directly.
#
# Texts are charged a flat fee the moment they're delivered.
# Calls are only metered once CONNECTED (accepted) — ringing,
# missed, or declined calls are always free, same as real life —
# and are billed to the CALLER, per tick, for as long as the
# call stays up. A call that runs out of airtime is ended
# automatically, same as !endcall, with both sides notified.
# ============================================================

AIRTIME_TEXT_COST = 200

AIRTIME_CALL_COST_PER_TICK = 300
AIRTIME_METER_INTERVAL_SECONDS = 30

# Shown to the player whenever a text/call is blocked for
# insufficient airtime.
AIRTIME_INSUFFICIENT_MESSAGE = (
    "\u26d4 Not enough airtime. Use `!airtime <amount>` to top up."
)


# ============================================================
# PLAYER STATS (see cogs/stats.py)
# ============================================================
#
# Six bars, all 0-100, all starting full at 100 for a brand new
# player (see database.create_player).
#
#   hunger      -> drained by walking distance (cogs/walk.py).
#   thirst      -> drained by walking distance, fastest of the
#                  "survival" bars.
#   health      -> NOT drained directly by walking/weather. Only
#                  drops as a consequence of hunger, thirst, or
#                  happiness sitting at 0 for a while (see
#                  cogs/stats.py's background decay loop).
#   hygiene     -> drained by walking distance, and worsened by
#                  hot/sunny weather.
#   breath      -> mouth/oral hygiene (breath odor), NOT a
#                  lung/exertion mechanic. Decays passively and
#                  slowly regardless of activity.
#   happiness   -> drained slowly by walking; drains faster the
#                  lower the other survival bars already are.
#
# COLLAPSE RULE:
#   If hunger, thirst, health, OR happiness hits 0, the player
#   collapses (see cogs/walk.py / cogs/stats.py). hygiene and
#   breath never trigger a collapse on their own — they're
#   cosmetic/social bars, not survival bars.
# ============================================================

PLAYER_STAT_NAMES = [
    "hunger",
    "thirst",
    "health",
    "hygiene",
    "breath",
    "happiness",
]

# Bars that can trigger a collapse when they hit 0.
COLLAPSE_STATS = [
    "hunger",
    "thirst",
    "health",
    "happiness",
]

STAT_MAX = 100.0
STAT_MIN = 0.0

STAT_STARTING_VALUE = 100.0

# Display label + emoji for each stat, used by !stats.
STAT_DISPLAY = {
    "hunger":    {"label": "Hunger",    "emoji": "\U0001F356"},
    "thirst":    {"label": "Thirst",    "emoji": "\U0001F4A7"},
    "health":    {"label": "Health",    "emoji": "\u2764\ufe0f"},
    "hygiene":   {"label": "Hygiene",   "emoji": "\U0001F9FC"},
    "breath":    {"label": "Breath",    "emoji": "\U0001F9B7"},
    "happiness": {"label": "Happiness", "emoji": "\U0001F60A"},
}

# How many blocks wide each !stats bar is drawn.
STAT_BAR_LENGTH = 10

# ------------------------------------------------------------
# WALKING DECAY RATES
#
# Points lost PER KILOMETRE walked (see cogs/walk.py). Distance-
# driven, not time-driven — a longer road segment always costs
# more than a short one, independent of how travel time gets
# clamped.
#
# Relative ordering intentionally mirrors real life:
#   thirst drains fastest, then hygiene, then hunger, then
#   happiness drains slowest of all (small passive tax for the
#   effort of walking, on top of whatever COMPOUND_UNHAPPINESS
#   adds below). health and breath are NOT drained by walking at
#   all.
# ------------------------------------------------------------

WALK_STAT_DECAY_PER_KM = {
    "thirst": 2.5,
    "hygiene": 1.6,
    "hunger": 1.2,
    "happiness": 0.4,
}

# If True, happiness loses a little extra per km walked whenever
# hunger or thirst is already below COMPOUND_UNHAPPINESS_THRESHOLD
# — being hungry/thirsty AND walking is more miserable than
# walking alone. Applied on top of the base happiness rate above.
COMPOUND_UNHAPPINESS_ENABLED = True
COMPOUND_UNHAPPINESS_THRESHOLD = 30.0
COMPOUND_UNHAPPINESS_EXTRA_PER_KM = 0.3

# ------------------------------------------------------------
# BACKGROUND STAT DECAY (idle drain, not tied to walking)
#
# A slow passive tick applied to every registered player on an
# interval, so hunger/thirst/hygiene/breath/happiness fall even
# while a player isn't actively walking. health is excluded here
# too — it only reacts to the rule below.
# ------------------------------------------------------------

STAT_DECAY_TICK_SECONDS = 300

STAT_DECAY_PER_TICK = {
    "hunger": 1.0,
    "thirst": 1.4,
    "hygiene": 0.5,
    "breath": 0.3,
    "happiness": 0.2,
}

# ------------------------------------------------------------
# HEALTH FALLOUT
#
# health is never drained directly. Instead, every tick, if
# hunger, thirst, or happiness is AT ZERO, health takes damage.
# Multiple zeroed stats stack (e.g. zero hunger AND zero thirst
# damages health twice as fast).
# ------------------------------------------------------------

HEALTH_LOSS_PER_ZEROED_STAT_PER_TICK = 3.0

# ------------------------------------------------------------
# LOW-STAT WARNINGS
#
# !stats and the passive decay loop warn a player once a
# survival stat crosses this threshold, so they get a heads up
# before they actually collapse.
# ------------------------------------------------------------

STAT_WARNING_THRESHOLD = 20.0


# ============================================================
# WALKING (!walk / !trek — see cogs/walk.py)
# ============================================================
#
# Reuses the road GRAPH from routing.py (same graph !drive and
# the bus system use), stepping through it one node/channel at a
# time — same underlying mechanic as cogs/bus.py's stop-by-stop
# progression — but with far quieter messaging (see below) and
# no toll/checkpoint stops, since a pedestrian doesn't pay road
# tolls or get pulled over the way a vehicle does.
#
# MESSAGING (per journey):
#   1. Departure  -> posted once in the origin channel, player
#                    TAGGED.
#   2. Every       -> posted in that channel, name only, NO
#      intermediate    mention, short-lived (auto-deletes).
#      hop
#   3. Collapse    -> posted in whichever channel the player is
#      (if any)       passing through at that moment, player
#                      TAGGED.
#   4. Arrival     -> posted once in the destination channel,
#                      player TAGGED.
#
# Write access only ever moves TWICE for a walk — revoked at the
# origin when the walk starts, granted at the destination (or
# frozen entirely, at the collapse point, if they collapse) —
# exactly the same pattern cogs/bus.py uses for a passenger
# (never touched at intermediate stops, only origin -> final).
# ============================================================

WALK_MIN_TRAVEL_TIME_SECONDS = 15

WALK_MAX_TRAVEL_TIME_SECONDS = 180

# Deliberately much slower than driving (TRAVEL_SECONDS_PER_KM =
# 3.0) or the bus (BRT_SECONDS_PER_KM = 3.0) — walking a km takes
# real time.
WALK_SECONDS_PER_KM = 25.0

# How long the "{name} walks past." intermediate-hop message
# stays up before auto-deleting. Short — it's flavor, not an
# announcement anyone needs to act on.
WALK_MESSAGE_DELETE_DELAY_SECONDS = 8


# ============================================================
# COLLAPSE / UNCONSCIOUS (see cogs/walk.py)
# ============================================================
#
# Triggered whenever hunger, thirst, health, OR happiness hits 0
# (see config.COLLAPSE_STATS above) while walking. hygiene and
# breath never trigger a collapse on their own.
#
# FLOW:
#   1. The player is given the Unconscious role, their database
#      location is frozen at wherever they collapsed, and
#      players.unconscious is set to 1.
#   2. While unconscious, a global bot-wide check (see bot.py)
#      blocks EVERY command from that player, including phone
#      commands — they simply can't act at all.
#   3. Someone else must !emergency hospital (see cogs/
#      emergency.py — already exists, unchanged) to alert Medic
#      Staff, OR a Medic can just walk/drive there directly.
#   4. A Medic Staff member standing in the SAME channel as the
#      collapsed player can !resuscitate <@player> to revive them
#      on the spot, OR load them into an ambulance (see cogs/
#      ambulance.py) and drive them to the hospital, where
#      they're automatically treated on arrival.
#   5. TIMEOUT: if nobody resuscitates/transports them within
#      COLLAPSE_TIMEOUT_SECONDS, they're auto-teleported to the
#      hospital lobby and treated there automatically.
#
# Recovery never restores a stat to full (100) — just enough to
# be back on their feet — so collapsing still has a lasting cost
# even after being saved.
# ============================================================

UNCONSCIOUS_ROLE = "Unconscious"

COLLAPSE_TIMEOUT_SECONDS = 300

# What each of the four collapse-triggering stats is restored to
# on resuscitation/hospital treatment. Applied only to whichever
# of them are still below this value — a stat that never dropped
# this low (e.g. happiness was fine, only hunger hit 0) is left
# alone.
COLLAPSE_RECOVERY_STAT_VALUE = 35.0

# Roles that can !resuscitate a collapsed player on the spot.
RESUSCITATE_ROLE = "Medic Staff"


# ============================================================
# AMBULANCE SYSTEM (see cogs/ambulance.py)
# ============================================================
#
# Mirrors the police patrol-car fleet system exactly (purchase ->
# unassigned pool -> assign/retrieve -> !ambulance is the
# medic's !patrol equivalent) — see cogs/police.py's module
# docstring for the fleet model this copies.
#
# CAPACITY (unlike a patrol car's driver + 1):
#   - 2 crew seats total: 1 driver + 1 optional ride-along medic.
#   - 2 separate patient slots for unconscious players being
#     transported. A patient can't act for themselves, so a crew
#     member has to !loadpatient them in at the current location
#     before departing.
#
# On arrival at the hospital, every patient onboard is
# automatically treated (same recovery as !resuscitate) and
# dropped off there — no separate !resuscitate needed once they
# reach the hospital.
# ============================================================

AMBULANCE_CHIEF_ROLE = "Chief Medical Officer"
AMBULANCE_DEPUTY_CHIEF_ROLE = "Deputy Chief Medical Officer"

# Roles allowed to !assignambulance / !retrieveambulance an
# ambulance to/from a medic. New roles — create them to match
# (same as POLICE_CHIEF_ROLE/POLICE_DEPUTY_CHIEF_ROLE).
AMBULANCE_ASSIGNMENT_ROLES = [
    AMBULANCE_CHIEF_ROLE,
    AMBULANCE_DEPUTY_CHIEF_ROLE,
]

# Role allowed to !purchaseambulance — same role that gates
# !purchasepd / !purchasebus.
AMBULANCE_PURCHASE_ROLE = "Mayor of Eko"

# The existing "hospital" LOCATIONS entry (channel
# hospital-lobby) doubles as the ambulance depot — no new
# channel needed.
AMBULANCE_STATION_LOCATION = "hospital"

# The one ambulance vehicle type. Defined in VEHICLES below so
# !vehicle/!refuel/!fixcar all read it like any other vehicle.
AMBULANCE_VEHICLE = "Ambulance"

# Maximum unconscious patients an ambulance can carry at once.
AMBULANCE_PATIENT_CAPACITY = 2

# On-disk pool of purchased-but-unassigned ambulances. Same
# format/purpose as police_fleet.json / bus_fleet.json.
AMBULANCE_FLEET_FILE = "ambulance_fleet.json"

# Same auto-delete pattern as POLICE_MESSAGE_DELETE_DELAY_SECONDS.
AMBULANCE_MESSAGE_DELETE_DELAY_SECONDS = 15
