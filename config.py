"""
Eko Metropolis — central configuration.
"""

STARTING_BALANCE = 20_000_000
STARTING_LOCATION = "dealership"

ZONE_HUBS = {"island", "mainland", "ghetto", "farmland"}
OVERSEAS = {"dubai", "maldives"}

TOLL_ZONES = {
    "mainland": {"name": "Third Mainland Bridge", "amount": 200},
    "island": {"name": "Island Toll Gate", "amount": 200},
}

LOCATIONS = {
    "island": {"name": "Island", "channel": "island", "zone": "island", "roles": None},
    "lekki": {"name": "Lekki Phase 1", "channel": "lekki-phase-1", "zone": "island",
              "roles": ["Lekki resident", "Island visitor"]},
    "ikoyi": {"name": "Ikoyi Waterfall Villa", "channel": "ikoyi-waterfall-villa", "zone": "island",
              "roles": ["Ikoyi resident", "Island visitor"]},
    "eko-atlantic": {"name": "Eko Atlantic Penthouse", "channel": "eko-atlantic-penthouse", "zone": "island",
                     "roles": ["Eko Atlantic resident", "Island visitor"]},
    "mayor-villa": {"name": "Mayor's Penthouse", "channel": "mayors-penthouse", "zone": "island",
                    "roles": ["Presidential Villa resident", "Presidential Villa visitor"]},
    "deputy-villa": {"name": "Deputy's Residence", "channel": "deputys-residence", "zone": "island",
                     "roles": ["Presidential Villa resident", "Presidential Villa visitor"]},
    "guesthouse1": {"name": "Villa Guesthouse 1", "channel": "villa-guesthouse-1", "zone": "island",
                    "roles": ["Presidential Villa resident", "Presidential Villa visitor"]},
    "guesthouse2": {"name": "Villa Guesthouse 2", "channel": "villa-guesthouse-2", "zone": "island",
                    "roles": ["Presidential Villa resident", "Presidential Villa visitor"]},
    "cos": {"name": "Chief of Staff", "channel": "chief-of-staff", "zone": "island",
            "roles": ["Eko chiefs", "Eko deputies", "government officials"]},
    "council": {"name": "Eko Council", "channel": "eko-council", "zone": "island",
                "roles": ["Eko chiefs", "Eko deputies", "government officials"]},
    "justice": {"name": "Ministry of Justice", "channel": "minister-of-justice", "zone": "island",
                "roles": ["Eko chiefs", "Eko deputies", "government officials"]},
    "housing": {"name": "Ministry of Home Affairs & Housing", "channel": "minister-of-home-affairs-housing",
                "zone": "island", "roles": ["Eko chiefs", "Eko deputies", "government officials"]},
    "agriculture": {"name": "Ministry of Agriculture", "channel": "minister-of-agriculture", "zone": "island",
                    "roles": ["Eko chiefs", "Eko deputies", "government officials"]},
    "clerk": {"name": "Clerk Office", "channel": "clerk-office", "zone": "island", "roles": None},
    "bank": {"name": "Central Bank of Eko", "channel": "banking-hall", "zone": "island", "roles": None},
    "hospital": {"name": "Eko Medical Service", "channel": "hospital-lobby", "zone": "island", "roles": None},
    "police": {"name": "Eko Police Department", "channel": "precint-reception", "zone": "island", "roles": None},
    "rental": {"name": "Property Development Department", "channel": "rental-desk", "zone": "island", "roles": None},
    "university": {"name": "Eko Metropolis University", "channel": "vice-chancellors-office", "zone": "island", "roles": None},
    "lobby": {"name": "Eko Lobby", "channel": "eko-lobby", "zone": "island", "roles": None},
    "clubhouse": {"name": "Eko Clubhouse", "channel": "eko-clubhouse", "zone": "island", "roles": None},
    "chapel": {"name": "Eko City Chapel", "channel": "eko-city-chapel", "zone": "island", "roles": None},

    "mainland": {"name": "Third Mainland Bridge", "channel": "3rd-mainland-bridge", "zone": "mainland", "roles": None},
    "ikeja": {"name": "Ikeja Estate", "channel": "ikeja-estate", "zone": "mainland",
              "roles": ["Ikeja resident", "Mainland visitor"]},
    "yaba": {"name": "Yaba Estate", "channel": "yaba-estate", "zone": "mainland",
             "roles": ["Yaba resident", "Mainland visitor"]},
    "surulere": {"name": "Surulere Estate", "channel": "surulere-estate", "zone": "mainland",
                 "roles": ["Surulere resident", "Mainland visitor"]},
    "immigration": {"name": "Eko Immigration Office", "channel": "help-desk", "zone": "mainland", "roles": None},
    "market": {"name": "Eko Market", "channel": "eko-market", "zone": "mainland", "roles": None},
    "restaurant": {"name": "Eko Restaurant", "channel": "eko-restaurant", "zone": "mainland", "roles": None},
    "fuel": {"name": "Eko Oil & Gas", "channel": "eko-oil-and-gas", "zone": "mainland", "roles": None},
    "mall": {"name": "Eko Mall", "channel": "eko-mall", "zone": "mainland", "roles": None},
    "depot": {"name": "Depot", "channel": "depot", "zone": "mainland", "roles": ["Supplier"]},
    "dealership": {"name": "Vehicle Dealership", "channel": "dealership", "zone": "mainland", "roles": None},
    "taxi": {"name": "Taxi Company", "channel": "taxi-company", "zone": "mainland", "roles": None},
    "repair": {"name": "Automobile Repair", "channel": "auto-repair", "zone": "mainland", "roles": None},
    "agency": {"name": "Travel Agency", "channel": "travel-agency", "zone": "mainland", "roles": None},

    "ghetto": {"name": "Ghetto", "channel": "ghetto", "zone": "ghetto", "roles": None},
    "makoko": {"name": "Makoko", "channel": "makoko", "zone": "ghetto", "roles": ["Makoko resident"]},
    "ajegunle": {"name": "Ajegunle", "channel": "ajegunle", "zone": "ghetto", "roles": ["Ajegunle resident"]},
    "tenement": {"name": "Face Me I Face You", "channel": "face-me-i-face-you", "zone": "ghetto",
                 "roles": ["Tenement resident"]},

    "farmland": {"name": "Farmland", "channel": "farmland", "zone": "farmland", "roles": ["Streethustler"]},

    "dubai": {"name": "Dubai", "channel": "dubai", "zone": "overseas", "roles": ["On vacation"]},
    "maldives": {"name": "Maldives", "channel": "maldives", "zone": "overseas", "roles": ["On vacation"]},
}

