# Eko Metropolis Bot

Discord RP bot implementing the location/travel/dealership system.

## How it maps to your spec

- **`config.py`** — every location code, visible name, channel name, zone,
  and restricted roles, plus vehicle inventory and toll amounts, all in one
  place (req #2, #6, #11, #25).
- **`database.py`** — SQLite table `players` is the single source of truth
  for location (req #19, #26). `reset_all_locations()` /
  `reset_all_vehicle_data()` handle existing-record resets separately from
  new-player defaults (req #15, #21).
- **`routing.py`** — builds a graph from your distance table and runs
  Dijkstra, so `!drive`/`!route` always compute the real path through zone
  checkpoints (island/mainland/ghetto/farmland) rather than a flat lookup
  (req #6, #7).
- **`checks.py`** — `require_location(code)` is the shared guard used by
  every location-sensitive command: it checks the Discord channel **and**
  the database location, and refuses if they disagree (req #4, #22, #23).
- **`permissions.py`** — grants/revokes `send_messages` on the player's
  current-location channel whenever their DB location changes. Roles stay
  responsible for *visibility*; the bot only manages *writing* (req #24).
- **`cogs/travel.py`** — `!drive` always uses `player["location"]` from the
  DB as the origin, never the channel the command was typed in (req #5,
  #17). It locks the player out of writing anywhere while travelling, pauses
  at toll checkpoints (tagging the player in the checkpoint's own channel),
  and only grants write access at the destination on arrival (req #8, #9,
  #10).
- **`cogs/dealership.py`** — `!cars`/`!buy` only work at `dealership`,
  validate DB location too, reject double-purchases and insufficient funds,
  and set vehicle/fuel/condition/role together on success (req #12–#16).
- **`cogs/admin.py`** — admin-only `!setlocation`, `!resetalllocations`,
  `!resetvehicledata` for the explicit, deliberate resets your spec calls
  for (never automatic).

## Commands

`!location` `!route <dest>` `!drive <dest>` `!paytoll` `!cars` `!buy <name>`
`!vehicle` `!refuel` `!balance`
Admin: `!setlocation @user <code>` `!resetalllocations [code]` `!resetvehicledata`

## Setup

1. Create your Discord server with a text channel for **every** `channel`
   name in `config.py` (e.g. `bank`, `dealership`, `3rd-mainland-bridge`,
   `help-desk` for Immigration, etc.) and the roles listed under
   "Restricted locations" — set those channels' visibility with normal
   Discord role permissions. The bot only manages *writing* access, not
   visibility.
2. Create a Discord application + bot at https://discord.com/developers,
   enable the **Message Content** and **Server Members** privileged
   intents, invite it with `Send Messages`, `Manage Roles`, and
   `Manage Channels`-level permissions (it needs to edit per-member channel
   overwrites).
3. Locally:
   ```
   pip install -r requirements.txt
   export DISCORD_TOKEN=your-bot-token
   python bot.py
   ```

## Deploying on Render

`render.yaml` is set up as a **worker** service (no HTTP port — this is a
gateway bot, not a web server).

1. Push this project to a GitHub repo.
2. On Render: New → Blueprint → point at the repo → it reads `render.yaml`.
3. Set the `DISCORD_TOKEN` env var in the Render dashboard (marked
   `sync: false` so it's not committed).
4. Deploy.

### ⚠️ Important: persistence

Render's free worker disks are **ephemeral** — a redeploy or restart can
wipe `ekobot.db`, resetting every player. For a live server, either:
- add a Render **Persistent Disk** and set `DB_PATH` to a path on it
  (small paid add-on), or
- swap `database.py` for Render's managed **Postgres** (the free tier
  exists) — only that one file needs rewriting; nothing else touches SQL
  directly.

## Known simplifications (flag if you want these built out further)

- Travel currently has no real-time transit duration — it resolves
  immediately once any tolls are paid. Add an `asyncio.sleep(travel_time)`
  in `cogs/travel.py` `_complete_journey` if you want drive time to matter.
- `!drive` fuel consumption is deducted on arrival; vehicle repair/condition
  degradation isn't wired up yet (`vehicle_condition` is tracked but nothing
  currently lowers it).
- The dropdown/select-menu purchase UI mentioned in your spec was **not**
  built — per your notes, `!cars` + `!buy` is the intended flow and the
  dropdown is optional/legacy.
