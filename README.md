# OSRS Dashboard

A private dashboard for your Old School RuneScape account, built from the screenshots RuneLite already saves.

Point it at your character and it reads years of screenshots off your own disk, pulls your live hiscores, and turns both into a browsable account history. Everything runs locally. Nothing is uploaded anywhere.

I'm not a developer by trade. This started as a way to see what my own screenshot folder actually contained, and it grew from there.

## What It Shows

Seven pages, all built from your own data:

- **Stats** is the account cover: current levels, total level, and where the account stands right now.
- **Maxing Journey** tracks progress toward 99s, with XP-per-day pacing and estimated time remaining once it has a few days of history.
- **Luck** compares your drops against published rates, linked back to the screenshot that proves each one.
- **Bosses** covers kill counts and drop tables for every boss it finds on your hiscores.
- **Loot Log** is the running record of valuable drops, with thumbnails you can open.
- **Gallery** is every screenshot, filterable, with hearts for saving Favorite Memories.
- **Chronicle** is the historical timeline, told through the screenshots themselves.

## What You Need

- Windows
- RuneLite with the Screenshot plugin enabled, which is where the level-up and drop images come from
- At least one saved screenshot

## Getting Started

Download the latest `OSRS.Dashboard.exe` from the [Releases](../../releases) page and double-click it. GitHub writes the filename with dots in place of spaces, which changes nothing about how it runs.

Windows may show a blue "Windows protected your PC" warning. Click **More info**, then **Run anyway**. That warning appears because the file isn't signed through the Microsoft Store, not because anything is wrong with it. Nothing gets installed.

A console window opens and lists the characters it found under your RuneLite screenshots folder. Type the number next to the one you want and press Enter. The dashboard builds and opens in your browser.

Use the **Refresh** button inside the page to rescan. The console window closes itself a few seconds after you close the last dashboard tab.

## Running From Source

If you'd rather run the Python directly:

1. Install Python 3.8 or newer, checking "Add Python to PATH" during setup.
2. Copy `config.example.py` to `config.py` and set `PLAYER_NAME` to your character name. It's case-sensitive and has to match the folder name under `~/.runelite/screenshots/`.
3. Double-click `1 - Refresh Dashboard.bat`.

Two other launchers are included. `2 - Update Drop Tables.bat` refreshes boss drop tables, rates, and Combat Achievement data from the OSRS Wiki, worth running after notable game updates. `3 - Audit Shared Drops.bat` is a diagnostic for items that appear on several drop tables.

All Python lives in `_engine/`. The fonts and Chart.js in `OSRS Dashboard Resources/` are embedded into the generated page so it renders with no network connection.

## What It Does With Your Files

It never edits, moves, or deletes screenshots. It reads them.

Alongside your screenshots it writes `osrs_dashboard.html` plus small JSON files holding XP history, known bosses, favorites, and local caches. Those belong to your account and stay on your machine.

The only network calls are to the OSRS hiscores, the OSRS Wiki, and the Grand Exchange price feed. Your data is never sent anywhere.

## How It Works

The engine scans filenames and image content in the RuneLite screenshot folder to identify level-ups, drops, pets, quest completions, and Combat Achievements, then cross-references them against wiki-sourced drop tables and live GE prices. Newly seen bosses trigger a targeted wiki lookup, so the drop data keeps up without manual maintenance.

Wealth tracking only counts items with clear evidence and a resolvable price. Incomplete item builds are shown separately from realized value rather than folded into it.

## Feedback

Bug reports and ideas are welcome through [Issues](../../issues).

## License

MIT, see [LICENSE](LICENSE). Cinzel and Crimson Text are used under the SIL Open Font License. Chart.js is bundled under the MIT License.