ZONE_LABELS = {
    "island": "Island",
    "mainland": "Mainland",
    "ghetto": "Ghetto",
    "farmland": "Farmland",
    "overseas": "Overseas",
}

ROAD_DESTINATIONS = {
    code for code in LOCATIONS
    if code not in ZONE_HUBS and code not in OVERSEAS
}

RAW_DISTANCES = {
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

    ("ghetto", "makoko"): 4,
    ("ghetto", "ajegunle"): 8,
    ("ghetto", "tenement"): 5,

    ("mainland", "farmland"): 15,
    ("ghetto", "farmland"): 10,
    ("island", "farmland"): 22,

    ("island", "mainland"): 12,
    ("island", "ghetto"): 20,
    ("mainland", "ghetto"): 12,
}

# ============================================================
# TRAVEL TIMING
# ============================================================

# Every route takes between 10 and 90 seconds.
MIN_TRAVEL_TIME_SECONDS = 10
MAX_TRAVEL_TIME_SECONDS = 90

# The travel time is calculated from route distance.
# 0 km = 10 sec, with longer routes taking proportionally longer,
# capped at 90 seconds.
TRAVEL_SECONDS_PER_KM = 3.0

# Existing message deletion delay.
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
    },
    "Lexus": {
        "price": 15_000_000,
        "quantity": 10,
        "role": "Lexus",
        "fuel_capacity": 70,
        "fuel_consumption": 0.14,
        "condition": 100,
    },
}
