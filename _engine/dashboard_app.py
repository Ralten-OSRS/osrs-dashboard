#!/usr/bin/env python3
"""
OSRS Dashboard - Clan Edition launcher.

Double-click the app. It finds your RuneLite characters, asks which one to
track, then builds and serves your interactive dashboard in your browser.

There are no config files to edit: the character's screenshot folder name is
your OSRS name, which is also what the hiscores lookup uses. Your dashboard
(and its XP history) are saved right next to your screenshots, so everything
stays with your account data.
"""
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

# Make the engine + drop tables importable whether we're running from source
# (python dashboard_app.py) or frozen into an .exe by PyInstaller. PyInstaller
# unpacks bundled modules to sys._MEIPASS at runtime.
_HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Version and GitHub coordinates live in version.py so the generator can stamp
# the same numbers into the dashboard and into in-app issue reports.
from version import APP_VERSION, RELEASES_LATEST_API, RELEASES_PAGE  # noqa: E402


def _version_tuple(text):
    """Turn a tag like 'v1.2.3' into (1, 2, 3). Unparseable pieces become 0."""
    pieces = []
    for part in str(text or "").strip().lstrip("vV").split(".")[:4]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        pieces.append(int(digits) if digits else 0)
    return tuple(pieces) if pieces else (0,)


def check_for_update(timeout=2.5):
    """Print a notice if a newer release exists on GitHub.

    This only ever reads and prints. Every failure - offline, rate limited,
    GitHub changing its response, a proxy in the way - is swallowed on
    purpose. An update notice is a courtesy, and it must never be the reason
    someone cannot build their dashboard.
    """
    try:
        request = Request(
            RELEASES_LATEST_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"osrs-dashboard/{APP_VERSION}",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            latest = json.loads(response.read().decode("utf-8")).get("tag_name")
        if not latest or _version_tuple(latest) <= _version_tuple(APP_VERSION):
            return
        print("-" * 58)
        print(f"  An update is available: {latest}   (you have v{APP_VERSION})")
        print(f"  Download it here: {RELEASES_PAGE}")
        print("  Skipping it is fine. Your dashboard keeps working either way.")
        print("-" * 58)
        print()
    except Exception:  # noqa: BLE001 - a failed check has to stay invisible
        pass


def banner():
    print("=" * 58)
    print("            OSRS DASHBOARD  -  Clan Edition")
    print("=" * 58)
    print()


def find_characters():
    """Find RuneLite screenshot character folders.

    Returns (base_folder, [(name, path, has_shots), ...]). RuneLite creates one
    subfolder per character under ~/.runelite/screenshots/.
    """
    base = Path.home() / ".runelite" / "screenshots"
    if not base.exists():
        return base, []
    chars = []
    for p in sorted(base.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        # Does this character folder contain at least one screenshot? Stop at
        # the first hit so huge folders stay fast.
        has_shots = next(p.rglob("*.png"), None) is not None
        chars.append((p.name, p, has_shots))
    return base, chars


def choose_character():
    """Walk the user through picking a character. Returns (name, Path) or (None, None)."""
    base, chars = find_characters()
    real = [(name, path) for (name, path, has_shots) in chars if has_shots]

    if real:
        if len(real) == 1:
            name, path = real[0]
            print(f"Found one character: {name}")
            print(f"  {path}")
            ans = input("\nUse this one? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes"):
                return name, path
        else:
            print("Found these RuneLite characters:\n")
            for i, (name, _path) in enumerate(real, 1):
                print(f"  {i}. {name}")
            print()
            while True:
                ans = input(f"Which character? [1-{len(real)}]: ").strip()
                if ans.isdigit() and 1 <= int(ans) <= len(real):
                    return real[int(ans) - 1]
                print("  Please type one of the numbers listed above.")

    # Fallback: nothing auto-detected, or the user declined the single match.
    print()
    if not base.exists():
        print("Couldn't find RuneLite's screenshots folder in the usual place:")
        print(f"  {base}")
    else:
        print("No character screenshot folders were found automatically.")
    print("\nYou can point me straight at your character's screenshot folder.")
    print("(In RuneLite, the screenshot location is shown in the Screenshot")
    print(" plugin settings if you've changed it from the default.)")
    while True:
        raw = input("\nPaste the full folder path (or press Enter to quit): ").strip().strip('"')
        if not raw:
            return None, None
        path = Path(raw)
        if path.is_dir():
            return path.name, path
        print("  That folder doesn't exist - double-check the path and try again.")


def ask_boss_data_refresh():
    """Offer a wiki refresh of boss drop tables before building.

    Default is no. The fast path is what almost everyone wants: bosses this
    app has never seen are looked up automatically anyway. The slow path only
    matters when an existing boss's drop table has changed since this build.
    """
    print()
    print("-" * 58)
    print("  Boss data")
    print()
    print("  This app ships with drop tables from the day it was built. Any")
    print("  boss it hasn't seen before gets looked up automatically as you")
    print("  play, so you do not need this often.")
    print()
    print("  Refreshing re-reads the wiki for every boss you have kills on.")
    print("  It takes a few minutes and is worth doing occasionally, or after")
    print("  a game update changed drops you care about.")
    print("-" * 58)
    try:
        answer = input("\nRefresh boss data first? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def run():
    banner()
    check_for_update()
    print("Looking for your RuneLite screenshots...\n")
    name, path = choose_character()
    if not path:
        print("\nNothing selected - no dashboard built. You can run this again anytime.")
        return

    refresh_boss_data = ask_boss_data_refresh()

    print(f"\nBuilding the dashboard for {name}...")
    print("(First run also starts your XP history; pace tracking fills in as")
    print(" you refresh on future days.)\n")

    import osrs_dashboard as eng
    eng.FORCE_BOSS_DATA_REFRESH = refresh_boss_data
    eng.PLAYER_NAME = name
    eng.SCREENSHOTS_PATH = str(path)
    eng.OUTPUT_FILE = str(path / "osrs_dashboard.html")
    eng.XP_HISTORY_FILE = str(path / "xp_history.json")
    eng.FAVORITES_FILE = str(path / "favorites.json")
    eng.KNOWN_BOSSES_FILE = str(path / "known_bosses.json")
    eng.ECONOMIC_EVENTS_FILE = str(path / "economic_events.json")
    eng.VALUE_PRICE_CACHE_FILE = str(path / "value_price_cache.json")
    eng.WIKI_DISCOVERY_CATALOG_FILE = str(path / "wiki_discovery_catalog.json")
    # Hard-reset every personalization knob to neutral. Engine defaults are
    # already neutral, but if a stray config.py ever gets bundled into the
    # exe (or found on the user's machine), this guarantees no one inherits
    # someone else's manual settings or attested loot.
    eng.ACTIVE_SKILLS = []
    eng.LUCK_OWNED_OVERRIDES = {}
    eng.VALUE_COMPONENT_OVERRIDES = {}
    from dashboard_server import serve_dashboard
    serve_dashboard(eng, open_browser="--no-open" not in sys.argv)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001 - top-level guard so the window never just vanishes
        import traceback
        print("\n" + "=" * 58)
        print("Something went wrong while building the dashboard:")
        print(f"  {exc}")
        print("-" * 58)
        traceback.print_exc()
        print("=" * 58)
        print("If this keeps happening, open an issue with this whole window:")
        print("  https://github.com/Ralten-OSRS/osrs-dashboard/issues")
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass
