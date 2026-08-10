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

Your browser opens straight away. If you have more than one character with screenshots, it asks which one you want and remembers the answer, so you only choose once. Then it reads your screenshots, showing progress as it goes, and your dashboard appears when it's done. The first run on a large account takes a minute or so.

Every run after that goes straight to building. Use the **Refresh** button inside the page to rescan whenever you like, and **Settings** in the sidebar to switch characters or refresh boss data.

There's no terminal and nothing to install. The app closes itself a few seconds after you close the last dashboard tab.

Keep the file somewhere other than Downloads and you can pin it to your taskbar like any other app. When a new version comes out, save it over the copy you already have so the pin keeps pointing at the current one.

### If something goes wrong

Errors appear on the page rather than disappearing. Every run also writes `dashboard_log.txt` next to your screenshots, which is the thing to attach if you open an issue. The **Report an issue** button in the sidebar fills in your version and setup details for you.

## Staying Up To Date

The app checks for a newer release when it starts and tells you if one exists. It never downloads anything by itself, and it stays quiet when you're offline or already current.

You can also click **Watch** at the top of this page, choose **Custom**, and tick **Releases**. GitHub will email you when a new version is published and stay silent the rest of the time.

## Refreshing Boss Data

You don't normally need to. Bosses the app has never seen are looked up on the OSRS Wiki automatically as you play, so new content starts working on its own.

The manual refresh, under **Settings**, covers the narrower case where a boss you already fight has had its drop table changed since your copy was built. It re-reads the wiki for every boss you have kills on and takes a few minutes.

## Running From Source

If you'd rather run the Python directly:

1. Install Python 3.8 or newer, checking "Add Python to PATH" during setup.
2. Double-click `1 - Refresh Dashboard.bat`. It runs the same launcher the packaged app does, so it behaves the same way — it asks which character on the first run and remembers it.
3. Optionally, copy `config.example.py` to `config.py` for personal tweaks such as attesting drops you own but never screenshotted. Source runs read it; the packaged app deliberately ignores it, so nobody inherits someone else's settings.

The launcher takes `--pick` to choose a different character and `--refresh-boss-data` to re-read the wiki before building.

`_engine/` holds two maintainer scripts you will not normally need — a wiki refresh of the bundled drop tables, and a diagnostic for items that drop from several bosses. See `READ ME - maintainer tools.txt` there for when each is worth running.

All Python lives in `_engine/`. The fonts and Chart.js in `OSRS Dashboard Resources/` are embedded into the generated page so it renders with no network connection.

## What It Does With Your Files

It never edits, moves, or deletes screenshots. It reads them.

Alongside your screenshots it writes `osrs_dashboard.html`, a `dashboard_log.txt` from the most recent runs, and small JSON files holding XP history, known bosses, favorites, and local caches. Those belong to your account and stay on your machine. Which character you last used is remembered separately, under your Windows app data folder.

The only network calls are to the OSRS hiscores, the OSRS Wiki, and the Grand Exchange price feed. Your data is never sent anywhere.

## How It Works

The engine scans filenames and image content in the RuneLite screenshot folder to identify level-ups, drops, pets, quest completions, and Combat Achievements, then cross-references them against wiki-sourced drop tables and live GE prices. Newly seen bosses trigger a targeted wiki lookup, so the drop data keeps up without manual maintenance.

Wealth tracking only counts items with clear evidence and a resolvable price. Incomplete item builds are shown separately from realized value rather than folded into it.

## Troubleshooting

**Nothing appears when I run it.** Check `dashboard_log.txt` next to your screenshots. If that file doesn't exist either, the app failed before it could find your screenshot folder — open an issue and say what happened.

**Charts are empty.** Your hiscores couldn't be reached, either because the name isn't on the hiscores yet or the network is down. Everything built from screenshots still works.

**Bosses I've killed aren't showing up.** RuneLite only saves screenshots for valuable drops, untradeables, collection log entries, and combat achievements, and only with the relevant plugin settings enabled. Anything it never screenshotted can't appear.

**Wealth says prices are unavailable.** The public price feed couldn't be reached and there's no saved quote yet. Nothing is lost; refresh again later and the value resolves.

**A boss is showing items that aren't its own.** Some items drop from several bosses and can't be attributed from a filename alone. Items on four or more drop tables are grouped automatically; if one slips through, open an issue with the boss and item.

**It's building the wrong character.** Open **Settings** in the sidebar and switch. It applies the next time you start the app.

## Feedback

Bug reports and ideas are welcome through [Issues](../../issues).

## License

MIT, see [LICENSE](LICENSE). Cinzel and Crimson Text are used under the SIL Open Font License. Chart.js is bundled under the MIT License.
