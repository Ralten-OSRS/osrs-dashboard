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
from version import APP_VERSION, ISSUES_URL, RELEASES_LATEST_API, RELEASES_PAGE  # noqa: E402
import console  # noqa: E402
import settings  # noqa: E402

# Install at import time, not inside run(). A windowed build has no stdout, so
# anything that raises while the remaining modules are still loading would
# write its traceback to None and the process would die with nothing on screen
# and nothing on disk. Installing here means even an import-time failure is
# captured and can be reported.
console.install()


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


def resolve_character(force_pick=False):
    """Decide which character to build for, asking only when necessary.

    Picking a character is answerable from saved state after the first run, so
    asking every time is an amnesia problem rather than a UI one. We ask on the
    first run, when the remembered folder has gone away, and when the user
    explicitly asks to choose again with --pick. Otherwise we go straight to
    building. Returns (name, Path) or (None, None).
    """
    base, _chars = find_characters()

    if not force_pick:
        remembered = settings.last_account()
        if remembered:
            folders = settings.account_folders(remembered, base)
            if folders:
                name = remembered["display_name"] or remembered["primary_folder"]
                print(f"Building for {name}.")
                print("Not you? Close this and run it again with --pick.")
                return name, folders[0]
            print(f"The folder for {remembered['display_name']} is no longer there.")
            print("Let us pick again.\n")
            settings.forget_last()

    name, path = choose_character()
    if path is not None:
        settings.remember_account(path.name, display_name=name)
    return name, path


def choose_character():
    """Walk the user through picking a character. Returns (name, Path) or (None, None)."""
    base, chars = find_characters()
    if not console.can_prompt():
        # No stdin means no picker. Slice 2 moves this into the browser; until
        # then, failing loudly beats hanging on a read that can never return.
        print("This build cannot ask which character to use without a console.")
        return None, None
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


def run():
    banner()
    check_for_update()

    force_pick = "--pick" in sys.argv
    refresh_boss_data = "--refresh-boss-data" in sys.argv

    name, path = resolve_character(force_pick=force_pick)
    if not path:
        print("\nNothing selected - no dashboard built. You can run this again anytime.")
        return

    # The log lives with the account data, so it can only be opened once the
    # account is known. Everything printed before this point is already in the
    # in-memory buffer and gets written out with the rest.
    console.install(log_path=path / "dashboard_log.txt")

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
    # Hard-reset every personalization knob to neutral, so nobody inherits
    # someone else's manual settings or attested loot.
    #
    # This keys off being packaged rather than off which script was launched.
    # The exe is the only artefact that ships, and `sys.frozen` is always true
    # there, so the reset always runs for every distributed copy — the exact
    # protection DESIGN.md non-negotiable #10 asks for, tied to the condition
    # that actually matters instead of to an entry point.
    #
    # Running from source deliberately keeps `config.py`. That is what the
    # README documents config.py for, it is how the engine already behaved
    # when run directly, and it lets the maintainer use the same launcher as
    # everyone else rather than maintaining a private path that hides
    # user-facing problems.
    if getattr(sys, "frozen", False):
        eng.ACTIVE_SKILLS = []
        eng.LUCK_OWNED_OVERRIDES = {}
        eng.VALUE_COMPONENT_OVERRIDES = {}
    elif any([eng.ACTIVE_SKILLS, eng.LUCK_OWNED_OVERRIDES, eng.VALUE_COMPONENT_OVERRIDES]):
        print("Using personal settings from config.py (source run only).")
    from dashboard_server import serve_dashboard
    try:
        serve_dashboard(eng, open_browser="--no-open" not in sys.argv)
    except OSError as exc:
        # The service could not bind or could not start. This is the one
        # failure with no page to report itself on, so it gets the message box.
        log_path = path / "dashboard_log.txt"
        print(f"\nThe local service could not start: {exc}")
        console.alert(
            "OSRS Dashboard",
            "The dashboard could not start its local service.\n\n"
            f"{exc}\n\n"
            "This is usually a firewall or security tool blocking a local "
            "connection. Nothing leaves your computer either way.\n\n"
            f"Details were saved to:\n{log_path}",
        )
        raise


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001 - top-level guard so the window never just vanishes
        import traceback
        console.install()
        print("\n" + "=" * 58)
        print("Something went wrong while building the dashboard:")
        print(f"  {exc}")
        print("-" * 58)
        traceback.print_exc()
        print("=" * 58)
        print(f"If this keeps happening, open an issue: {ISSUES_URL}")
        stream = console.stream()
        log_note = ""
        if stream is not None and getattr(stream, "_log", None) is not None:
            log_note = "\n\nThe full details were saved to your log file, next to your screenshots."
        # Holding the window open needs a console to hold. Without stdin this
        # raises rather than returning EOF, which would replace a readable
        # error with an unrelated traceback.
        if console.can_prompt():
            try:
                input("\nPress Enter to close this window...")
            except EOFError:
                pass
        else:
            # Windowed build: nothing on screen to read. Say so in a dialog.
            console.alert("OSRS Dashboard", f"The dashboard could not start.\n\n{exc}{log_note}")
