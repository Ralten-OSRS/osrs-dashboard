# OSRS RuneLite Dashboard — Read Me First

A private local dashboard that scans your RuneLite screenshots folder and
visualizes your OSRS progression: stats, boss drops, wealth progression, loot
log, hall of fame, favorite memories, and a browsable gallery.

The dashboard reads from your hiscores (live) and the screenshots RuneLite
saves to disk. Nothing is uploaded anywhere. The launcher runs a small local
service so the browser can save favorites and refresh; it closes automatically
a few seconds after the last dashboard tab closes.

## What to click, and how often

This folder uses a numbered convention. The number tells you the order to
think about each file, not necessarily a strict frequency:

| File | When to use |
| :---- | :---- |
| **`1 - Refresh Dashboard.bat`** | Start the dashboard. Double-click once. The page's Refresh button rescans screenshots, pulls live hiscores, logs the daily XP/KC snapshot, resolves completed untradeable assemblies, and auto-discovers new bosses plus clear new assembly recipes. The launcher window closes itself after the dashboard tab closes. |
| **`2 - Update Drop Tables.bat`** | Occasionally — once after first setup, then after notable OSRS updates (or monthly). Pulls fresh wiki data for every boss: drop tables, drop rates, boss categories, and the Combat Achievement task list. ~1 minute. |
| **`3 - Audit Shared Drops.bat`** | Diagnostic only. Items on 4+ drop tables are auto-classified as shared now, so this is just for the rare judgment call — run it if you ever spot a screenshot on the wrong boss tile. |

If you only ever run #1, you'll be fine — new bosses even show up automatically.
Run #2 now and then to keep the wiki-sourced data (drops, rates, categories,
combat achievements) current. Ordinary refreshes now also perform targeted wiki
lookups for newly seen bosses and save any unambiguous untradeable assembly
recipe into the account-local catalog.

## First-time setup (you only do this once)

Requirements:
- Python 3.8+ installed (Windows). If you don't have it, grab it from python.org and check "Add Python to PATH" during install.
- RuneLite with screenshot saving enabled (default settings work).
- At least one screenshot already saved in your RuneLite folder.

Steps:
1. Drop this folder anywhere on your computer.
2. Copy `config.example.py` and rename the copy to `config.py`.
3. Open `config.py` and set `PLAYER_NAME` to your in-game character name (case-sensitive — has to match the subfolder under `~/.runelite/screenshots/`).
4. Double-click `1 - Refresh Dashboard.bat`. It opens the dashboard in your default browser. The launcher window stays available while you use Refresh or Favorites, then closes itself after the dashboard tab closes.
5. (Optional) Right-click that .bat and "Send to → Desktop (create shortcut)" so you can refresh in one click from anywhere.

That's it.

## What's in this folder

| Item | What it is |
| :---- | :---- |
| `0 - READ ME FIRST.md` | This file. Open it when you've forgotten how this works. |
| `1 - Refresh Dashboard.bat` | Starts the private local dashboard service and opens the page. |
| `2 - Update Drop Tables.bat` | Pulls fresh drop data from the OSRS Wiki. |
| `3 - Audit Shared Drops.bat` | Flags multi-boss items that need categorization. |
| `config.example.py` | Settings template. Copy to `config.py` and edit. |
| `_engine/` | Python code that runs everything. You don't need to touch it. |
| `_Archive/` | Old versions of the script. Leave alone. |
| `OSRS Dashboard Resources/` | Pinned Chart.js runtime and dashboard fonts embedded into every generated HTML file for fully offline viewing. |
| `AGENTS.md`, `MEMORY.md`, `DESIGN.md` | AI-assistant docs. Not needed if you're not using an AI assistant. |

## Where the dashboard saves to

`~/.runelite/screenshots/<PLAYER_NAME>/osrs_dashboard.html`

The account folder also holds `xp_history.json`, `known_bosses.json`,
`favorites.json`, and three small wealth files: `economic_events.json` (frozen
completed assembly values), `value_price_cache.json` (the latest public price
lookup), and `wiki_discovery_catalog.json` (auto-learned boss references and
unambiguous recipes). Launch through the `.bat`; opening the HTML directly still
shows the dashboard, but Refresh and Favorites require the local service.

## Troubleshooting

**"SCREENSHOTS_PATH doesn't exist"** — your `PLAYER_NAME` in `config.py` doesn't match the folder name RuneLite uses. Open `~/.runelite/screenshots/` and check the exact spelling.

**Dashboard renders but charts are empty** — your hiscores aren't accessible. Either the player name isn't on hiscores yet, or there's a network issue. The screenshot data still works without hiscores.

**Indirect value says "Prices unavailable"** — the dashboard could not reach
the public OSRS price feed and has no saved quote yet. Your screenshot history
is safe; refresh again later and the completed-value event will resolve.

**Bosses I've killed aren't showing up** — RuneLite only saves screenshots for valuable drops, untradeables, collection log entries, and combat tasks (with the right plugins enabled). If you have RuneLite's Screenshot plugin disabled or filtered, those bosses won't appear.

**A boss tab is showing items that aren't from that boss** — Run `3 - Audit Shared Drops.bat` to flag the offender, then add it to `GLOBAL_SHARED_DROPS` or `CATEGORY_SHARED_DROPS` in `_engine/osrs_dashboard.py`.
