"""
OSRS RuneLite Screenshot Dashboard Generator
Scans your RuneLite screenshots folder and generates a self-contained HTML dashboard
with stats, milestones, AND a browsable gallery filtered by category.

Usage:
    python osrs_dashboard.py

Output:
    osrs_dashboard.html  (saved next to your screenshots, opens in any browser)
"""

import os
import re
import json
import math
import base64
import sys
import webbrowser
from html import escape
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.request import urlopen
from urllib.parse import quote
from urllib.error import URLError

from economic_value import resolve_economic_value
from value_recipes import VALUE_RECIPES
from wiki_discovery import refresh_catalog
from version import APP_VERSION, ISSUES_URL, NEW_ISSUE_URL

# ─────────────────────────────────────────────────────────────────────
#  CONFIG — set these for your character. Loaded from config.py if present,
#  so a shared copy of the dashboard doesn't have to edit this file directly.
# ─────────────────────────────────────────────────────────────────────
PLAYER_NAME = "Player"
# By default we look at RuneLite's standard screenshots folder. Override
# below if you keep yours somewhere else.
SCREENSHOTS_PATH = str(Path.home() / ".runelite" / "screenshots" / PLAYER_NAME)
# Folders the user declared to be this same account under a former name. They
# are pooled into the scan above. Empty is the only correct default: merging is
# always something the user has explicitly asked for, never something detected.
MERGE_FOLDERS = []

# Road to Max — ACTIVE_SKILLS is a manual override for the ⚡ Active badge.
# Leave empty (default) and active skills are detected automatically from the
# XP snapshot history: whatever actually gained XP recently gets the badge.
# ENGINE DEFAULTS ARE NEUTRAL — personal strategy lives in config.py, never
# here. Anything set here ships to every clan user via the exe.
ACTIVE_SKILLS = []

# Luck engine: attested loot for bosses farmed before the screenshot era.
# The luck math can only see screenshot evidence — drops from before RuneLite
# screenshots was enabled are invisible and the boss reads "no evidence".
# Items listed here count as owned (name → copies). Set in config.py —
# NEVER here (this is per-player attested loot, not engine data).
LUCK_OWNED_OVERRIDES = {}

# One-time, user-attested component quantities that predate or escaped
# screenshot capture. These are additions beyond screenshot evidence, not a
# second inventory system; keep the engine neutral and use config.py only.
VALUE_COMPONENT_OVERRIDES = {}

# Optional config.py override — lets a shared copy plug in their own player
# name + path without editing this file. config.py lives at the project root
# (one level up from _engine/) so the user can edit it without digging into code.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
try:
    import config as _user_config
    PLAYER_NAME = getattr(_user_config, "PLAYER_NAME", PLAYER_NAME)
    SCREENSHOTS_PATH = getattr(_user_config, "SCREENSHOTS_PATH", SCREENSHOTS_PATH)
    ACTIVE_SKILLS = getattr(_user_config, "ACTIVE_SKILLS", ACTIVE_SKILLS)
    LUCK_OWNED_OVERRIDES = getattr(_user_config, "LUCK_OWNED_OVERRIDES", LUCK_OWNED_OVERRIDES)
    VALUE_COMPONENT_OVERRIDES = getattr(
        _user_config, "VALUE_COMPONENT_OVERRIDES", VALUE_COMPONENT_OVERRIDES
    )
except ImportError:
    pass

OUTPUT_FILE = str(Path(SCREENSHOTS_PATH) / "osrs_dashboard.html")

# Hiscores skill order (matches index_lite.ws response, skip index 0 = Overall)
HISCORES_SKILLS = [
    "Attack", "Defence", "Strength", "Hitpoints", "Ranged", "Prayer", "Magic",
    "Cooking", "Woodcutting", "Fletching", "Fishing", "Firemaking", "Crafting",
    "Smithing", "Mining", "Herblore", "Agility", "Thieving", "Slayer",
    "Farming", "Runecraft", "Hunter", "Construction", "Sailing",
]

SKILLS = HISCORES_SKILLS


def xp_for_level(level):
    """Total XP required to reach a given level (standard OSRS formula)."""
    total = 0
    for l in range(1, level):
        total += int(l + 300 * (2 ** (l / 7.0)))
    return total // 4


MAX_XP = xp_for_level(99)  # 13,034,431

# XP snapshot history — lives next to the screenshots/output so it travels
# with the account data. One entry per calendar day; re-running the same day
# overwrites that day's entry.
XP_HISTORY_FILE = str(Path(SCREENSHOTS_PATH) / "xp_history.json")

# User-curated screenshot favorites live beside the dashboard and XP history.
# The active local service writes this file; normal dashboard rebuilds only
# read it, so refreshing can never erase the user's curation.
FAVORITES_FILE = str(Path(SCREENSHOTS_PATH) / "favorites.json")

# Indirect-value state lives with the account, beside the other durable
# dashboard files. Completed economic events freeze their detected value so
# old progression does not move when Grand Exchange prices change.
ECONOMIC_EVENTS_FILE = str(Path(SCREENSHOTS_PATH) / "economic_events.json")
VALUE_PRICE_CACHE_FILE = str(Path(SCREENSHOTS_PATH) / "value_price_cache.json")
WIKI_DISCOVERY_CATALOG_FILE = str(Path(SCREENSHOTS_PATH) / "wiki_discovery_catalog.json")

# Opt-in only. A normal refresh looks up bosses it has never seen and skips the
# rest, which is what keeps it fast. Setting this re-reads the wiki for every
# boss the account has kills on, so items added to an already-known boss's drop
# table get picked up without waiting for a new build. The packaged launcher
# asks the user; nothing sets it automatically.
FORCE_BOSS_DATA_REFRESH = False


def _boss_refresh_progress(position, total, boss):
    """Print one line per boss during a forced refresh so it never looks hung."""
    print(f"  [{position}/{total}] {boss}")

# Road to Max forecasting uses a rolling two-week window: responsive enough
# to follow a real playstyle change, but less volatile than a single week.
PACE_WINDOW_DAYS = 14
PACE_EARLY_MIN_DAYS = 7
ETA_USEFUL_MAX_DAYS = 365 * 3
ACTIVE_XP_FLOOR = 100_000


FAVORITES_SCHEMA_VERSION = 2


def favorite_key(folder_name, relative_path):
    """The stable identity of a screenshot: its folder plus its path inside it.

    Deliberately not the path relative to whichever folder is currently
    primary. That one changes when the user recomposes the account — the same
    image is `Boss Kills/x.png` in one composition and `../OldName/Boss
    Kills/x.png` in another — so keying on it would drop the user's hearts
    every time they changed their mind (DESIGN.md non-negotiable #16).
    """
    return f"{folder_name}/{str(relative_path).replace(chr(92), '/').lstrip('/')}"


def load_favorite_paths(path=None, primary_folder=None):
    """Return stable favorite keys from favorites.json, migrating version 1.

    Version 1 stored paths relative to the account folder, back when an
    account was always exactly one folder. Every such entry therefore belongs
    to the primary folder, which makes the upgrade unambiguous. It is applied
    on read and not written back: a plain refresh has never modified this file
    and must not start, so the file becomes version 2 the next time the user
    actually toggles a favorite.
    """
    favorites_path = Path(path or FAVORITES_FILE)
    try:
        with favorites_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()

    if not isinstance(payload, dict):
        return set()

    raw = payload.get("favorites", [])
    values = [
        str(value).replace("\\", "/").strip()
        for value in raw
        if isinstance(value, str) and value.strip()
    ]

    try:
        version = int(payload.get("version", 1))
    except (TypeError, ValueError):
        version = 1

    if version >= FAVORITES_SCHEMA_VERSION:
        return set(values)

    # Version 1: bare account-relative paths. Without a primary folder name
    # there is nothing to key them to, so leave them as-is rather than invent
    # a prefix that would not match anything.
    folder = primary_folder or Path(SCREENSHOTS_PATH).name
    if not folder:
        return set(values)
    return {favorite_key(folder, value) for value in values}


def favorites_file_for(folder):
    """Where a folder's own favourites live: beside its screenshots."""
    return Path(folder) / "favorites.json"


def load_all_favorites(primary_path=None, merge_folders=None):
    """Every favourite for this account, across all the folders it is made of.

    Each folder stores the favourites for its *own* screenshots, which is what
    keeps this a plain union with nothing to reconcile. It also means removing
    a folder from the account leaves its hearts dormant alongside it rather
    than deleting them, and re-adding the folder brings them back — the
    reversibility non-negotiable #16 promises.
    """
    primary = Path(primary_path or SCREENSHOTS_PATH)
    favorites = load_favorite_paths(favorites_file_for(primary), primary_folder=primary.name)
    for folder in merge_folders or []:
        folder = Path(folder)
        favorites |= load_favorite_paths(
            favorites_file_for(folder),
            primary_folder=folder.name,
        )
    return favorites


def load_dashboard_asset(name):
    """Read a pinned dashboard asset from source or a PyInstaller bundle."""
    candidates = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / name)
    candidates.append(Path(_PROJECT_ROOT) / "OSRS Dashboard Resources" / name)
    for path in candidates:
        try:
            return path.read_bytes()
        except OSError:
            continue
    raise FileNotFoundError(f"Dashboard asset is missing: {name}")


def load_chart_js():
    """Load the pinned Chart.js runtime for fully offline chart rendering."""
    script = load_dashboard_asset("chart.umd.min.js").decode("utf-8")
    if "Chart.js v4.4.0" in script:
        return script.replace("//# sourceMappingURL=chart.umd.js.map", "").replace("</script>", "<\\/script>")
    raise ValueError("chart.umd.min.js is not the pinned Chart.js 4.4.0 runtime.")


def load_dashboard_font_css():
    """Embed the dashboard's pinned Latin webfonts as data URIs."""
    faces = [
        ("Cinzel", "normal", "400 700", "Cinzel-Latin.woff2"),
        ("Crimson Text", "normal", "400", "CrimsonText-Regular-Latin.woff2"),
        ("Crimson Text", "normal", "600", "CrimsonText-Semibold-Latin.woff2"),
        ("Crimson Text", "italic", "400", "CrimsonText-Italic-Latin.woff2"),
    ]
    rules = []
    for family, style, weight, name in faces:
        encoded = base64.b64encode(load_dashboard_asset(name)).decode("ascii")
        rules.append(
            "@font-face{"
            f"font-family:'{family}';font-style:{style};font-weight:{weight};"
            "font-display:swap;"
            f"src:url(data:font/woff2;base64,{encoded}) format('woff2');"
            "}"
        )
    return "".join(rules)


def _read_xp_history_file(path):
    """One xp_history.json as a list, or an empty list. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (OSError, ValueError):
        return []
    return history if isinstance(history, list) else []


def merge_xp_history(primary_history, merge_folders=None):
    """Union in snapshots recorded under this account's earlier names.

    Read-only, and deliberately so. The merged snapshots are *not* written
    into the primary folder's file: absorbing them would make the merge
    permanent, so removing a folder later would leave its history behind with
    no way to tell it apart. Recomposing the account has to be reversible, the
    same way favourites are (DESIGN.md #16), so the union is recomputed each
    refresh and only the primary folder's own file is ever written.

    A date present in more than one file resolves to the primary folder's
    copy. That is the file still being appended to, so it is the most recent
    reading of the account.
    """
    if not merge_folders:
        return primary_history

    by_date = {}
    for folder in merge_folders:
        for entry in _read_xp_history_file(Path(folder) / "xp_history.json"):
            if isinstance(entry, dict) and entry.get("date"):
                by_date.setdefault(entry["date"], entry)
    inherited = len(by_date)

    for entry in primary_history or []:
        if isinstance(entry, dict) and entry.get("date"):
            by_date[entry["date"]] = entry

    merged = sorted(by_date.values(), key=lambda h: h.get("date", ""))
    added = len(merged) - len(primary_history or [])
    if added > 0:
        print(f"Inherited {added} XP snapshot(s) from earlier names "
              f"({inherited} read, {inherited - added} already present).")
    return merged


def update_xp_history(hiscores):
    """Log today's per-skill XP snapshot and return the full history (oldest first).

    This is what powers pace tracking and ETAs on the Road to Max tab. It
    builds automatically as the dashboard is refreshed — no external service
    required. If hiscores are unreachable this refresh, the existing history
    is returned untouched."""
    history = []
    try:
        with open(XP_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
        if not isinstance(history, list):
            history = []
    except (OSError, ValueError):
        history = []

    skills = hiscores.get("skills", {})
    if skills:
        today = datetime.now().strftime("%Y-%m-%d")
        entry = {
            "date": today,
            "xp": {s: v["xp"] for s, v in skills.items() if v.get("xp", -1) >= 0},
            # Widened July 2026: clues, boss KC, and collection-log count ride
            # along in every snapshot. Costs ~2KB/day; buys every future trend
            # insight (clues/month, KC velocity, clog completion curve) a
            # history that only exists from the day collection started.
            "clues": dict(hiscores.get("clues", {})),
            "kc": dict(hiscores.get("bosses", {})),
        }
        if hiscores.get("collections_logged"):
            entry["clog"] = hiscores["collections_logged"]
        history = [h for h in history if h.get("date") != today]
        history.append(entry)
        history.sort(key=lambda h: h.get("date", ""))
        try:
            with open(XP_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f)
            print(f"XP snapshot logged ({len(history)} total in history).")
        except OSError as e:
            print(f"Could not write xp_history.json: {e}")
    return history


def compute_pace(history, window_days=PACE_WINDOW_DAYS):
    """Per-skill XP/day over the recent window.

    Returns (rates_dict, span_days). Uses the oldest snapshot within the
    window as the baseline; needs at least 3 days of spread before rates
    are considered meaningful (returns empty rates below that)."""
    if len(history) < 2:
        return {}, 0
    latest = history[-1]
    try:
        latest_dt = datetime.strptime(latest["date"], "%Y-%m-%d")
    except (KeyError, ValueError):
        return {}, 0
    baseline = None
    for h in history[:-1]:
        try:
            h_dt = datetime.strptime(h["date"], "%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        if (latest_dt - h_dt).days <= window_days:
            baseline = h
            break
    if baseline is None:
        # A snapshot outside the selected window is not evidence of recent pace.
        return {}, 0
    try:
        span = (latest_dt - datetime.strptime(baseline["date"], "%Y-%m-%d")).days
    except (KeyError, ValueError):
        return {}, 0
    if span < 3:
        return {}, span
    rates = {}
    for skill, xp_now in latest.get("xp", {}).items():
        xp_then = baseline.get("xp", {}).get(skill)
        if xp_then is not None and xp_now > xp_then:
            rates[skill] = (xp_now - xp_then) / span
    return rates, span


def compute_account_pulse(history, window_days):
    """Summarize measurable account movement inside a recent time window.

    Uses the earliest snapshot that falls within the requested window as the
    baseline and the newest snapshot as the endpoint. Older XP-only snapshots
    remain valid: collection-log, clue, and boss-KC movement are shown only
    when both endpoints contain that metric.
    """
    empty = {
        "window": window_days,
        "available": False,
        "span_days": 0,
        "from_date": "",
        "to_date": "",
        "total_xp": 0,
        "boss_kills": 0,
        "clog_gain": None,
        "clue_gain": None,
        "focus_skill": "",
        "focus_gain": 0,
        "focus_share": 0,
        "top_skills": [],
        "top_bosses": [],
        "verdict": "Account Pulse is building its history.",
    }
    if len(history) < 2:
        return empty

    dated = []
    for snapshot in history:
        try:
            snapshot_date = datetime.strptime(snapshot.get("date", ""), "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        dated.append((snapshot_date, snapshot))
    dated.sort(key=lambda pair: pair[0])
    if len(dated) < 2:
        return empty

    latest_date, latest = dated[-1]
    cutoff = latest_date - timedelta(days=window_days)
    candidates = [(date, snapshot) for date, snapshot in dated[:-1] if date >= cutoff]
    if not candidates:
        return empty

    baseline_date, baseline = candidates[0]
    span_days = (latest_date - baseline_date).days
    if span_days < 1:
        return empty

    xp_gains = {}
    for skill, current in latest.get("xp", {}).items():
        previous = baseline.get("xp", {}).get(skill)
        if previous is not None and current > previous:
            xp_gains[skill] = current - previous

    kc_gains = {}
    for boss, current in latest.get("kc", {}).items():
        previous = baseline.get("kc", {}).get(boss)
        if previous is not None and current > previous:
            kc_gains[boss] = current - previous

    total_xp = sum(xp_gains.values())
    boss_kills = sum(kc_gains.values())
    top_skills = sorted(xp_gains.items(), key=lambda item: -item[1])[:3]
    top_bosses = sorted(kc_gains.items(), key=lambda item: -item[1])[:3]
    focus_skill, focus_gain = top_skills[0] if top_skills else ("", 0)
    focus_share = round(focus_gain / total_xp * 100) if total_xp else 0

    clog_gain = None
    if baseline.get("clog") is not None and latest.get("clog") is not None:
        clog_gain = max(int(latest["clog"]) - int(baseline["clog"]), 0)

    clue_gain = None
    baseline_clues = baseline.get("clues", {}).get("Clue Scrolls (all)")
    latest_clues = latest.get("clues", {}).get("Clue Scrolls (all)")
    if baseline_clues is not None and latest_clues is not None:
        clue_gain = max(int(latest_clues) - int(baseline_clues), 0)

    if total_xp and focus_share >= 60:
        verdict = f"{focus_skill} defined the last {span_days} days."
    elif total_xp and boss_kills:
        verdict = "A balanced stretch of skilling and bossing."
    elif boss_kills:
        verdict = f"Bossing defined the last {span_days} days."
    elif total_xp:
        verdict = "Progress was spread across the account."
    elif (clog_gain or 0) + (clue_gain or 0) > 0:
        verdict = "Quiet gains, but the account still moved."
    else:
        verdict = "No measurable gains in this window yet."

    return {
        "window": window_days,
        "available": True,
        "span_days": span_days,
        "from_date": baseline.get("date", ""),
        "to_date": latest.get("date", ""),
        "total_xp": total_xp,
        "boss_kills": boss_kills,
        "clog_gain": clog_gain,
        "clue_gain": clue_gain,
        "focus_skill": focus_skill,
        "focus_gain": focus_gain,
        "focus_share": focus_share,
        "top_skills": [
            {"name": name, "gain": gain, "share": round(gain / total_xp * 100)}
            for name, gain in top_skills
        ] if total_xp else [],
        "top_bosses": [
            {"name": name, "kills": kills, "share": round(kills / boss_kills * 100)}
            for name, kills in top_bosses
        ] if boss_kills else [],
        "verdict": verdict,
    }


# Boss discovery — activities in the hiscores JSON are treated as bosses
# UNLESS they match one of these markers (case-insensitive substring). Bosses
# churn constantly; this non-boss list changes maybe once a year. Failure mode
# is benign and visible: a weird new non-boss activity shows up as an empty
# boss tile in "Other" and gets one marker added here. (The old inclusion-list
# design failed silently — 9 guessed phantom entries, 6 real bosses missing.)
NON_BOSS_MARKERS = (
    "bounty hunter", "points", "rank", "zeal",
    "rifts closed", "colosseum glory", "collections logged",
)

# Cache of bosses discovered from the live API beyond the seed list. Lives
# next to the output so it travels with the account data. Names are never
# removed — retired bosses keep their historical attribution.
KNOWN_BOSSES_FILE = str(Path(SCREENSHOTS_PATH) / "known_bosses.json")


def load_known_bosses():
    """Seed list (HISCORES_BOSSES) plus cached API discoveries, deduped case-insensitively."""
    names = list(HISCORES_BOSSES)
    seen = {n.lower() for n in names}
    try:
        with open(KNOWN_BOSSES_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if isinstance(cached, list):
            for n in cached:
                if isinstance(n, str) and n.lower() not in seen:
                    names.append(n)
                    seen.add(n.lower())
    except (OSError, ValueError):
        pass
    return names


def update_known_bosses(names):
    """Union newly seen activity names into the cache file. Returns the new ones."""
    current = {n.lower() for n in load_known_bosses()}
    new = [n for n in names if n.lower() not in current]
    if not new:
        return []
    try:
        with open(KNOWN_BOSSES_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, list):
            cached = []
    except (OSError, ValueError):
        cached = []
    cached = sorted(set(cached) | set(new))
    try:
        with open(KNOWN_BOSSES_FILE, "w", encoding="utf-8") as f:
            json.dump(cached, f, indent=1)
        print(f"New bosses discovered from hiscores: {', '.join(new)}")
    except OSError as e:
        print(f"Could not write known_bosses.json: {e}")
    return new


def parse_hiscores_payload(payload):
    """Turn the raw hiscores JSON into the result dict. Split out from
    fetch_hiscores so it can be tested without network access."""
    result = {"skills": {}, "clues": {}, "bosses": {}, "boss_names": []}

    skill_lookup = {s.lower(): s for s in HISCORES_SKILLS}
    for s in payload.get("skills", []):
        name = s.get("name", "")
        level = s.get("level", -1)
        xp = s.get("xp", -1)
        canonical = skill_lookup.get(name.lower())
        if canonical and level > 0:
            result["skills"][canonical] = {"level": int(level), "xp": int(xp)}

    # Known names canonicalize case drift (API says "Kree'Arra", our data
    # says "Kree'arra"); genuinely new names pass through verbatim.
    boss_lookup = {b.lower(): b for b in load_known_bosses()}
    for a in payload.get("activities", []):
        name = a.get("name", "")
        score = a.get("score", -1)
        if not name:
            continue
        low = name.lower()
        if "clue scrolls" in low:
            if score is not None and score > 0:
                result["clues"][name] = int(score)
            continue
        if "collections logged" in low:
            if score is not None and score > 0:
                result["collections_logged"] = int(score)
            continue
        if any(marker in low for marker in NON_BOSS_MARKERS):
            continue
        canonical_boss = boss_lookup.get(low, name)
        result["boss_names"].append(canonical_boss)
        if score is not None and score > 0:
            result["bosses"][canonical_boss] = int(score)

    return result


def fetch_hiscores(player_name, debug=False):
    """Fetch skill levels, clue completions, and boss KC from the OSRS Hiscores JSON API.

    The JSON endpoint returns named entries, which means we don't have to track
    Jagex's positional drift (new activity slots added between Bounty Hunter and
    Clue Scrolls etc.). All matching here is by name, and every non-excluded
    activity is treated as a boss — new bosses are discovered automatically.
    """
    # The name has to be percent-encoded. OSRS names may contain spaces, and an
    # unencoded space makes urlopen raise "URL can't contain control characters"
    # — which the broad except below swallows into the screenshot-data fallback.
    # The result is a silently worse dashboard, with the real cause visible only
    # in the log file. Found August 10, 2026 in a real account's log ("Hunt N
    # Gains"); spaces are common in OSRS names, so this affected a large share
    # of downloaded copies from the first release.
    url = f"https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player={quote(player_name, safe='')}"
    try:
        print(f"Fetching hiscores for {player_name}...")
        with urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        if debug:
            debug_path = SCREENSHOTS_PATH + r"\hiscores_debug.txt"
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            print(f"  Debug dump written to: {debug_path}")

        result = parse_hiscores_payload(payload)
        print(f"Skills: {len(result['skills'])} | Bosses with KC: {len(result['bosses'])} | Clues: {len(result['clues'])}")
        return result
    except URLError as e:
        print(f"Could not fetch hiscores: {e}. Using screenshot data instead.")
        return {"skills": {}, "clues": {}, "bosses": {}, "boss_names": []}
    except Exception as e:
        print(f"Hiscores parse error: {e}. Using screenshot data instead.")
        return {"skills": {}, "clues": {}, "bosses": {}, "boss_names": []}

# NOTE: HISCORES_ACTIVITIES list is intentionally not maintained. We now use the
# JSON hiscores endpoint, which keys every activity by name, so positional drift
# (Jagex inserting new activity slots) no longer breaks clue parsing.

HISCORES_BOSSES = [
    "Abyssal Sire", "Alchemical Hydra", "Amoxliatl", "Araxxor", "Artio",
    "Barrows Chests", "Bryophyta", "Callisto", "Calvar'ion", "Cerberus",
    "Chambers of Xeric", "Chambers of Xeric: Challenge Mode",
    "Chaos Elemental", "Chaos Fanatic", "Commander Zilyana", "Corporeal Beast",
    "Crazy Archaeologist", "Dagannoth Prime", "Dagannoth Rex", "Dagannoth Supreme",
    "Deranged Archaeologist", "Duke Sucellus", "Kree'arra", "Giant Mole",
    "Grotesque Guardians", "Hespori", "Kalphite Queen", "King Black Dragon",
    "Kraken", "General Graardor", "K'ril Tsutsaroth", "Lunar Chests", "Mimic",
    "Nex", "Nightmare", "Phosani's Nightmare", "Obor", "Phantom Muspah",
    "Sarachnis", "Scorpia", "Scurrius", "Skotizo", "Sol Heredit", "Spindel",
    "Tempoross", "The Gauntlet", "The Corrupted Gauntlet", "The Hueycoatl",
    "The Leviathan", "The Whisperer", "Theatre of Blood",
    "Theatre of Blood: Hard Mode", "Thermonuclear Smoke Devil",
    "Tombs of Amascut", "Tombs of Amascut: Expert Mode",
    "TzKal-Zuk", "TzTok-Jad", "Vardorvis", "Venenatis", "Vet'ion",
    "Vorkath", "Wintertodt", "Zalcano", "Zulrah",
    # Tormented Demons: not a hiscores activity, but kept here so the drop
    # scraper (which imports this list) fetches its table for attribution.
    "Tormented Demons",
    # Newer bosses — verified against the live hiscores JSON on 2026-07-05.
    # (A prior batch of guessed "(hard)" variant entries was removed the same
    # day: the API has no such activities.)
    "Brutus", "Doom of Mokhaiotl", "Maggot King", "Shellbane Gryphon",
    "The Royal Titans", "Yama",
]

# Maps boss name → item names that can drop from it (for screenshot pairing)
BOSS_DROPS = {
    "Abyssal Sire":             ["Abyssal dagger", "Abyssal orphan", "Unsired"],
    "Alchemical Hydra":         ["Hydra's claw", "Hydra's heart", "Hydra leather", "Hydra tail", "Ikkle hydra"],
    "Araxxor":                  ["Noxious halberd", "Araxyte head"],
    "Barrows Chests":           ["Ahrim", "Dharok", "Guthan", "Karil", "Torag", "Verac"],
    "Callisto":                 ["Tyrannical ring", "Callisto cub"],
    "Calvar'ion":               ["Tyrannical ring", "Callisto cub"],
    "Cerberus":                 ["Pegasian crystal", "Primordial crystal", "Eternal crystal", "Smouldering stone", "Hellpuppy"],
    "Chambers of Xeric":        ["Twisted bow", "Elder maul", "Kodai wand", "Dragon hunter crossbow",
                                  "Dexterous prayer scroll", "Arcane prayer scroll", "Olmlet"],
    "Chambers of Xeric: Challenge Mode": ["Twisted ancestral colour kit", "Metamorphic dust"],
    "Chaos Elemental":          ["Dragon pickaxe", "Pet chaos elemental"],
    "Commander Zilyana":        ["Saradomin sword", "Armadyl crossbow", "Saradomin hilt", "Pet zilyana"],
    "Corporeal Beast":          ["Spectral sigil", "Arcane sigil", "Elysian sigil", "Pet dark core"],
    "Dagannoth Prime":          ["Seers ring", "Pet dagannoth prime"],
    "Dagannoth Rex":            ["Berserker ring", "Pet dagannoth rex"],
    "Dagannoth Supreme":        ["Archers ring", "Pet dagannoth supreme"],
    "Duke Sucellus":            ["Magus vestige", "Ice quartz", "Frozen tablet", "Chromium ingot", "Baron",
                                  "Eye of the duke", "Eye of duke"],
    "General Graardor":         ["Bandos chestplate", "Bandos tassets", "Bandos boots", "Bandos hilt", "Pet general graardor"],
    "Giant Mole":               ["Baby mole"],
    "Grotesque Guardians":      ["Granite gloves", "Granite ring", "Noon", "Dawn"],
    "Kalphite Queen":           ["Dragon chainbody", "Kalphite princess"],
    "King Black Dragon":        ["Dragon pickaxe", "Prince black dragon"],
    "Kraken":                   ["Kraken tentacle", "Trident of the seas", "Pet kraken"],
    "Kree'arra":                ["Armadyl chestplate", "Armadyl chainskirt", "Armadyl helmet", "Armadyl hilt", "Pet kree'arra"],
    "K'ril Tsutsaroth":         ["Zamorakian spear", "Steam battlestaff", "Zamorak hilt", "Pet k'ril tsutsaroth"],
    "Nex":                      ["Torva full helm", "Torva platebody", "Torva platelegs", "Nihil horn", "Nexling"],
    "Nightmare":                ["Inquisitor's great helm", "Inquisitor's hauberk", "Inquisitor's plateskirt",
                                  "Nightmare staff", "Little nightmare"],
    "Phantom Muspah":           ["Venator shard", "Frozen cache"],
    "Sarachnis":                ["Sarachnis cudgel", "Sraracha"],
    "Scorpia":                  ["Odium shard", "Malediction shard", "Scorpia's offspring"],
    "Skotizo":                  ["Skotos", "Dark totem"],
    "Sol Heredit":              ["Dizana's quiver", "Quiver", "Echo crystal"],
    "Spindel":                  ["Voidwaker gem", "Spindel"],
    "The Corrupted Gauntlet":   ["Enhanced crystal weapon seed", "Youngllef"],
    "The Gauntlet":             ["Crystal weapon seed", "Youngllef"],
    "The Leviathan":            ["Leviathan's lure", "Venator vestige", "Lure of the Leviathan", "Scarred tablet", "Lil' lev"],
    "The Whisperer":            ["Bellator vestige", "Smoke quartz", "Sirenic tablet", "Wisp"],
    "Theatre of Blood":         ["Scythe of vitur", "Justiciar faceguard", "Justiciar chestguard",
                                  "Justiciar legguards", "Avernic defender hilt", "Sanguinesti staff",
                                  "Ghrazi rapier", "Lil' zik"],
    "Theatre of Blood: Hard Mode": ["Sanguine torva", "Sanguine ornament kit"],
    "Thermonuclear Smoke Devil": ["Smoke battlestaff", "Occult necklace", "Pet smoke devil"],
    "Tombs of Amascut":         ["Tumeken's shadow", "Masori mask", "Masori body", "Masori chaps",
                                  "Elidinis' ward", "Osmumten's fang", "Lightbearer", "Breach of the scarab"],
    "Tombs of Amascut: Expert Mode": ["Tumeken's shadow"],
    "TzKal-Zuk":                ["Infernal cape", "Tzrek-zuk"],
    "TzTok-Jad":                ["Fire cape", "Jal-nib-rek"],
    "Vardorvis":                ["Executioner's axe head", "Ultor vestige", "Blood quartz", "Strangled tablet", "Butch"],
    "Venenatis":                ["Venenatis spiderling", "Treasonous ring"],
    "Vet'ion":                  ["Voidwaker gem", "Vet'ion jr.", "Ring of the gods"],
    "Vorkath":                  ["Draconic visage", "Skeletal visage", "Vorki", "Vorkath's head"],
    "Wintertodt":               ["Phoenix"],
    "Zalcano":                  ["Smolcano", "Crystal tool seed"],
    "Zulrah":                   ["Tanzanite fang", "Magic fang", "Serpentine visage", "Tanzanite mutagen",
                                  "Magma mutagen", "Snakeling"],
    "Tormented Demons":         ["Tormented synapse", "Emberlight"],
}

# If update_boss_drops.py has been run, prefer the auto-generated dict — it's
# more comprehensive than the curated list above. Generated entries take
# precedence; any boss missing from the generated file keeps its manual entry.
try:
    from boss_drops_generated import BOSS_DROPS as _GENERATED
    for _b, _items in _GENERATED.items():
        BOSS_DROPS[_b] = _items
    print(f"Loaded {len(_GENERATED)} bosses from boss_drops_generated.py")
except ImportError:
    pass

# ────────────────────────────────────────────────────────────────────────
# Shared drops — items that don't belong to a specific boss because they
# drop from many. Two tiers:
#
#   1. GLOBAL_SHARED_DROPS  — items that drop from many high-level monsters
#      across the game (long bone, crystal key, etc.). Get bucketed into a
#      page-top virtual tile called "Common Drops".
#
#   2. CATEGORY_SHARED_DROPS — items that drop from any boss within a
#      specific category (godsword shards from any GWD boss, awakener's orbs
#      from any DT2 boss, etc.). Get bucketed into a virtual tile at the top
#      of that category called "Shared <Category> Drops".
#
# Each item maps to a `source label` that shows under the screenshot so the
# viewer knows where it came from.
# ────────────────────────────────────────────────────────────────────────

GLOBAL_SHARED_DROPS = {
    "long bone":           "Many high-level bosses",
    "curved bone":         "Many high-level bosses",
    "crystal key":         "Various monsters",
    "loop half of key":    "Many monsters",
    "tooth half of key":   "Many monsters",
    "ancient effigy":      "Many bosses",
    "starved ancient effigy": "Many bosses",
    "onyx bolts (e)":      "Many high-level bosses",
    "onyx bolt tips":      "Many high-level bosses",
    "dragonstone bolts (e)": "Many high-level bosses",
    "dragonstone bolt tips": "Many high-level bosses",
    "brimstone key":       "Konar slayer task (any monster)",
    "rusty sword":         "Many monsters",
    "looting bag":         "Any monster in the Wilderness",
    "aether catalyst":     "Abyssal Sire / The Leviathan",
    "diamond bolts (e)":   "Spindel / Venenatis",
    "dragon pickaxe":      "Many wilderness bosses + KQ / KBD",
    "dragon javelin tips": "DT2 bosses + Kree'arra / Scorpia",
    "tooth half of key (moon key)": "Amoxliatl / The Hueycoatl",
    # Clue scrolls drop from practically everything — never boss-specific.
    # (Added July 2026: Doom of Mokhaiotl's scraped table listed elite clues,
    # which pulled every elite clue screenshot onto a boss with 0 KC.)
    "clue scroll (beginner)": "Any monster",
    "clue scroll (easy)":   "Any monster",
    "clue scroll (medium)": "Any monster",
    "clue scroll (hard)":   "Any monster",
    "clue scroll (elite)":  "Any monster",
    "clue scroll (master)": "Any monster",
    # Generic alchables on many boss tables (added July 2026 after Yama's
    # scraped table claimed Vorkath-era plateskirt stacks).
    "battlestaff":          "Many monsters",
    "dragon platelegs":     "Many bosses & demons",
    "dragon plateskirt":    "Many bosses & demons",
    "rune platebody":       "Many bosses",
    "rune chainbody":       "Many bosses",
}

CATEGORY_SHARED_DROPS = {
    "Desert Treasure 2": {
        "awakener's orb":              "Any DT2 boss",
        "virtus mask":                 "Any DT2 boss",
        "virtus robe top":             "Any DT2 boss",
        "virtus robe bottom":          "Any DT2 boss",
        "ancient blood ornament kit":  "Any DT2 boss",
        "chromium ingot":              "Any DT2 boss",
    },
    "God Wars Dungeon": {
        "godsword shard 1":   "Any GWD boss",
        "godsword shard 2":   "Any GWD boss",
        "godsword shard 3":   "Any GWD boss",
    },
    "Wilderness": {
        "voidwaker hilt":     "Vet'ion / Calvar'ion",
        "voidwaker blade":    "Venenatis / Spindel",
        "voidwaker gem":      "Callisto / Artio",
        "tyrannical ring":    "Callisto / Artio",
        "treasonous ring":    "Venenatis / Spindel",
        "ring of the gods":   "Vet'ion / Calvar'ion",
        "fangs of venenatis": "Venenatis / Spindel",
        "claws of callisto":  "Callisto / Artio",
        "skull of vet'ion":   "Vet'ion / Calvar'ion",
        "pet chaos elemental": "Chaos Elemental / Chaos Fanatic",
    },
}

# Flat lookup helpers built from the above
_GLOBAL_SHARED_LOOKUP = {k: v for k, v in GLOBAL_SHARED_DROPS.items()}
_CATEGORY_SHARED_LOOKUP = {}  # item_lower → (category, source_label)
for _cat, _items in CATEGORY_SHARED_DROPS.items():
    for _it, _src in _items.items():
        _CATEGORY_SHARED_LOOKUP[_it] = (_cat, _src)
_ALL_SHARED_KEYS = set(_GLOBAL_SHARED_LOOKUP) | set(_CATEGORY_SHARED_LOOKUP)

# Auto-shared detection (July 2026): any item appearing on 4+ bosses' drop
# tables is treated as global-shared automatically. This is the permanent fix
# for the misattribution class found the day the new-boss scrape landed —
# Yama's table claimed Vorkath-era plateskirt stacks, and Doom's claimed every
# elite clue. Real uniques never sit on 4+ tables; the manual lists above
# stay authoritative for labels and for anything below the threshold.
_AUTO_SHARED_THRESHOLD = 4
_item_boss_count = defaultdict(set)
for _boss, _items in BOSS_DROPS.items():
    for _it in _items:
        _item_boss_count[_it.lower()].add(_boss)
_auto_shared = []
for _it_l, _bosses_set in _item_boss_count.items():
    if len(_bosses_set) >= _AUTO_SHARED_THRESHOLD and _it_l not in _ALL_SHARED_KEYS:
        _GLOBAL_SHARED_LOOKUP[_it_l] = f"Many bosses ({len(_bosses_set)} tables)"
        _auto_shared.append(_it_l)
_ALL_SHARED_KEYS |= set(_auto_shared)
if _auto_shared:
    print(f"Auto-shared {len(_auto_shared)} multi-boss item(s) "
          f"(on {_AUTO_SHARED_THRESHOLD}+ drop tables)")

# ── Luck engine ─────────────────────────────────────────────────────────
# Expected-vs-actual drop math: KC × rate says what you should have seen;
# screenshots say what you got. Honest by design — only measures where the
# data is trustworthy (see LUCK_EXCLUDED_BOSSES and the KC floor), and says
# "not enough rolls" rather than faking precision.
try:
    from boss_drops_generated import BOSS_DROP_RATES as _DROP_RATES
except ImportError:
    _DROP_RATES = {}

LUCK_MIN_KC = 25          # below this, not enough rolls to say anything
LUCK_MAX_P = 1.0 / 100    # only items at least this rare count as "uniques"
                          # (1/100 floor keeps junk sub-uniques like 1/64
                          # teleport scrolls out of the math)
LUCK_GAP_EXPECTED = 2.0   # a boss with this many expected uniques but ZERO
                          # owned is almost certainly missing screenshot
                          # coverage (pre-RuneLite kills), not cursed —
                          # flagged and excluded from the headline number
LUCK_MISSING_MAX_P = 1.0 / 250  # UNOWNED items must be at least this rare to
                          # count. Mid-rarity generics (Dragon scimitar-class,
                          # ~1/100-1/250) aren't clog items and never trigger
                          # RuneLite screenshots — they'd sit in the "owed"
                          # list as phantoms you can never clear. Owned items
                          # keep the 1/100 floor: ownership proves trackable.
LUCK_EXCLUDED_BOSSES = {
    # Points/contribution-based loot — KC × rate math doesn't apply
    "Chambers of Xeric", "Chambers of Xeric: Challenge Mode",
    "Theatre of Blood", "Theatre of Blood: Hard Mode",
    "Tombs of Amascut", "Tombs of Amascut: Expert Mode",
    "Barrows Chests", "Lunar Chests",
    "Wintertodt", "Tempoross", "Zalcano", "Hespori",
}

_RATE_RE = re.compile(
    r"^\s*(?:~\s*)?(?:(\d+(?:\.\d+)?)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*/\s*([\d,]+(?:\.\d+)?)\s*$"
)


def parse_rate_string(s):
    """'1/136' → 0.00735, '3 x 1/508.8' → 3/508.8, '2/58' → 2/58.

    Returns probability per kill, or None for anything unparseable
    ('Always', 'Varies', leaked wiki templates)."""
    if not s:
        return None
    m = _RATE_RE.match(s.strip())
    if not m:
        return None
    mult = float(m.group(1)) if m.group(1) else 1.0
    num = float(m.group(2))
    den = float(m.group(3).replace(",", ""))
    if den <= 0:
        return None
    p = mult * num / den
    if p <= 0 or p > 1:
        return None
    return p


def apply_discovered_boss_references(references):
    """Merge account-local wiki discoveries without replacing curated tables."""
    for boss, reference in (references or {}).items():
        if not isinstance(reference, dict):
            continue
        drops = reference.get("drops")
        if isinstance(drops, list) and drops:
            existing = list(BOSS_DROPS.get(boss, []))
            seen = {item.lower() for item in existing if isinstance(item, str)}
            for item in drops:
                if isinstance(item, str) and item.lower() not in seen:
                    existing.append(item)
                    seen.add(item.lower())
            BOSS_DROPS[boss] = existing
        rates = reference.get("rates")
        if isinstance(rates, dict) and rates:
            _DROP_RATES.setdefault(boss, {}).update({
                item: rate for item, rate in rates.items()
                if isinstance(item, str) and isinstance(rate, str)
            })


def _norm_cdf(z):
    """Standard normal CDF via erf — turns a z-score into a percentile."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _poisson_tail(lam, c):
    """P(X >= c) for X ~ Poisson(lam) — 'what fraction of players at this KC
    would have c or more of this item?' The engine of the ×N wow-factor."""
    if c <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term = math.exp(-lam)
    cdf = term
    for k in range(1, min(int(c), 200)):
        term *= lam / k
        cdf += term
    return max(0.0, 1.0 - cdf)


def luck_verdict(pct):
    """OSRS-appropriate label for an overall luck percentile."""
    if pct >= 97.5:
        return "Spooned"
    if pct >= 84:
        return "Lucky"
    if pct >= 16:
        return "Fair dice"
    if pct >= 2.5:
        return "Dry"
    return "Scammed by Gielinor"

# Boss categories for the Boss tab. Display order top-to-bottom. Any boss with
# screenshot content but not listed here falls into "Other" at the bottom.
BOSS_CATEGORIES = [
    ("Common Drops", [
        "Common Drops",
    ]),
    ("Raids", [
        "Chambers of Xeric", "Chambers of Xeric: Challenge Mode",
        "Theatre of Blood", "Theatre of Blood: Hard Mode",
        "Tombs of Amascut", "Tombs of Amascut: Expert Mode",
    ]),
    ("Desert Treasure 2", [
        "Shared DT2 Drops",
        "Vardorvis", "Duke Sucellus", "The Whisperer", "The Leviathan",
    ]),
    ("God Wars Dungeon", [
        "Shared GWD Drops",
        "General Graardor", "Kree'arra", "Commander Zilyana",
        "K'ril Tsutsaroth", "Nex",
    ]),
    ("Wilderness", [
        "Shared Wilderness Drops",
        "Callisto", "Artio", "Venenatis", "Spindel",
        "Vet'ion", "Calvar'ion",
        "Chaos Elemental", "Chaos Fanatic",
        "Crazy Archaeologist", "Deranged Archaeologist",
        "King Black Dragon", "Scorpia",
    ]),
    ("Slayer", [
        "Abyssal Sire", "Alchemical Hydra", "Cerberus",
        "Grotesque Guardians", "Kraken", "Thermonuclear Smoke Devil",
        "Araxxor", "Kalphite Queen", "Tormented Demons",
    ]),
    ("Caves & Colosseum", [
        "TzTok-Jad", "TzKal-Zuk", "Sol Heredit",
    ]),
    ("Skilling Bosses", [
        "Tempoross", "Wintertodt", "Zalcano", "Hespori",
    ]),
    ("Solo Bosses", [
        "Vorkath", "Zulrah", "Phantom Muspah",
        "The Hueycoatl", "Amoxliatl",
        "Nightmare", "Phosani's Nightmare",
        "Sarachnis", "Skotizo", "Corporeal Beast",
        "Dagannoth Prime", "Dagannoth Rex", "Dagannoth Supreme",
        "The Gauntlet", "The Corrupted Gauntlet",
    ]),
    ("Mid-game", [
        "Mimic", "Obor", "Bryophyta", "Scurrius",
        "Giant Mole", "Barrows Chests", "Lunar Chests",
    ]),
]

# Reverse lookup: boss name → category
BOSS_TO_CATEGORY = {}
for _cat_name, _bosses in BOSS_CATEGORIES:
    for _b in _bosses:
        BOSS_TO_CATEGORY[_b] = _cat_name

# Auto-categorization hints from the wiki scraper (BOSS_CATEGORY_HINTS in
# boss_drops_generated.py). Manual placements above ALWAYS win — hints only
# apply to bosses not manually listed, and only to categories that already
# exist in the display order. Adding a new display category (e.g., a future
# "Sailing" section) is a deliberate manual edit, never something the wiki
# can conjure.
try:
    from boss_drops_generated import BOSS_CATEGORY_HINTS as _CAT_HINTS
except ImportError:
    _CAT_HINTS = {}
_VALID_CATS = {_c for _c, _ in BOSS_CATEGORIES}
_hinted_count = 0
for _b, _hint_cat in _CAT_HINTS.items():
    if _b not in BOSS_TO_CATEGORY and _hint_cat in _VALID_CATS:
        for _cat_name, _bosses in BOSS_CATEGORIES:
            if _cat_name == _hint_cat:
                _bosses.append(_b)
                break
        BOSS_TO_CATEGORY[_b] = _hint_cat
        _hinted_count += 1
if _hinted_count:
    print(f"Auto-categorized {_hinted_count} boss(es) from wiki hints")


# Maps keywords found in combat task names → boss they belong to
# Used when the boss name doesn't appear verbatim in the task name
COMBAT_TASK_KEYWORDS = {
    "demonic": "K'ril Tsutsaroth",
    "zamorak": "K'ril Tsutsaroth",
    "armadyl": "Kree'arra",
    "bandos": "General Graardor",
    "saradomin": "Commander Zilyana",
    "inferno": "TzKal-Zuk",
    "fight cave": "TzTok-Jad",
    "tzhaar": "TzTok-Jad",
    "tob": "Theatre of Blood",
    "maiden": "Theatre of Blood",
    "verzik": "Theatre of Blood",
    "toa": "Tombs of Amascut",
    "cox": "Chambers of Xeric",
    "olm": "Chambers of Xeric",
    "gauntlet": "The Gauntlet",
    "perilous": "Amoxliatl",
    "colosseum": "Sol Heredit",
    "corp": "Corporeal Beast",
    "kbd": "King Black Dragon",
    "kq": "Kalphite Queen",
    "muspah": "Phantom Muspah",
    "nightmare": "Phosani's Nightmare",
    "smoke devil": "Thermonuclear Smoke Devil",
}


def parse_combat_task(filename):
    """Extract task name from 'Combat task (Task Name) timestamp.png'."""
    match = re.match(r'Combat task \((.+?)\)', Path(filename).stem, re.IGNORECASE)
    return match.group(1).strip() if match else None


# Authoritative CA task → monster mapping from the wiki scraper (July 2026).
# Populated by update_boss_drops.py; empty on old generated files, in which
# case the name heuristics below carry the load alone.
try:
    from boss_drops_generated import CA_TASKS as _CA_TASKS
except ImportError:
    _CA_TASKS = {}
_CA_TASK_LOOKUP = {t.lower(): m for t, m in _CA_TASKS.items()}


def boss_for_combat_task(task_name, all_bosses):
    """Match a combat task name to a boss.

    Wiki mapping first (authoritative); falls back to boss-name substring
    and keyword heuristics for tasks the mapping doesn't know (renamed
    tasks, stale generated file)."""
    lower = task_name.lower()
    monster = _CA_TASK_LOOKUP.get(lower)
    if monster:
        ml = monster.lower()
        # Exact boss match, then longest substring either way (handles
        # "The Nightmare" vs our "Nightmare").
        best, best_len = None, 0
        for boss in all_bosses:
            bl = boss.lower()
            if bl == ml:
                return boss
            if (bl in ml or ml in bl) and len(bl) > best_len:
                best, best_len = boss, len(bl)
        # Mapping is authoritative: if the monster isn't a tracked boss
        # (e.g. slayer-monster tasks), the task has no boss bucket.
        return best
    # Longest boss-name substring match wins (avoids 'nex' matching 'next')
    best, best_len = None, 0
    for boss in all_bosses:
        b = boss.lower()
        if b in lower and len(b) > best_len:
            best, best_len = boss, len(b)
    if best:
        return best
    # Fall back to keyword map
    for kw, boss in COMBAT_TASK_KEYWORDS.items():
        if kw in lower:
            return boss
    return None


SKILL_COLORS = {
    "Attack": "#C23B22", "Strength": "#4A7C59", "Defence": "#4A6FA5",
    "Ranged": "#6B9E3C", "Prayer": "#D4AF37", "Magic": "#6A5ACD",
    "Runecraft": "#E8C84A", "Construction": "#8B7355", "Hitpoints": "#C41E3A",
    "Agility": "#708090", "Herblore": "#2E8B57", "Thieving": "#6A0572",
    "Crafting": "#B8860B", "Fletching": "#228B22", "Slayer": "#DC143C",
    "Hunter": "#8B4513", "Mining": "#778899", "Smithing": "#CD853F",
    "Fishing": "#4682B4", "Cooking": "#FF6347", "Firemaking": "#FF4500",
    "Woodcutting": "#8B4513", "Farming": "#556B2F", "Dungeoneering": "#483D8B"
}

CATEGORY_LABELS = {
    "Levels": "Level-Ups",
    "Clue Scrolls": "Clue Scrolls",
    "Deaths": "Deaths",
    "Valuable Drops": "Valuable Drops",
    "Pets": "Pets",
    "Quests": "Quests",
    "Screenshots": "Manual Screenshots",
    "Killed": "PvP Kills",
    "Duels": "Duels",
    "Collection Log": "Collection Log",
    "BA Highscores": "Barbarian Assault",
    "Achievement Diary": "Achievement Diaries",
    "Combat Tasks": "Combat Tasks",
    "Combat tasks": "Combat Tasks",
    "Combat Achievements": "Combat Tasks",
    "Combat achievements": "Combat Tasks",
    "Boss Killed": "Boss Kills",
    "Untradeable Drops": "Untradeable Drops",
    "Untradeable drops": "Untradeable Drops",
    "Valuable drops": "Valuable Drops",
    "Collection log": "Collection Log",
}

CHART_COLORS = [
    "#e8631a", "#1a7ae8", "#2ecc71", "#e74c3c", "#9b59b6",
    "#f39c12", "#1abc9c", "#e67e22", "#3498db", "#e91e63",
    "#00bcd4", "#8bc34a", "#ff5722", "#607d8b"
]


def parse_timestamp(filename):
    # Older RuneLite format: "Pet 2018-12-19 at 10.41.47 AM.png"
    old_match = re.search(r'(\d{4}-\d{2}-\d{2}) at (\d{1,2}\.\d{2}\.\d{2}) (AM|PM)', filename)
    if old_match:
        try:
            dt_str = old_match.group(1) + ' ' + old_match.group(2) + ' ' + old_match.group(3)
            return datetime.strptime(dt_str, '%Y-%m-%d %I.%M.%S %p')
        except ValueError:
            pass

    patterns = [
        r'(\d{8}_\d{6})',
        r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})',
    ]
    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            ts = match.group(1).replace('-', '')
            try:
                return datetime.strptime(ts, '%Y%m%d_%H%M%S')
            except ValueError:
                try:
                    return datetime.strptime(ts[:8], '%Y%m%d')
                except ValueError:
                    pass
    return None


def parse_level_up(filename):
    patterns = [
        r'^(.+?)\((\d+)\)',
        r'^Level[ _]Up[ _](.+?)[ _]\((\d+)\)',
        r'^(.+?)[ _](\d+)_\d{8}',
    ]
    stem = Path(filename).stem
    for pattern in patterns:
        match = re.match(pattern, stem, re.IGNORECASE)
        if match:
            skill = match.group(1).strip().title()
            level = int(match.group(2))
            if any(s.lower() == skill.lower() for s in SKILLS):
                return skill.title(), level
    return None, None


HALL_OF_FAME_ITEMS = {
    # Capes
    "Infernal cape": ("🔥 Infernal Cape", "Hardest PvM cape in the game"),
    "Fire cape": ("🔴 Fire Cape", "TzTok-Jad"),
    "Quiver": ("🏹 Quiver", "Fortis Colosseum"),
    "Dizana's quiver": ("🏹 Dizana's Quiver", "Fortis Colosseum"),
    # Raids
    "Twisted bow": ("🏹 Twisted Bow", "Chambers of Xeric"),
    "Tumeken's shadow": ("🌑 Tumeken's Shadow", "Tombs of Amascut"),
    "Scythe of vitur": ("⚔️ Scythe of Vitur", "Theatre of Blood"),
    "Justiciar faceguard": ("🛡️ Justiciar Faceguard", "Theatre of Blood"),
    "Justiciar chestguard": ("🛡️ Justiciar Chestguard", "Theatre of Blood"),
    "Justiciar legguards": ("🛡️ Justiciar Legguards", "Theatre of Blood"),
    "Elder maul": ("🪓 Elder Maul", "Chambers of Xeric"),
    "Kodai wand": ("🪄 Kodai Wand", "Chambers of Xeric"),
    "Dragon hunter crossbow": ("🏹 Dragon Hunter Crossbow", "Chambers of Xeric"),
    # God Wars
    "Bandos chestplate": ("🛡️ Bandos Chestplate", "General Graardor"),
    "Bandos tassets": ("🛡️ Bandos Tassets", "General Graardor"),
    "Armadyl chestplate": ("🛡️ Armadyl Chestplate", "Kree'arra"),
    "Armadyl chainskirt": ("🛡️ Armadyl Chainskirt", "Kree'arra"),
    "Armadyl helmet": ("🪖 Armadyl Helmet", "Kree'arra"),
    "Zamorakian spear": ("⚔️ Zamorakian Spear", "K'ril Tsutsaroth"),
    "Steam battlestaff": ("🪄 Steam Battlestaff", "K'ril Tsutsaroth"),
    "Saradomin sword": ("⚔️ Saradomin Sword", "Commander Zilyana"),
    # Slayer
    "Hydra's claw": ("🐍 Hydra's Claw", "Alchemical Hydra"),
    "Brimstone ring": ("💍 Brimstone Ring", "Konar Slayer"),
    "Occult necklace": ("📿 Occult Necklace", "Smoke Devil"),
    "Trident of the swamp": ("🔱 Trident of the Swamp", "Zulrah"),
    "Tanzanite fang": ("🐍 Tanzanite Fang", "Zulrah"),
    "Serpentine visage": ("🐍 Serpentine Visage", "Zulrah"),
    "Imbued heart": ("💜 Imbued Heart", "Slayer"),
    # Other bosses
    "Dragon warhammer": ("🔨 Dragon Warhammer", "Lizardman Shamans"),
    "Abyssal whip": ("⚡ Abyssal Whip", "Abyssal Demon"),
    "Abyssal dagger": ("🗡️ Abyssal Dagger", "Abyssal Demon"),
    "Kraken tentacle": ("🐙 Kraken Tentacle", "Kraken"),
    "Pegasian crystal": ("💎 Pegasian Crystal", "Cerberus"),
    "Primordial crystal": ("💎 Primordial Crystal", "Cerberus"),
    "Eternal crystal": ("💎 Eternal Crystal", "Cerberus"),
    "Smolcano": ("🌋 Smolcano", "Zalcano"),
    "Voidwaker gem": ("💎 Voidwaker Gem", "Vet'ion/Calvar'ion"),
    "Tormented synapse": ("⚡ Tormented Synapse", "Tormented Demons"),
    "Virtus mask": ("🎭 Virtus Mask", "Desert Treasure 2 bosses"),
    "Virtus robe top": ("🎭 Virtus Robe Top", "Desert Treasure 2 bosses"),
    "Virtus robe bottom": ("🎭 Virtus Robe Bottom", "Desert Treasure 2 bosses"),
}


def parse_collection_log(filename):
    """Extract item name from a collection log filename.
    Format: 'Collection log (Item Name) YYYY-MM-DD_HH-MM-SS.png'
    """
    # Greedy item capture is intentional: item names can contain their own
    # parentheses (for example, "Master scroll book (empty)"). Anchor on the
    # final parenthesis immediately before the timestamp.
    match = re.match(
        r'Collection log \((.+)\)\s+\d{4}-\d{2}-\d{2}',
        Path(filename).stem,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return None


def parse_quest_name(filename):
    """Extract quest name from 'Quest(Quest Name) YYYY-MM-DD_HH-MM-SS.png'"""
    match = re.match(r'Quest\((.+?)\)', Path(filename).stem, re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_valuable_drop(filename):
    """Extract item name, quantity, and coin value from a valuable drop filename.
    Format: 'Valuable drop [qty x] item name (value coins) timestamp.png'
    """
    stem = Path(filename).stem
    match = re.match(r'Valuable drop (?:(\d+) x )?(.+?) \(([0-9,]+) coins\)', stem, re.IGNORECASE)
    if match:
        qty = int(match.group(1)) if match.group(1) else 1
        item = match.group(2).strip()
        value = int(match.group(3).replace(',', ''))
        return item, qty, value
    return None, 1, 0


HOME_MOMENT_CATEGORIES = {
    "Level-Ups", "Manual Screenshots", "Quests", "Clue Scroll Rewards",
    "Pets", "Valuable Drops", "Collection Log", "Untradeable Drops",
    "Combat Achievements",
}
HOME_MOMENT_WINDOW_DAYS = 90


def select_home_moments(gallery_items, limit=5):
    """Choose a current, varied set of screenshot-backed account moments.

    Selection stays deterministic and local: recency supplies the base score,
    account milestones add significance, and a one-per-category first pass
    prevents a single grind from taking over the full cover. Older evidence is
    considered only when the recent window cannot fill the requested set.
    """
    candidates = [
        item for item in gallery_items
        if item.get("timestamp") and item.get("category") in HOME_MOMENT_CATEGORIES
    ]
    if not candidates or limit <= 0:
        return []

    newest = max(item["timestamp"] for item in candidates)
    recent = [
        item for item in candidates
        if (newest - item["timestamp"]).days <= HOME_MOMENT_WINDOW_DAYS
    ]
    pool = recent if len(recent) >= limit else candidates

    def significance(item):
        category = item["category"]
        filename = item["filename"]
        if category == "Pets":
            return 90
        if category == "Valuable Drops":
            _, _, value = parse_valuable_drop(filename)
            if value >= 50_000_000:
                return 90
            if value >= 10_000_000:
                return 70
            if value >= 1_000_000:
                return 50
            return 30
        if category == "Level-Ups":
            _, level = parse_level_up(filename)
            if level == 99:
                return 100
            if level and level >= 90:
                return 45
            return 25
        if category == "Collection Log":
            item_name = parse_collection_log(filename)
            return 75 if item_name in HALL_OF_FAME_ITEMS else 40
        return {
            "Quests": 50,
            "Combat Achievements": 45,
            "Untradeable Drops": 35,
            "Clue Scroll Rewards": 25,
            "Manual Screenshots": 5,
        }.get(category, 0)

    ranked = sorted(
        pool,
        key=lambda item: (
            max(0, HOME_MOMENT_WINDOW_DAYS - (newest - item["timestamp"]).days) * 2
            + significance(item),
            item["timestamp"],
            item.get("rel_path", ""),
        ),
        reverse=True,
    )

    selected = []
    selected_paths = set()
    category_counts = defaultdict(int)
    for item in ranked:
        path = item.get("rel_path")
        if path in selected_paths or category_counts[item["category"]] >= 1:
            continue
        selected.append(item)
        selected_paths.add(path)
        category_counts[item["category"]] += 1
        if len(selected) == limit:
            return selected

    for item in ranked:
        path = item.get("rel_path")
        if path in selected_paths:
            continue
        selected.append(item)
        selected_paths.add(path)
        if len(selected) == limit:
            break
    return selected


def parse_untradeable_drop(filename):
    """Extract item name and quantity from an untradeable drop filename.
    Format: 'Untradeable drop  [qty x] item name YYYY-MM-DD_HH-MM-SS.png'
    Note the double space after 'drop' in RuneLite's naming convention.
    """
    stem = Path(filename).stem
    # Strip trailing timestamp (YYYY-MM-DD_HH-MM-SS) to isolate item portion
    match = re.match(r'Untradeable drop\s+(?:(\d+) x )?(.+?)\s+\d{4}-\d{2}-\d{2}', stem, re.IGNORECASE)
    if match:
        qty = int(match.group(1)) if match.group(1) else 1
        item = match.group(2).strip()
        return item, qty
    return None, 1


def scan_screenshots(base_path, merge_folders=None):
    """Read one account's screenshots, pooling any folders merged into it.

    `merge_folders` holds folders the user declared to be the same account
    under a former name. They are read as part of this account's history —
    never a different game mode, and never a different account, both of which
    keep their own dashboards.
    """
    data = {
        "categories": defaultdict(list),
        "level_ups": defaultdict(list),
        "timeline": defaultdict(int),
        "total": 0,
        "first_screenshot": None,
        "last_screenshot": None,
        "gallery_items": [],
        "drops": [],          # parsed valuable drops
        "hall_of_fame": [],   # prestigious collection log unlocks
        "combat_tasks": [],   # combat achievement screenshots
    }

    base = Path(base_path)
    if not base.exists():
        print(f"ERROR: Path not found: {base_path}")
        return data

    all_timestamps = []

    # Roots to read, as (folder, prefix) pairs. The primary folder is where the
    # dashboard is written, so its screenshots keep the bare relative paths they
    # have always had — which is what lets existing favorites survive a merge
    # untouched. Folders merged in under a former name sit beside it, so they
    # are reached with a `../OldName/` prefix.
    #
    # These are real relative paths rather than a virtual scheme on purpose:
    # the generated HTML has to keep rendering when it is opened straight from
    # disk with no local service running (DESIGN.md non-negotiable #17), and a
    # made-up path would only resolve through the service.
    roots = [(base, "")]
    for folder in merge_folders or []:
        folder = Path(folder)
        if not folder.is_dir() or folder.resolve() == base.resolve():
            continue
        roots.append((folder, f"../{folder.name}/"))

    # Collect first, then sort across every root together. Sorting each folder
    # separately would interleave wrongly and make the newest screenshot in the
    # merged history depend on which folder it happened to live in.
    found = []
    for root, prefix in roots:
        for item in root.rglob("*.png"):
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            found.append((mtime, item, root, prefix))
    found.sort(key=lambda row: row[0], reverse=True)

    # A user who followed the documented workaround — hand-copying an old
    # folder's screenshots into the current one — and then also declares that
    # old folder would otherwise see every shared screenshot twice, inflating
    # counts, drop totals and wealth. RuneLite filenames carry their own
    # timestamp, so category plus filename identifies a screenshot well enough
    # to catch that. The primary folder is scanned first within any tie, so the
    # copy that keeps its short path wins.
    merging = len(roots) > 1
    seen_keys = set()
    duplicates = 0

    for _mtime, item, root, prefix in found:
        if item.name == "osrs_dashboard.html":
            continue

        rel = item.relative_to(root)
        parts = rel.parts
        category_raw = parts[0] if len(parts) > 1 else "Screenshots"
        category = CATEGORY_LABELS.get(category_raw, category_raw)

        if merging:
            key = (category_raw.lower(), item.name.lower())
            if key in seen_keys:
                duplicates += 1
                continue
            seen_keys.add(key)

        ts = parse_timestamp(item.name)
        ts_str = ts.strftime("%b %d, %Y · %I:%M %p") if ts else ""
        ts_sort = ts.isoformat() if ts else ""

        # Use a relative path so the HTML works from the same folder
        rel_path = prefix + str(rel).replace("\\", "/")

        entry = {
            "filename": item.name,
            "path": str(item),
            "rel_path": rel_path,
            # Stable across recompositions; `rel_path` is not. See #16.
            "fav_key": favorite_key(root.name, rel),
            "timestamp": ts,
            "ts_str": ts_str,
            "ts_sort": ts_sort,
            "category": category,
            "category_raw": category_raw,
        }

        data["categories"][category].append(entry)
        data["gallery_items"].append(entry)
        data["total"] += 1

        if ts:
            all_timestamps.append(ts)
            month_key = ts.strftime("%Y-%m")
            data["timeline"][month_key] += 1

        if category_raw == "Levels":
            skill, level = parse_level_up(item.name)
            if skill and level:
                data["level_ups"][skill].append((level, ts))

        if category_raw.lower() == "collection log":
            clog_item = parse_collection_log(item.name)
            if clog_item:
                key = clog_item.lower()
                for hof_key, (hof_label, hof_source) in HALL_OF_FAME_ITEMS.items():
                    if hof_key.lower() == key:
                        data["hall_of_fame"].append({
                            "label": hof_label,
                            "source": hof_source,
                            "item": clog_item,
                            "timestamp": ts,
                            "ts_str": ts.strftime("%b %d, %Y") if ts else "",
                            "rel_path": rel_path,
                        })
                        break

        if category_raw.lower() == "valuable drops":
            drop_item, drop_qty, drop_value = parse_valuable_drop(item.name)
            if drop_item:
                data["drops"].append({
                    "item": drop_item,
                    "qty": drop_qty,
                    "value": drop_value,
                    "timestamp": ts,
                    "rel_path": rel_path,
                    "ts_str": ts.strftime("%b %d, %Y") if ts else "",
                })

        if category_raw.lower() in ("combat tasks", "combat achievements"):
            task_name = parse_combat_task(item.name)
            if task_name:
                data["combat_tasks"].append({
                    "task": task_name,
                    "rel_path": rel_path,
                    "ts_str": ts_str,
                })

    for skill in data["level_ups"]:
        data["level_ups"][skill].sort(key=lambda x: x[0])

    if all_timestamps:
        data["first_screenshot"] = min(all_timestamps)
        data["last_screenshot"] = max(all_timestamps)

    # Report the merge rather than performing it silently. A pooled scan
    # changes almost every number on the dashboard, so a user comparing against
    # what they saw yesterday needs to be told why — and a duplicate count is
    # the one figure that says whether their folders overlapped.
    if merging:
        merged_names = [prefix.strip("./") for _root, prefix in roots if prefix]
        data["merged_folders"] = merged_names
        data["merged_duplicates"] = duplicates
        print(f"Merged {len(merged_names)} folder(s) from earlier names: {', '.join(merged_names)}")
        if duplicates:
            print(f"  Skipped {duplicates} screenshot(s) already present in the current folder.")

    return data


def build_economic_acquisitions(data, component_overrides=None):
    """Normalize screenshot evidence for the indirect-value resolver."""
    events = []
    for drop in data.get("drops", []):
        if drop.get("timestamp"):
            events.append({
                "item": drop["item"],
                "qty": drop.get("qty", 1),
                "value": drop.get("value", 0),
                "timestamp": drop["timestamp"],
                "rel_path": drop.get("rel_path", ""),
                "source": "valuable",
            })
    for entry in data.get("categories", {}).get("Untradeable Drops", []):
        item, quantity = parse_untradeable_drop(entry["filename"])
        if item and entry.get("timestamp"):
            events.append({
                "item": item,
                "qty": quantity,
                "value": 0,
                "timestamp": entry["timestamp"],
                "rel_path": entry.get("rel_path", ""),
                "source": "untradeable",
            })
    for entry in data.get("categories", {}).get("Collection Log", []):
        item = parse_collection_log(entry["filename"])
        if item and entry.get("timestamp"):
            events.append({
                "item": item,
                "qty": 1,
                "value": 0,
                "timestamp": entry["timestamp"],
                "rel_path": entry.get("rel_path", ""),
                "source": "collection",
            })
    for item, override in (component_overrides or {}).items():
        # Accepted forms: {"Item": 1} or {"Item": {"qty": 1, "attested_on": "YYYY-MM-DD"}}.
        # The timestamp must be STABLE across refreshes: completed-recipe ledger
        # ids embed the completion timestamp, so a drifting attested timestamp
        # would re-date and re-price a frozen event on every refresh.
        attested_on = None
        if isinstance(override, dict):
            quantity = override.get("qty", 1)
            attested_on = override.get("attested_on")
        else:
            quantity = override
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue
        try:
            timestamp = datetime.strptime(str(attested_on), "%Y-%m-%d")
        except (TypeError, ValueError):
            # No (or bad) date given: use a fixed sentinel so the event is
            # deterministic. Completion dates prefer real screenshot evidence
            # (see economic_value.complete), so this date only ever surfaces
            # if every consumed component is attested.
            timestamp = datetime(2000, 1, 1)
        if isinstance(item, str) and item.strip() and quantity > 0:
            events.append({
                "item": item.strip(),
                "qty": quantity,
                "value": 0,
                "timestamp": timestamp,
                "rel_path": "",
                "source": "attested",
            })
    return events


def build_this_week_memories(gallery_items, chronicle_events, today=None,
                             window_days=3, max_per_year=6,
                             favorite_paths=None):
    """Curate prior-year screenshots from the current calendar week.

    Chronicle milestones receive first priority. Meaningful gallery categories
    fill the remaining slots, and the final list is round-robin ordered by year
    so the first page shows one memory from each available year instead of four
    screenshots from the same session.
    """
    today = today or datetime.now()
    favorite_paths = favorite_paths or set()
    anchor = datetime(2000, today.month, today.day)
    chronicle_by_src = {event.get("src"): event for event in chronicle_events if event.get("src")}
    by_year = defaultdict(list)

    for entry in gallery_items:
        ts = entry.get("timestamp")
        if not ts or ts.year >= today.year:
            continue
        candidate_anchor = datetime(2000, ts.month, ts.day)
        distance = abs((candidate_anchor - anchor).days)
        distance = min(distance, 366 - distance)
        if distance > window_days:
            continue

        category = entry.get("category", "")
        src = entry.get("rel_path", "")
        event = chronicle_by_src.get(src)
        title = ""
        sub = ""
        badge = ""
        color = "#c8a45a"
        score = 0

        if event:
            title = event.get("title", "")
            sub = event.get("sub", "")
            badge = event.get("badge", "MEMORY")
            color = event.get("color", color)
            score = {"skill": 120, "pet": 115, "drop": 110, "quest": 105}.get(event.get("type"), 100)
        elif category == "Collection Log":
            item = parse_collection_log(entry["filename"])
            if item:
                title, sub, badge, score = item, "Collection Log", "LOG", 90
        elif category == "Valuable Drops":
            item, qty, value = parse_valuable_drop(entry["filename"])
            if item:
                qty_prefix = f"{qty}x " if qty > 1 else ""
                title, sub, badge, score = qty_prefix + item, "Valuable Drop", "DROP", 86
        elif category == "Level-Ups":
            skill, level = parse_level_up(entry["filename"])
            if skill and level:
                title, sub, badge = f"{skill} {level}", "Level Up", "LEVEL"
                score = 100 if level in (90, 92, 99) else 78
                color = SKILL_COLORS.get(skill, color)
        elif category == "Combat Tasks":
            task = parse_combat_task(entry["filename"])
            if task:
                title, sub, badge, score = task, "Combat Achievement", "TASK", 76
        elif category == "Clue Scroll Rewards":
            clue = re.match(r"([A-Za-z]+)\((\d+)\)", Path(entry["filename"]).stem)
            if clue:
                title = f"{clue.group(1)} clue #{clue.group(2)}"
                sub, badge, score = "Clue Scroll", "CLUE", 55
        elif category == "Untradeable Drops":
            item, qty = parse_untradeable_drop(entry["filename"])
            if item:
                qty_prefix = f"{qty}x " if qty > 1 else ""
                title, sub, badge, score = qty_prefix + item, "Untradeable Drop", "DROP", 48
        elif category == "Manual Screenshots":
            title, sub, badge, score = "A saved moment", "Manual Screenshot", "MEMORY", 35

        if not title or not src:
            continue

        # Explicit user curation outranks inferred importance while preserving
        # the anniversary-window contract for This Week in Gielinor. Matched on
        # the stable key, not the renderable path, so a recomposed account
        # keeps promoting the same screenshots.
        if entry.get("fav_key", "") in favorite_paths:
            score += 200
        score += (window_days - distance) * 2
        years_ago = today.year - ts.year
        by_year[ts.year].append({
            "title": title,
            "sub": sub,
            "badge": badge,
            "color": color,
            "src": src,
            "ts": ts,
            "ts_str": ts.strftime("%b %d, %Y"),
            "year": ts.year,
            "years_ago": years_ago,
            "years_ago_str": f"{years_ago} year{'s' if years_ago != 1 else ''} ago",
            "score": score,
        })

    years = sorted(by_year, reverse=True)
    for year in years:
        seen_titles = set()
        ranked = []
        for memory in sorted(by_year[year], key=lambda item: (-item["score"], -item["ts"].timestamp())):
            key = memory["title"].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            ranked.append(memory)
            if len(ranked) >= max_per_year:
                break
        by_year[year] = ranked

    memories = []
    max_rank = max((len(items) for items in by_year.values()), default=0)
    for rank in range(max_rank):
        for year in years:
            if rank < len(by_year[year]):
                memory = dict(by_year[year][rank])
                memory.pop("score", None)
                memory.pop("ts", None)
                memory["idx"] = len(memories)
                memories.append(memory)

    start = today - timedelta(days=window_days)
    end = today + timedelta(days=window_days)
    if start.month == end.month:
        window_label = f"{start.strftime('%b')} {start.day}–{end.day}"
    else:
        window_label = f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}"
    return memories, window_label


def build_html(data, hiscores=None, xp_history=None, favorite_paths=None,
               economic_value=None, discovery_summary=None):
    chart_js = load_chart_js()
    font_css = load_dashboard_font_css()
    if hiscores is None:
        hiscores = {}
    if xp_history is None:
        xp_history = []
    favorite_paths = favorite_paths or set()
    economic_value = economic_value or {
        "events": [], "realized_total": 0, "pending": [], "latent_total": 0,
        "prices_updated_at": None, "prices_stale": True,
    }
    discovery_summary = discovery_summary or {}
    refreshed_at_str = datetime.now().strftime("%b %d, %Y · %I:%M %p")

    # Category chart
    cat_labels, cat_counts = [], []
    for cat, entries in sorted(data["categories"].items(), key=lambda x: -len(x[1])):
        cat_labels.append(cat)
        cat_counts.append(len(entries))

    # Timeline — fill every month between first and last so year labels are evenly spaced
    _raw_timeline = sorted(data["timeline"].items())
    if _raw_timeline:
        _cur = datetime.strptime(_raw_timeline[0][0], "%Y-%m")
        _end = datetime.strptime(_raw_timeline[-1][0], "%Y-%m")
        _full: dict = {}
        while _cur <= _end:
            _key = _cur.strftime("%Y-%m")
            _full[_key] = data["timeline"].get(_key, 0)
            _cur = (_cur.replace(day=1) + __import__('datetime').timedelta(days=32)).replace(day=1)
        timeline_sorted = sorted(_full.items())
    else:
        timeline_sorted = _raw_timeline
    tl_labels = [k for k, v in timeline_sorted]
    tl_values = [v for k, v in timeline_sorted]
    # Display labels: show the year only at January of each year, empty string otherwise
    tl_display_labels = [lbl[:4] if lbl.endswith('-01') else '' for lbl in tl_labels]

    # Wealth over time — direct valuable drops plus frozen indirect-value
    # completions. Every calendar month is represented so horizontal distance
    # remains honest even during quiet stretches.
    gp_by_month = defaultdict(int)
    gp_by_day = defaultdict(int)
    for d in data["drops"]:
        if d["timestamp"]:
            gp_by_month[d["timestamp"].strftime("%Y-%m")] += d["value"]
            gp_by_day[d["timestamp"].strftime("%Y-%m-%d")] += d["value"]
    for event in economic_value.get("events", []):
        try:
            event_dt = datetime.fromisoformat(event["completed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        event_month = event_dt.strftime("%Y-%m")
        gp_by_month[event_month] += int(event.get("value", 0))
        gp_by_day[event_dt.strftime("%Y-%m-%d")] += int(event.get("value", 0))

    # Pending components can predate the first realized GP event. Include their
    # acquisition months so all-time view has a real dot for that evidence.
    pending_evidence_months = []
    for pending in economic_value.get("pending", []):
        for part in pending.get("detail", []):
            for proof in part.get("evidence", []):
                proof_month = str(proof.get("timestamp", ""))[:7]
                if len(proof_month) == 7:
                    pending_evidence_months.append(proof_month)

    gp_months_sorted = []
    wealth_month_keys = list(gp_by_month) + pending_evidence_months
    if wealth_month_keys:
        first_month = datetime.strptime(min(wealth_month_keys), "%Y-%m")
        last_key = max(wealth_month_keys)
        if data.get("last_screenshot"):
            last_key = max(last_key, data["last_screenshot"].strftime("%Y-%m"))
        last_month = datetime.strptime(last_key, "%Y-%m")
        cursor = first_month
        while cursor <= last_month:
            key = cursor.strftime("%Y-%m")
            gp_months_sorted.append((key, gp_by_month.get(key, 0)))
            cursor = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
    gp_running = 0
    gp_tl_labels, gp_tl_values, gp_monthly_values = [], [], []
    for month, val in gp_months_sorted:
        gp_running += val
        dt = datetime.strptime(month, "%Y-%m")
        gp_tl_labels.append(dt.strftime("%b %y"))
        gp_tl_values.append(gp_running)
        gp_monthly_values.append(val)

    # Per-month contributors: what actually produced each month's GP, with the
    # screenshot path so the chart tooltip can show the evidence.
    contrib_by_month = defaultdict(list)
    contrib_by_day = defaultdict(list)
    for d in data["drops"]:
        if d["timestamp"] and d["value"] > 0:
            qty_prefix = f"{d['qty']}x " if d.get("qty", 1) > 1 else ""
            label = qty_prefix + d["item"]
            rel_path = d.get("rel_path", "")
            screenshot = {
                "s": rel_path,
                "l": label,
                "ts": d.get("ts_str", ""),
            }
            contributor = {
                "l": label,
                "v": int(d["value"]),
                "s": rel_path,
                "ts": d.get("ts_str", ""),
                "shots": [screenshot] if rel_path else [],
            }
            contrib_by_month[d["timestamp"].strftime("%Y-%m")].append(contributor)
            contrib_by_day[d["timestamp"].strftime("%Y-%m-%d")].append(contributor)
    for event in economic_value.get("events", []):
        try:
            event_dt = datetime.fromisoformat(event["completed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        event_month = event_dt.strftime("%Y-%m")
        label = event.get("label", "Completed assembly") + " (assembled)"
        evidence = []
        for proof in event.get("evidence", []):
            rel_path = str(proof.get("rel_path") or "").strip()
            if not rel_path:
                continue
            try:
                proof_date = datetime.fromisoformat(proof.get("timestamp", "")).strftime("%b %d, %Y")
            except (TypeError, ValueError):
                proof_date = ""
            evidence.append({
                "s": rel_path,
                "l": label + " — " + proof.get("item", "component"),
                "ts": proof_date,
            })
        contributor = {
            "l": label,
            "v": int(event.get("value", 0)),
            "s": evidence[0]["s"] if evidence else "",
            "ts": evidence[0]["ts"] if evidence else "",
            "shots": evidence,
        }
        contrib_by_month[event_month].append(contributor)
        contrib_by_day[event_dt.strftime("%Y-%m-%d")].append(contributor)
    def pack_wealth_contributors(raw_entries):
        # A named tooltip contributor must always have something the user can
        # open. Aggregate totals can still include ledger value, but the info
        # box never advertises a row with no screenshot behind it.
        entries = sorted(
            (entry for entry in raw_entries if entry.get("shots")),
            key=lambda e: -e["v"],
        )
        return {
            "top": entries[:4],
            "items": entries,
            "more_n": len(entries[4:]),
            "more_v": sum(e["v"] for e in entries[4:]),
        }

    gp_tl_contrib = [
        pack_wealth_contributors(contrib_by_month.get(month, []))
        for month, _val in gp_months_sorted
    ]

    # Pending-assembly value over time: each incomplete recipe earns progress
    # in the month a component actually dropped, valued at current prices
    # (there is no historical price feed, and the chart says so).
    gp_tl_pending = None
    gp_tl_pending_detail = None
    pending_recipes = [
        p for p in economic_value.get("pending", [])
        if p.get("net_output") is not None and p.get("need")
    ]

    def pending_snapshot(period_key):
        """Return pending total/detail as of a month or day ISO key."""
        key_len = len(period_key)
        period_total = 0
        period_detail = []
        for pending in pending_recipes:
            satisfied = 0
            evidence = []
            attested = []
            for part in pending.get("detail", []):
                available = [
                    proof for proof in part.get("evidence", [])
                    if str(proof.get("timestamp", ""))[:key_len] <= period_key
                ]
                selected = available[:int(part.get("need", 0))]
                satisfied += len(selected)
                for proof in selected:
                    # Progress is cumulative, but evidence belongs only to the
                    # exact month/day when that component was acquired.
                    if str(proof.get("timestamp", ""))[:key_len] != period_key:
                        continue
                    rel_path = str(proof.get("rel_path") or "").strip()
                    if rel_path:
                        try:
                            proof_date = datetime.fromisoformat(
                                proof.get("timestamp", "")
                            ).strftime("%b %d, %Y")
                        except (TypeError, ValueError):
                            proof_date = ""
                        evidence.append({
                            "s": rel_path,
                            "l": pending["label"] + " (pending) — " + part["item"],
                            "ts": proof_date,
                            "iso": proof.get("timestamp", ""),
                        })
                    elif proof.get("attested"):
                        attested.append(part["item"])
            if satisfied <= 0:
                continue
            evidence.sort(key=lambda proof: proof.get("iso", ""), reverse=True)
            for proof in evidence:
                proof.pop("iso", None)
            contribution = int(round(
                pending["net_output"] * satisfied / pending["need"]
            ))
            period_total += contribution
            period_detail.append({
                "l": pending["label"], "v": contribution,
                "h": satisfied, "n": pending["need"],
                "shots": evidence, "a": sorted(set(attested)),
            })
        return period_total, period_detail

    if pending_recipes and gp_months_sorted:
        gp_tl_pending = []
        gp_tl_pending_detail = []
        for month, _val in gp_months_sorted:
            month_total, month_detail = pending_snapshot(month)
            gp_tl_pending.append(month_total)
            gp_tl_pending_detail.append(month_detail)

    # The 1 Month control uses a true rolling daily series. Keep 31 calendar
    # points (today/last screenshot plus the prior 30 days) while preserving
    # the lifetime realized baseline underneath that window.
    gp_daily_payload = None
    if gp_by_day or pending_recipes:
        daily_end = (
            data["last_screenshot"].date()
            if data.get("last_screenshot") else datetime.now().date()
        )
        daily_start = daily_end - timedelta(days=30)
        daily_start_key = daily_start.isoformat()
        daily_running = sum(
            value for day_key, value in gp_by_day.items()
            if day_key < daily_start_key
        )
        daily_baseline = daily_running
        daily_keys = []
        daily_labels = []
        daily_values = []
        daily_gains = []
        daily_contrib = []
        daily_pending = []
        daily_pending_detail = []
        cursor = daily_start
        while cursor <= daily_end:
            day_key = cursor.isoformat()
            day_gain = int(gp_by_day.get(day_key, 0))
            daily_running += day_gain
            pending_total, pending_detail = pending_snapshot(day_key)
            daily_keys.append(day_key)
            daily_labels.append(cursor.strftime("%b %d"))
            daily_values.append(daily_running)
            daily_gains.append(day_gain)
            daily_contrib.append(pack_wealth_contributors(
                contrib_by_day.get(day_key, [])
            ))
            daily_pending.append(pending_total)
            daily_pending_detail.append(pending_detail)
            cursor += timedelta(days=1)
        gp_daily_payload = {
            "keys": daily_keys,
            "labels": daily_labels,
            "cumulative": daily_values,
            "monthly": daily_gains,
            "baseline": daily_baseline,
            "potential": (
                [realized + pending for realized, pending
                 in zip(daily_values, daily_pending)]
                if any(daily_pending) else None
            ),
            "pend": daily_pending_detail if any(daily_pending) else None,
            "contrib": daily_contrib,
        }

    # Unpack hiscores sections
    hs_skills = hiscores.get("skills", {})
    hs_clues = hiscores.get("clues", {})
    hs_bosses = hiscores.get("bosses", {})

    def fmt_gp(v):
        if v >= 1_000_000_000:
            return f"{v/1_000_000_000:.2f}B"
        if v >= 1_000_000:
            return f"{v/1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v/1_000:.0f}K"
        return f"{v:,}"

    # Skills — prefer live hiscores, fall back to screenshot-derived levels
    screenshot_levels = {s: max(l for l, _ in lvls) for s, lvls in data["level_ups"].items()}
    skill_max_levels = {}
    for skill in HISCORES_SKILLS:
        if skill in hs_skills:
            skill_max_levels[skill] = hs_skills[skill]["level"]
        elif skill in screenshot_levels:
            skill_max_levels[skill] = screenshot_levels[skill]

    # Sort skills by level for the chart
    skill_items = sorted(skill_max_levels.items(), key=lambda x: x[1])
    skill_labels = [s for s, _ in skill_items]
    skill_values = [l for _, l in skill_items]
    skill_bar_colors = [SKILL_COLORS.get(s, "#c8a45a") for s in skill_labels]

    # Total level and XP
    total_level = sum(skill_max_levels.values())
    total_xp = sum(v["xp"] for v in hs_skills.values()) if hs_skills else 0

    # 99 Timeline — one entry per skill at level 99. Prefer the level-up
    # screenshot (with date) when we have one; otherwise fall back to hiscores
    # and mark the date as unknown so older 99s still get represented.
    nineties = []
    skills_with_dated_99 = set()
    for skill, levels in data["level_ups"].items():
        for level, ts in levels:
            if level == 99 and ts:
                nineties.append({
                    "skill": skill,
                    "ts": ts,
                    "ts_str": ts.strftime("%b %d, %Y"),
                    "color": SKILL_COLORS.get(skill, "#c8a45a"),
                })
                skills_with_dated_99.add(skill)

    # Add any 99s known via hiscores but without a screenshot — these are 99s
    # the player hit before they were tracking screenshots, so we know the
    # achievement but not when.
    for skill, lvl in skill_max_levels.items():
        if lvl >= 99 and skill not in skills_with_dated_99:
            nineties.append({
                "skill": skill,
                "ts": None,
                "ts_str": "Date unknown",
                "color": SKILL_COLORS.get(skill, "#c8a45a"),
            })

    # Newest first. Undated entries sink to the bottom (they're the oldest by
    # nature — pre-tracking 99s).
    def _nineties_sort(n):
        if n["ts"] is None:
            return (1, 0)
        return (0, -n["ts"].timestamp())
    nineties.sort(key=_nineties_sort)

    nineties_json = [{"skill": n["skill"], "ts_str": n["ts_str"], "color": n["color"]} for n in nineties]

    # Road to Max — XP-based truth. Levels lie: level 92 is the halfway point
    # of a skill, so progress bars use XP, not levels. Live hiscores XP when
    # available; falls back to the latest history snapshot (offline refresh),
    # then to level-based estimates as a last resort.
    xp_now = {s: v["xp"] for s, v in hs_skills.items() if v.get("xp", -1) >= 0}
    if not xp_now and xp_history:
        xp_now = dict(xp_history[-1].get("xp", {}))
    pace_rates, pace_span = compute_pace(xp_history)

    # Active skills: manual ACTIVE_SKILLS override wins if set; otherwise
    # inferred dynamically from the snapshot history — the top XP gainers
    # over the pace window get the ⚡ badge. The 100k floor filters out
    # incidental XP (quest rewards, clue steps); capped at 3 so a scattered
    # month doesn't badge half the list. No history yet = no badges, which
    # resolves itself as snapshots accumulate.
    max_progress_rates = {
        skill: rate for skill, rate in pace_rates.items()
        if skill_max_levels.get(skill, 0) < 99
    }
    qualified_max_progress_rates = {
        skill: rate for skill, rate in max_progress_rates.items()
        if rate * pace_span >= ACTIVE_XP_FLOOR
    }
    if ACTIVE_SKILLS:
        _active_set = {
            skill for skill in ACTIVE_SKILLS
            if skill_max_levels.get(skill, 0) < 99
        }
    else:
        _gains = {s: r * pace_span for s, r in max_progress_rates.items()}
        _active_set = {s for s, g in sorted(_gains.items(), key=lambda x: -x[1])[:3]
                       if g >= ACTIVE_XP_FLOOR}

    road_to_max = []
    total_xp_remaining = 0
    for skill, level in skill_max_levels.items():
        if level >= 99:
            continue
        color = SKILL_COLORS.get(skill, "#c8a45a")
        xp = xp_now.get(skill)
        if xp is not None:
            xp_remaining = max(MAX_XP - xp, 0)
            pct = round(xp / MAX_XP * 100, 1)
        else:
            xp_remaining = None
            pct = round((level / 99) * 100, 1)
        if xp_remaining:
            total_xp_remaining += xp_remaining
        rate = pace_rates.get(skill, 0)
        eta_days = None
        if xp_remaining and rate > 0:
            eta_days = int(xp_remaining / rate + 0.5)
        eta_str = ""
        if eta_days and eta_days <= ETA_USEFUL_MAX_DAYS:
            eta_str = (datetime.now() + timedelta(days=eta_days)).strftime("%b %Y")
        if eta_str:
            eta_label = f"~{eta_str}"
        else:
            eta_label = "Not currently training"
        road_to_max.append({
            "skill": skill,
            "level": level,
            "remaining": 99 - level,
            "pct": pct,
            "color": color,
            "xp_remaining": xp_remaining if xp_remaining is not None else -1,
            "xp_rem_str": fmt_gp(xp_remaining) if xp_remaining else "",
            "rate": int(rate) if rate else 0,
            "eta_str": eta_str,
            "eta_label": eta_label,
            "active": skill in _active_set,
        })

    # Sort every remaining skill consistently: active grinds first, then by
    # XP remaining ascending (closest 99 first).
    def _rtm_sort_key(s):
        tier = 0 if s["active"] else 1
        # -1 means no XP data — sink below skills with real numbers
        rem = s["xp_remaining"] if s["xp_remaining"] >= 0 else MAX_XP * 2
        return (tier, rem)
    road_to_max.sort(key=_rtm_sort_key)
    road_to_max_json = road_to_max

    # Headline projections for the Road to Max tab
    total_rate = sum(qualified_max_progress_rates.values())
    max_eta_str = ""
    max_eta_label = "Building History"
    if total_xp_remaining and total_rate > 0 and pace_span >= PACE_EARLY_MIN_DAYS:
        max_days = int(total_xp_remaining / total_rate + 0.5)
        max_eta_str = (datetime.now() + timedelta(days=max_days)).strftime("%B %Y")
        max_eta_label = "Projected Max" if pace_span >= PACE_WINDOW_DAYS else "Early Estimate"
    pace_headline = fmt_gp(int(total_rate)) if total_rate > 0 else "—"
    total_xp_rem_str = fmt_gp(total_xp_remaining) if total_xp_remaining else "—"
    if qualified_max_progress_rates and pace_span < PACE_EARLY_MIN_DAYS:
        pace_note_html = f'<p class="rtm-pace-note">Pace measured over the last {pace_span} days. The max forecast unlocks at 7 days and settles into a rolling 14-day view.</p>'
    elif qualified_max_progress_rates and pace_span < PACE_WINDOW_DAYS:
        pace_note_html = f'<p class="rtm-pace-note">Early estimate from {pace_span} days of XP snapshots. It becomes the rolling 14-day projection as history fills in.</p>'
    elif qualified_max_progress_rates:
        pace_note_html = f'<p class="rtm-pace-note">Projection uses the most recent {pace_span} days inside the rolling 14-day window. ETAs assume that recent pace holds.</p>'
    elif max_progress_rates:
        max_eta_label = "No recent max progress"
        pace_note_html = f'<p class="rtm-pace-note">Recent XP in unmaxed skills was too small to treat as an active maxing pace.</p>'
    elif pace_rates:
        max_eta_label = "No recent max progress"
        pace_note_html = f'<p class="rtm-pace-note">Recent XP was earned only in already-maxed skills, so it does not move the max projection.</p>'
    else:
        pace_note_html = '<p class="rtm-pace-note">Pace tracking builds automatically. Refresh on different days to establish a recent trajectory; the max forecast unlocks at 7 days.</p>'
    # Daily XP gained — the road-to-max story is velocity, not the near-flat
    # cumulative total (which is so large that a strong session barely nudges
    # the line and every axis label rounds to the same "409M"). Each bar is
    # XP/day over the interval since the prior snapshot, normalized by calendar
    # days so an irregular refresh cadence doesn't distort bar heights. The raw
    # gain and span ride along for the tooltip.
    xp_trend = []
    _prev_total = None
    _prev_date = None
    for h in xp_history:
        _total = sum(h.get("xp", {}).values())
        _date = h.get("date", "")
        if _prev_total is not None:
            try:
                _days = (datetime.strptime(_date, "%Y-%m-%d")
                         - datetime.strptime(_prev_date, "%Y-%m-%d")).days
            except (ValueError, TypeError):
                _days = 0
            if _days < 1:
                _days = 1
            _gained = _total - _prev_total
            xp_trend.append({
                "date": _date,
                "total": _total,
                "gained": _gained,
                "per_day": int(round(_gained / _days)),
                "days": _days,
            })
        _prev_total = _total
        _prev_date = _date

    # Account Pulse turns the widened daily snapshots into a recent activity
    # story. Both windows use the same source and calculation path so the hero
    # cards and breakdown rows always reconcile.
    account_pulse_json = json.dumps({
        "7": compute_account_pulse(xp_history, 7),
        "30": compute_account_pulse(xp_history, 30),
    })

    # Milestones
    milestones = []
    for skill, levels in data["level_ups"].items():
        for level, ts in levels:
            if level in [50, 70, 80, 90, 92, 99]:
                milestones.append({
                    "text": f"{skill} {level}",
                    "ts": ts.strftime("%b %d, %Y") if ts else "Unknown",
                    "sort": ts or datetime.min,
                    "level": level
                })
    milestones.sort(key=lambda x: x["sort"])

    # Pets — compact clickable thumbnails
    pets = data["categories"].get("Pets", [])
    pets_json_data = []
    pet_html = ""
    if pets:
        pets_sorted = sorted(pets, key=lambda p: p["timestamp"] or datetime.min)
        for i, p in enumerate(pets_sorted):
            date_str = p["timestamp"].strftime("%b %d, %Y") if p["timestamp"] else ""
            pets_json_data.append({"src": p["rel_path"], "ts": date_str, "label": "Pet drop #" + str(i + 1)})
            pet_html += ('<div class="pet-thumb" onclick="openPetItem(' + str(i) + ')">'
                         + '<img src="' + p["rel_path"] + '" alt="" loading="lazy">'
                         + '<div class="pet-thumb-date">' + date_str + '</div>'
                         + '</div>')
    else:
        pet_html = '<p class="empty-note">No pet screenshots found yet.</p>'
    pets_json = json.dumps(pets_json_data)
    # The folder every un-prefixed relative path belongs to, so the page
    # can derive a stable favourite key without every item carrying one.
    primary_folder_json = json.dumps(Path(SCREENSHOTS_PATH).name)

    # Clues — compact stat row. Tier colors follow Destiny's engram rarity:
    # Medium=green, Hard=blue, Elite=purple, Master=gold.
    tier_colors = {"Beginner": "#aaa", "Easy": "#2ecc71",
                   "Medium": "#2ecc71",   # green
                   "Hard":   "#3498db",   # blue
                   "Elite":  "#9b59b6",   # purple
                   "Master": "#d4af37"}   # gold
    clue_tiers = {}
    if hs_clues:
        tier_map = {"beginner": "Beginner", "easy": "Easy", "medium": "Medium",
                    "hard": "Hard", "elite": "Elite", "master": "Master"}
        for key, count in hs_clues.items():
            for t, label in tier_map.items():
                if t in key.lower() and "all" not in key.lower():
                    clue_tiers[label] = count
    else:
        for entry in data["categories"].get("Clue Scrolls", []):
            fname = entry["filename"].lower()
            for tier in ["beginner", "easy", "medium", "hard", "elite", "master"]:
                if tier in fname:
                    clue_tiers[tier.title()] = clue_tiers.get(tier.title(), 0) + 1
                    break

    clue_parts = []
    for tier in ["Beginner", "Easy", "Medium", "Hard", "Elite", "Master"]:
        count = clue_tiers.get(tier, 0)
        if count:
            clue_parts.append(
                '<span class="clue-inline-tier" style="color:' + tier_colors[tier] + '">' + tier + '</span>'
                + '<span class="clue-inline-count">' + f"{count:,}" + '</span>'
            )
    clue_html = '<p class="clue-inline-row">' + '<span class="clue-inline-sep"> · </span>'.join(clue_parts) + '</p>' if clue_parts else '<p class="empty-note">No clue data available.</p>'

    # Boss tab — build item→boss reverse lookup. Skip shared-drop items so
    # they end up in their virtual tile (Common Drops / Shared <X> Drops)
    # rather than an arbitrary specific boss.
    item_to_boss = {}
    for boss, items in BOSS_DROPS.items():
        for item in items:
            key = item.lower()
            if key in _ALL_SHARED_KEYS:
                continue
            item_to_boss[key] = boss

    # Map a category name → virtual boss that holds its shared drops
    CATEGORY_TO_VIRTUAL = {
        "Desert Treasure 2": "Shared DT2 Drops",
        "God Wars Dungeon":  "Shared GWD Drops",
        "Wilderness":        "Shared Wilderness Drops",
    }
    GLOBAL_VIRTUAL = "Common Drops"

    def resolve_target(item_lower):
        """Return (target_boss, source_label_or_None) for an item.

        Shared items go to their virtual tile with a source label; everything
        else falls through to the standard item_to_boss lookup.
        """
        if item_lower in _GLOBAL_SHARED_LOOKUP:
            return GLOBAL_VIRTUAL, _GLOBAL_SHARED_LOOKUP[item_lower]
        if item_lower in _CATEGORY_SHARED_LOOKUP:
            cat, src = _CATEGORY_SHARED_LOOKUP[item_lower]
            virtual = CATEGORY_TO_VIRTUAL.get(cat)
            if virtual:
                return virtual, src
        return item_to_boss.get(item_lower), None

    # Separate buckets: valuable drops, untradeables, collection log, combat achievements
    boss_drops_map       = defaultdict(list)
    boss_untradeable_map = defaultdict(list)
    boss_collection_map  = defaultdict(list)
    boss_ach_map         = defaultdict(list)
    boss_gp_total        = defaultdict(int)   # GP logged per boss (luck tab sort)

    # Track items already represented per boss to dedupe collection log against
    # valuable + untradeable screenshots. Keyed by (boss, item_lower).
    seen_items_per_boss = defaultdict(set)

    # Pre-count occurrences per (target, item) so duplicate drops collapse
    # into a single entry with "(×N)" appended to the label.
    def _count_per_target(entries, parser):
        counts = defaultdict(int)
        for entry in entries:
            result = parser(entry["filename"])
            item_name = result[0] if isinstance(result, tuple) else result
            if not item_name:
                continue
            tgt, _ = resolve_target(item_name.lower())
            if tgt:
                counts[(tgt, item_name.lower())] += 1
        return counts

    valuable_counts    = _count_per_target(data["categories"].get("Valuable Drops", []),    parse_valuable_drop)
    untradeable_counts = _count_per_target(data["categories"].get("Untradeable Drops", []), parse_untradeable_drop)
    collection_counts  = _count_per_target(data["categories"].get("Collection Log", []),    parse_collection_log)

    def _ts_asc(e):
        return e.get("timestamp") or datetime.min

    # Valuable drops: show every occurrence so you can see each moment in time,
    # newest first. No dedup here — only non-tradeables and collection log collapse.
    for entry in sorted(data["categories"].get("Valuable Drops", []), key=_ts_asc, reverse=True):
        item, qty, value = parse_valuable_drop(entry["filename"])
        if not item:
            continue
        item_l = item.lower()
        target, source = resolve_target(item_l)
        if not target:
            continue
        qty_str = f"{qty}x " if qty > 1 else ""
        drop_entry = {
            "src": entry["rel_path"],
            "label": qty_str + item,
            "value": fmt_gp(value) + " gp",
            "ts": entry["ts_str"].split("·")[0].strip() if entry["ts_str"] else "",
        }
        if source:
            drop_entry["source"] = source
        boss_drops_map[target].append(drop_entry)
        boss_gp_total[target] += value
        seen_items_per_boss[target].add(item_l)

    for entry in sorted(data["categories"].get("Untradeable Drops", []), key=_ts_asc):
        item, qty = parse_untradeable_drop(entry["filename"])
        if not item:
            continue
        item_l = item.lower()
        target, source = resolve_target(item_l)
        if not target or item_l in seen_items_per_boss[target]:
            continue
        count = untradeable_counts.get((target, item_l), 1)
        qty_str = f"{qty}x " if qty > 1 else ""
        count_suffix = f"  (×{count})" if count > 1 else ""
        drop_entry = {
            "src": entry["rel_path"],
            "label": qty_str + item + count_suffix,
            "ts": entry["ts_str"].split("·")[0].strip() if entry["ts_str"] else "",
        }
        if source:
            drop_entry["source"] = source
        boss_untradeable_map[target].append(drop_entry)
        seen_items_per_boss[target].add(item_l)

    # Collection Log slots — pull in items not already shown via valuable/untradeable.
    # This is how items like Eye of duke and Infernal cape land on their boss tabs.
    for entry in sorted(data["categories"].get("Collection Log", []), key=_ts_asc):
        item = parse_collection_log(entry["filename"])
        if not item:
            continue
        item_l = item.lower()
        target, source = resolve_target(item_l)
        if not target or item_l in seen_items_per_boss[target]:
            continue
        count = collection_counts.get((target, item_l), 1)
        count_suffix = f"  (×{count})" if count > 1 else ""
        drop_entry = {
            "src": entry["rel_path"],
            "label": item + count_suffix,
            "ts": entry["ts_str"].split("·")[0].strip() if entry["ts_str"] else "",
        }
        if source:
            drop_entry["source"] = source
        boss_collection_map[target].append(drop_entry)
        seen_items_per_boss[target].add(item_l)

    all_known_bosses = sorted(set(list(load_known_bosses()) + list(BOSS_DROPS.keys())))
    for ct in data["combat_tasks"]:
        boss = boss_for_combat_task(ct["task"], all_known_bosses)
        if boss:
            boss_ach_map[boss].append({
                "src": ct["rel_path"],
                "label": ct["task"],
                "ts": ct["ts_str"].split("·")[0].strip() if ct["ts_str"] else "",
            })

    # All bosses with any screenshot content — first sort by content volume,
    # then we'll group into categories below.
    bosses_with_content = set(list(boss_drops_map) + list(boss_untradeable_map)
                              + list(boss_collection_map) + list(boss_ach_map))

    def _content_count(b):
        return (len(boss_drops_map.get(b, []))
                + len(boss_untradeable_map.get(b, []))
                + len(boss_collection_map.get(b, []))
                + len(boss_ach_map.get(b, [])))

    # Virtual tiles for shared drops — always first within their category.
    VIRTUAL_BOSSES = {"Common Drops", "Shared DT2 Drops",
                      "Shared GWD Drops", "Shared Wilderness Drops"}

    def _sort_key(b):
        return (0 if b in VIRTUAL_BOSSES else 1, -_content_count(b))

    # Build the category-ordered list: walk BOSS_CATEGORIES in display order,
    # within each category sort bosses by content volume descending (virtuals
    # always rank above real bosses). Anything with content but no category
    # falls into "Other" at the bottom.
    ordered_bosses = []
    seen = set()
    for cat_name, cat_bosses in BOSS_CATEGORIES:
        in_cat = sorted(
            [b for b in cat_bosses if b in bosses_with_content],
            key=_sort_key,
        )
        for b in in_cat:
            ordered_bosses.append((b, cat_name))
            seen.add(b)
    leftovers = sorted(
        [b for b in bosses_with_content if b not in seen],
        key=lambda b: -_content_count(b)
    )
    for b in leftovers:
        ordered_bosses.append((b, "Other"))

    # Per-boss CA task totals from the wiki mapping — gives the detail panel
    # an honest denominator ("9 of 24 captured") instead of implying the
    # captured screenshots are the whole story.
    ca_totals = defaultdict(int)
    for _t in _CA_TASKS:
        _cb = boss_for_combat_task(_t, all_known_bosses)
        if _cb:
            ca_totals[_cb] += 1

    boss_cards_data = []
    for boss, category in ordered_bosses:
        _boss_drops = boss_drops_map.get(boss, [])[:10]
        _boss_untradeable = boss_untradeable_map.get(boss, [])[:10]
        _boss_collection = boss_collection_map.get(boss, [])[:10]
        _boss_achievements = boss_ach_map.get(boss, [])[:15]
        _boss_evidence = _boss_drops + _boss_untradeable + _boss_collection + _boss_achievements
        boss_cards_data.append({
            "boss": boss,
            "category": category,
            "kc": hs_bosses.get(boss, 0),
            "gp": boss_gp_total.get(boss, 0),
            "gp_str": fmt_gp(boss_gp_total.get(boss, 0)) if boss_gp_total.get(boss, 0) else "",
            "representative": _boss_evidence[0]["src"] if _boss_evidence else "",
            "evidence_count": len(_boss_evidence),
            "drops": _boss_drops,
            "untradeable": _boss_untradeable,
            "collection": _boss_collection,
            "achievements": _boss_achievements,
            "ca_captured": len(boss_ach_map.get(boss, [])),
            "ca_total": ca_totals.get(boss, 0),
        })

    boss_cards_json = json.dumps(boss_cards_data)
    boss_image_json = json.dumps({card["boss"]: card["representative"] for card in boss_cards_data if card["representative"]})

    # Exact screenshot evidence for owned Luck items. Multiple captures of the
    # same drop stay together so the lightbox can browse the complete history.
    luck_item_evidence = defaultdict(list)
    for _category, _parser in (
        ("Valuable Drops", parse_valuable_drop),
        ("Untradeable Drops", parse_untradeable_drop),
        ("Collection Log", parse_collection_log),
    ):
        for _entry in sorted(data["categories"].get(_category, []), key=_ts_asc):
            _parsed = _parser(_entry["filename"])
            _evidence_item = _parsed[0] if isinstance(_parsed, tuple) else _parsed
            if not _evidence_item:
                continue
            _evidence_boss, _source = resolve_target(_evidence_item.lower())
            if not _evidence_boss:
                continue
            _evidence_key = (_evidence_boss, _evidence_item.lower())
            _evidence_src = _entry["rel_path"]
            if any(existing["src"] == _evidence_src for existing in luck_item_evidence[_evidence_key]):
                continue
            luck_item_evidence[_evidence_key].append({
                "src": _evidence_src,
                "label": _evidence_item,
                "ts": _entry["timestamp"].strftime("%b %d, %Y") if _entry.get("timestamp") else "",
            })

    # ── Luck engine computation ──────────────────────────────────────────
    # For every measured boss: expected uniques by now (Σ 1-(1-p)^KC) vs
    # actually-owned uniques (from screenshot attribution). Bernoulli-sum
    # z-score → percentile. Shared items and unparseable rates are skipped;
    # excluded bosses and sub-floor KC never enter the math.
    luck_bosses = []
    _rarest = None
    _driest = None
    _tot_exp, _tot_act, _tot_var = 0.0, 0, 0.0
    for _lb, _rate_items in _DROP_RATES.items():
        if _lb in LUCK_EXCLUDED_BOSSES:
            continue
        _kc = hs_bosses.get(_lb, 0)
        if _kc < LUCK_MIN_KC:
            continue
        # Screenshot evidence plus user-attested loot (LUCK_OWNED_OVERRIDES —
        # for bosses farmed before the screenshot era, e.g. Kraken).
        _overrides = {k.lower(): v for k, v in LUCK_OWNED_OVERRIDES.get(_lb, {}).items()}
        _owned_set = set(seen_items_per_boss.get(_lb, set())) | set(_overrides)
        _items_out = []
        _b_exp, _b_act, _b_var = 0.0, 0, 0.0
        for _item, _rate_str in _rate_items.items():
            _il = _item.lower()
            if _il in _ALL_SHARED_KEYS:
                continue
            _p = parse_rate_string(_rate_str)
            if _p is None or _p > LUCK_MAX_P:
                continue
            _owned = _il in _owned_set
            if not _owned and _p > LUCK_MISSING_MAX_P:
                continue  # unowned mid-rarity: can't distinguish junk from unique
            _p_have = 1.0 - (1.0 - _p) ** _kc
            _copies = max(valuable_counts.get((_lb, _il), 0)
                          + untradeable_counts.get((_lb, _il), 0),
                          _overrides.get(_il, 0))
            _b_exp += _p_have
            _b_var += _p_have * (1.0 - _p_have)
            _b_act += 1 if _owned else 0
            _exp_n = _kc * _p
            # Poisson tail: what % of players at this KC would have as many
            # copies as you do? (Clog-only evidence counts as 1 copy.)
            _tail_pct = None
            if _owned:
                _c_eff = max(_copies, 1)
                _tail = _poisson_tail(_exp_n, _c_eff)
                _tail_pct = round(_tail * 100, 1)
            _items_out.append({
                "item": _item, "rate": _rate_str, "owned": _owned,
                "copies": _copies if _copies > 1 else 0,
                "p_have": round(_p_have * 100, 1),
                "exp_n": round(_exp_n, 1),
                "tail_pct": _tail_pct,
                "evidence": luck_item_evidence.get((_lb, _il), []),
            })
            if _owned and (_rarest is None or _tail < _rarest["_raw"]):
                _rarest = {"item": _item, "boss": _lb, "rate": _rate_str,
                           "kc": _kc, "copies": _c_eff,
                           "pct": _tail_pct, "_raw": _tail}
            if not _owned and _exp_n >= 1.0 and (_driest is None or _exp_n > _driest["exp_n"]):
                _driest = {"item": _item, "boss": _lb, "kc": _kc,
                           "exp_n": round(_exp_n, 1),
                           "cold_pct": round(((1.0 - _p) ** _kc) * 100, 1)}
        if not _items_out:
            continue
        # Owned first (rarest achievements up top), then missing by how
        # overdue they are.
        _items_out.sort(key=lambda x: (0, x["p_have"]) if x["owned"] else (1, -x["exp_n"]))
        _z = (_b_act - _b_exp) / math.sqrt(_b_var) if _b_var > 1e-9 else 0.0
        # Zero evidence against meaningful expectation = coverage gap
        # (kills predate the screenshot habit), not bad luck.
        _gap = (_b_act == 0 and _b_exp >= LUCK_GAP_EXPECTED)
        _gp = boss_gp_total.get(_lb, 0)
        luck_bosses.append({
            "boss": _lb, "kc": _kc,
            "expected": round(_b_exp, 1), "actual": _b_act,
            "z": round(_z, 2), "pct": round(_norm_cdf(_z) * 100),
            "gap": _gap,
            "gp": _gp, "gp_str": fmt_gp(_gp) if _gp else "",
            "items": _items_out[:12],
        })
        if not _gap:
            _tot_exp += _b_exp
            _tot_act += _b_act
            _tot_var += _b_var

    luck_bosses.sort(key=lambda b: (b["gap"], -b["z"]))
    _overall_z = (_tot_act - _tot_exp) / math.sqrt(_tot_var) if _tot_var > 1e-9 else 0.0
    luck_overall_pct = round(_norm_cdf(_overall_z) * 100)
    luck_verdict_str = luck_verdict(luck_overall_pct)
    if _rarest:
        _rarest.pop("_raw", None)
    luck_json = json.dumps({
        "bosses": luck_bosses,
        "overall_pct": luck_overall_pct,
        "verdict": luck_verdict_str,
        "expected": round(_tot_exp, 1),
        "actual": _tot_act,
        "rarest": _rarest,
        "driest": _driest,
    })
    # Headline card strings (with graceful empties for offline/no-data runs)
    luck_pct_str = f"{luck_overall_pct}%" if luck_bosses else "—"
    luck_uniques_str = f"{_tot_act} / {round(_tot_exp, 1)}" if luck_bosses else "—"
    if _rarest:
        _rx_prefix = f"x{_rarest['copies']} " if _rarest["copies"] > 1 else ""
        luck_rarest_value = f"{_rarest['pct']}%"
        luck_rarest_label = f"Rarest Flex · {_rx_prefix}{_rarest['item']}"
    else:
        luck_rarest_value = "—"
        luck_rarest_label = "Rarest Flex"
    luck_driest_value = f"{_driest['exp_n']}x owed" if _driest else "—"
    luck_driest_label = f"Driest · {_driest['item']}" if _driest else "Driest Grind"

    # Stats page extras: Collections Logged card, Luck card, Favorite Bosses
    _clog_val = hiscores.get("collections_logged")
    clog_card_str = f"{_clog_val:,}" if _clog_val else "—"
    luck_card_label = f"Luck · {luck_verdict_str}" if luck_bosses else "Luck"
    # Top 16 roughly matches the 99s Timeline's height so the grid-2 row has
    # no dead column (whitespace was flagged with 8 rows, July 2026).
    fav_bosses = sorted(hs_bosses.items(), key=lambda x: -x[1])[:16]
    total_boss_kc = sum(hs_bosses.values())
    fav_bosses_json = json.dumps([{"boss": _fb, "kc": _fk} for _fb, _fk in fav_bosses])

    # Milestone HTML
    milestone_html = ""
    for m in milestones[-25:]:
        badge_color = "#e8631a" if m["level"] == 99 else "#1a7ae8"
        milestone_html += f'''
        <div class="milestone-item">
            <span class="milestone-badge" style="background:{badge_color}">{m["level"]}</span>
            <span class="milestone-skill">{m["text"]}</span>
            <span class="milestone-date">{m["ts"]}</span>
        </div>'''
    if not milestone_html:
        milestone_html = '<p class="empty-note">No 50/70/80/90/92/99 milestones parsed yet.</p>'

    # Summary stats
    total_level_ups = sum(len(v) for v in data["level_ups"].values())
    if hiscores:
        max99s = sum(1 for s in skill_max_levels.values() if s == 99)
    else:
        max99s = sum(1 for lvls in data["level_ups"].values() if any(l == 99 for l, _ in lvls))
    total_deaths = len(data["categories"].get("Deaths", []))
    total_drops = len(data["categories"].get("Valuable Drops", []))
    total_clues = hs_clues.get("Clue Scrolls (all)", sum(clue_tiers.values()))

    first_str = data["first_screenshot"].strftime("%B %d, %Y") if data["first_screenshot"] else "N/A"
    last_str = data["last_screenshot"].strftime("%B %d, %Y") if data["last_screenshot"] else "N/A"

    delta = (data["last_screenshot"] - data["first_screenshot"]) if data["first_screenshot"] and data["last_screenshot"] else None
    years_played = ""
    if delta:
        yrs = delta.days / 365.25
        years_played = f"{yrs:.1f} years of screenshots" if yrs >= 1 else f"{delta.days // 30} months of screenshots"

    # Gallery data — build as JSON for JS
    gallery_json = []
    for item in data["gallery_items"]:
        gallery_json.append({
            "src": item["rel_path"],
            "path": item["rel_path"],
            "cat": item["category"],
            "label": Path(item["filename"]).stem,
            "ts": item["ts_str"],
            "sort": item["ts_sort"],
        })

    # Homepage cover moments: rank recent evidence by recency and account-story
    # significance, then preserve category variety. Every tile opens its source.
    home_moments = []
    for _moment in select_home_moments(data["gallery_items"]):
        _category = _moment["category"]
        _label = Path(_moment["filename"]).stem
        if _category == "Level-Ups":
            _skill, _level = parse_level_up(_moment["filename"])
            if _skill and _level:
                _label = f"{_skill} level {_level}"
        elif _category == "Quests":
            _label = parse_quest_name(_moment["filename"]) or _label
        home_moments.append({
            "src": _moment["rel_path"],
            "label": _label,
            "category": _category,
            "ts": _moment["timestamp"].strftime("%b %d, %Y"),
        })
    home_moments_json = json.dumps(home_moments)
    home_moments_html = "".join(
        '<button class="home-moment' + (' home-moment-featured' if i == 0 else '')
        + '" onclick="openHomeMoment(' + str(i) + ')" aria-label="Open ' + moment["category"] + ' screenshot">'
        + '<img src="' + moment["src"] + '" alt="" loading="' + ('eager' if i == 0 else 'lazy') + '">'
        + '<span><small>' + moment["category"] + '</small><strong>' + moment["label"] + '</strong><em>' + moment["ts"] + '</em></span>'
        + '</button>'
        for i, moment in enumerate(home_moments)
    )

    # Screenshot-backed account journey. Dates are taken only from parsed
    # RuneLite filenames; gaps are inferred later between observed levels and
    # never receive invented dates.
    journey_events = []
    for _entry in data["categories"].get("Level-Ups", []):
        _skill, _level = parse_level_up(_entry["filename"])
        _timestamp = _entry.get("timestamp")
        if not _skill or not _level or not _timestamp or _skill.lower() == "combat":
            continue
        journey_events.append({
            "skill": _skill,
            "level": _level,
            "src": _entry["rel_path"],
            "date": _timestamp.strftime("%b %d, %Y"),
            "iso": _timestamp.strftime("%Y-%m-%d"),
            "month": _timestamp.strftime("%Y-%m"),
            "color": SKILL_COLORS.get(_skill, "#c8a45a"),
        })
    journey_events.sort(key=lambda event: (event["iso"], event["skill"], event["level"]))
    journey_json = json.dumps(journey_events)

    all_cats = sorted(set(i["category"] for i in data["gallery_items"]))
    cat_filter_buttons = "".join(
        '<button class="filter-btn" data-cat="' + c + '" onclick="setFilter(\'' + c + '\', this)">' + c + '</button>'
        for c in all_cats
    )

    # Drops data
    drops_sorted = sorted(data["drops"], key=lambda d: d["value"], reverse=True)
    direct_gp = sum(d["value"] for d in data["drops"])
    indirect_gp = int(economic_value.get("realized_total", 0))
    total_gp = direct_gp + indirect_gp
    if total_gp >= 1_000_000_000:
        total_gp_str = f"{total_gp / 1_000_000_000:.2f}B"
    elif total_gp >= 1_000_000:
        total_gp_str = f"{total_gp / 1_000_000:.1f}M"
    else:
        total_gp_str = f"{total_gp:,}"
    drops_json = [
        {"item": d["item"], "qty": d["qty"], "value": d["value"],
         "src": d["rel_path"], "ts": d["ts_str"],
         "ts_iso": d["timestamp"].isoformat() if d["timestamp"] else ""}
        for d in drops_sorted
    ]

    pending_value = int(economic_value.get("latent_total", 0))
    pending_value_str = fmt_gp(pending_value) if pending_value else "None"
    indirect_gp_str = fmt_gp(indirect_gp) if indirect_gp else "None yet"
    price_note = "Live prices"
    if economic_value.get("prices_stale"):
        price_note = "Last cached prices" if economic_value.get("prices_updated_at") else "Prices unavailable"

    pending_rows = []
    for pending in economic_value.get("pending", []):
        duplicate_note = (
            f" · {pending['duplicates']} duplicate"
            + ("s" if pending["duplicates"] != 1 else "")
            if pending.get("duplicates") else ""
        )
        estimate = pending.get("estimated_value")
        estimate_text = fmt_gp(estimate) + " current progress" if estimate is not None else "Value pending"
        completed_value = pending.get("projected_completed_value")
        completed_text = (
            fmt_gp(completed_value) + " projected when complete"
            if completed_value is not None else "Completed value pending"
        )
        component_text = " · ".join(
            f"{part['item']} {part['have']}/{part['need']}"
            for part in pending.get("detail", [])
        )
        pending_rows.append(
            '<div class="wealth-pending-row">'
            f'<div><div class="wealth-pending-title">{pending["label"]}</div>'
            f'<div class="wealth-pending-parts">{component_text}</div></div>'
            f'<div class="wealth-pending-meta">{pending["have"]}/{pending["need"]} components'
            f'{duplicate_note}<span>{estimate_text}</span>'
            f'<span class="wealth-pending-projection">{completed_text}</span></div></div>'
        )
    pending_html = "".join(pending_rows) if pending_rows else (
        '<p class="wealth-empty">No incomplete registered assemblies found.</p>'
    )
    discovery_bits = []
    if discovery_summary.get("new_recipes"):
        recipes = ", ".join(escape(label) for label in discovery_summary["new_recipes"])
        discovery_bits.append(f"Learned {recipes} from an unambiguous wiki recipe.")
    if discovery_summary.get("new_bosses"):
        bosses = ", ".join(escape(name) for name in discovery_summary["new_bosses"])
        discovery_bits.append(f"Added reference drops for {bosses}.")
    # Items still outside a safe recipe, with the reason — a bare count is
    # useless, so the note names each item behind an expandable disclosure.
    status_text = {
        "ambiguous": "wiki shows multiple possible recipes",
        "no_clear_recipe": "no clear wiki recipe",
        "failed": "wiki lookup failed, will retry",
    }
    unresolved_rows = []
    seen_unresolved = set()
    for record in (discovery_summary.get("item_checks") or {}).values():
        if not isinstance(record, dict):
            continue
        item_name = str(record.get("item") or "").strip()
        reason = status_text.get(record.get("status"))
        if item_name and reason:
            unresolved_rows.append((item_name, reason))
            seen_unresolved.add(item_name.lower())
    for item_name in economic_value.get("unregistered_observed", []):
        if str(item_name).strip().lower() not in seen_unresolved:
            unresolved_rows.append((str(item_name).strip(), "queued for wiki check"))
    unresolved_rows.sort()
    unresolved_html = ""
    if unresolved_rows:
        row_html = "".join(
            f'<li>{escape(item_name)} <span>— {reason}</span></li>'
            for item_name, reason in unresolved_rows
        )
        plural = "s" if len(unresolved_rows) != 1 else ""
        unresolved_html = (
            '<details class="wealth-unresolved">'
            f'<summary>{len(unresolved_rows)} indirect item{plural} not yet counted — why?</summary>'
            f'<ul>{row_html}</ul></details>'
        )
    # Replace the legacy all-items list with a source-aware coverage review.
    assembly_rows, direct_rows, excluded_rows = [], [], []
    legacy_count = 0
    classified_names = set()
    for record in (discovery_summary.get("item_checks") or {}).values():
        if not isinstance(record, dict):
            continue
        item_name = str(record.get("item") or "").strip()
        classification = record.get("classification")
        reason = str(record.get("reason") or "").strip()
        if not item_name or not classification:
            legacy_count += 1
            continue
        classified_names.add(item_name.lower())
        if classification in {"ambiguous", "failed"}:
            assembly_rows.append((item_name, reason))
        elif classification == "direct_unpriced":
            direct_rows.append((item_name, reason))
        elif classification in {"excluded", "non_assembly"}:
            excluded_rows.append((item_name, reason))
    for item_name in economic_value.get("unregistered_observed", []):
        cleaned = str(item_name).strip()
        if cleaned and cleaned.lower() not in classified_names:
            assembly_rows.append((cleaned, "queued for wiki classification"))

    def _coverage_details(title, rows):
        if not rows:
            return ""
        rows = sorted(set(rows))
        row_html = "".join(
            f'<li>{escape(item_name)} <span>— {escape(reason)}</span></li>'
            for item_name, reason in rows
        )
        return (
            '<details class="wealth-unresolved">'
            f'<summary>{escape(title)} ({len(rows)})</summary>'
            f'<ul>{row_html}</ul></details>'
        )

    unresolved_html = _coverage_details("Items awaiting source classification", assembly_rows)
    unresolved_html += _coverage_details("Direct rewards without dated value evidence", direct_rows)
    unresolved_html += _coverage_details("Intentionally excluded from assembly research", excluded_rows)
    if legacy_count:
        unresolved_html += (
            '<p class="wealth-catalog-note">'
            f'Classifying {legacy_count} historical item{"s" if legacy_count != 1 else ""} automatically during normal refreshes.'
            '</p>'
        )

    discovery_html = (
        ('<p class="wealth-catalog-note">' + " ".join(discovery_bits) + '</p>'
         if discovery_bits else '')
        + unresolved_html
    )
    wealth_chart_json = json.dumps({
        "keys": [month for month, _value in gp_months_sorted],
        "labels": gp_tl_labels,
        "cumulative": gp_tl_values,
        "monthly": gp_monthly_values,
        "potential": (
            [c + p for c, p in zip(gp_tl_values, gp_tl_pending)]
            if gp_tl_pending and any(gp_tl_pending) else None
        ),
        "pend": (
            gp_tl_pending_detail
            if gp_tl_pending and any(gp_tl_pending) else None
        ),
        "contrib": gp_tl_contrib,
        "daily": gp_daily_payload,
    })

    # Hall of Fame — a chronological record of prestige unlocks.
    hof_sorted = sorted(data["hall_of_fame"], key=lambda x: x["timestamp"] or datetime.min)
    hof_data_for_js = []
    hof_html = ""
    if hof_sorted:
        for idx, h in enumerate(hof_sorted):
            has_shot = bool(h.get("rel_path"))
            click_attr = f' onclick="openHofItem({idx})" style="cursor:pointer"' if has_shot else ''
            hof_html += (
                f'<div class="hof-item"{click_attr}>'
                + ('<img class="hof-shot" src="' + h.get("rel_path", "") + '" alt="" loading="lazy">' if has_shot else '')
                + '<div class="hof-label">' + h["label"] + '</div>'
                + '<div class="hof-source">' + h["source"] + '</div>'
                + '<div class="hof-date">Unlocked · ' + h["ts_str"] + '</div>'
                + '</div>'
            )
            hof_data_for_js.append({
                "src":   h.get("rel_path", ""),
                "label": h["label"],
                "ts":    h["ts_str"],
            })
    else:
        hof_html = '<p class="empty-note">No Hall of Fame items found in collection log yet.</p>'
    hof_data_json = json.dumps(hof_data_for_js)

    # Chronicle — unified chronological story of the account
    # Build skill_lower → rel_path lookup for level-99 screenshots
    _level99_paths = {}
    for _entry in data["categories"].get("Level-Ups", []):
        _skill, _lvl = parse_level_up(_entry["filename"])
        if _skill and _lvl == 99:
            _level99_paths[_skill.lower()] = _entry.get("rel_path", "")

    chronicle_events = []

    for n in nineties:
        if n["ts"]:
            chronicle_events.append({
                "type": "skill",
                "title": n["skill"] + " 99",
                "sub": "Cape of Accomplishment",
                "ts_str": n["ts_str"],
                "ts_iso": n["ts"].isoformat(),
                "src": _level99_paths.get(n["skill"].lower(), ""),
                "color": n["color"],
                "badge": "99",
                "idx": 0,
            })

    for _entry in data["categories"].get("Pets", []):
        if _entry["timestamp"]:
            chronicle_events.append({
                "type": "pet",
                "title": "Pet Drop",
                "sub": "Added to collection",
                "ts_str": _entry["timestamp"].strftime("%b %d, %Y"),
                "ts_iso": _entry["timestamp"].isoformat(),
                "src": _entry["rel_path"],
                "color": "#e8631a",
                "badge": "PET",
                "idx": 0,
            })

    for h in data["hall_of_fame"]:
        if h["timestamp"]:
            chronicle_events.append({
                "type": "drop",
                "title": h["label"],
                "sub": h["source"],
                "ts_str": h["ts_str"],
                "ts_iso": h["timestamp"].isoformat(),
                "src": h.get("rel_path", ""),
                "color": "#c8a45a",
                "badge": "DROP",
                "idx": 0,
            })

    for _entry in data["categories"].get("Quests", []):
        _qname = parse_quest_name(_entry["filename"])
        if _qname and _entry["timestamp"]:
            chronicle_events.append({
                "type": "quest",
                "title": _qname,
                "sub": "Quest Completed",
                "ts_str": _entry["timestamp"].strftime("%b %d, %Y"),
                "ts_iso": _entry["timestamp"].isoformat(),
                "src": _entry["rel_path"],
                "color": "#3498db",
                "badge": "QUEST",
                "idx": 0,
            })

    chronicle_events.sort(key=lambda e: e["ts_iso"], reverse=True)
    for _i, _e in enumerate(chronicle_events):
        _e["idx"] = _i
    chronicle_json = json.dumps(chronicle_events)

    # This Week in Gielinor — prior-year memories around today's calendar
    # date. Curated Chronicle milestones lead; meaningful gallery screenshots
    # fill the carousel so the feature stays useful even in quieter weeks.
    this_week_memories, memory_window_label = build_this_week_memories(
        data["gallery_items"], chronicle_events,
        favorite_paths=favorite_paths
    )
    this_week_memories_json = json.dumps(this_week_memories)

    # Year in Review — computed recap banners for each Chronicle year group.
    # Fully self-contained: stats and template prose are generated by the
    # script at refresh time (no AI, no manual writing — see DESIGN.md #9).
    # Past years never change, so recaps are stable across refreshes; the
    # current year gets a "so far" framing. Prose pattern choice is
    # deterministic (year % pattern count) so wording doesn't churn.
    _current_year = datetime.now().year
    _yr = {}

    def _ys(year):
        if year not in _yr:
            _yr[year] = {"shots": 0, "n99": 0, "skills99": [], "drops": 0,
                         "gp": 0, "big_item": "", "big_val": 0, "big_month": "",
                         "pets": 0, "hof": []}
        return _yr[year]

    for _g in data["gallery_items"]:
        if _g["timestamp"]:
            _s = _ys(_g["timestamp"].year)
            _s["shots"] += 1
            if _g["category"] == "Pets":
                _s["pets"] += 1
    for _n in nineties:
        if _n["ts"]:
            _s = _ys(_n["ts"].year)
            _s["n99"] += 1
            _s["skills99"].append(_n["skill"])
    for _d in data["drops"]:
        if _d["timestamp"]:
            _s = _ys(_d["timestamp"].year)
            _s["drops"] += 1
            _s["gp"] += _d["value"]
            if _d["value"] > _s["big_val"]:
                _s["big_val"] = _d["value"]
                _s["big_item"] = _d["item"]
                _s["big_month"] = _d["timestamp"].strftime("%B")
    for _h in data["hall_of_fame"]:
        if _h["timestamp"]:
            _ys(_h["timestamp"].year)["hof"].append(_h["label"])

    def _classify_year(s):
        if s["shots"] < 30:
            return "sparse"
        if s["n99"] >= 2 and s["drops"] >= 15:
            return "big"
        if s["n99"] >= 2:
            return "skilling"
        if s["drops"] >= 15 or s["gp"] >= 50_000_000:
            return "bossing"
        if s["n99"] == 1:
            return "milestone"
        return "steady"

    _YR_LEADS = {
        "big": [
            "{y} was a landmark year — {n99} 99s fell and the bosses paid out {gp} across {nd} valuable drops.",
            "{y} had it all: {n99} new capes and {gp} in drops across {nd} entries in the loot tab.",
        ],
        "skilling": [
            "{y} was a skilling year — {n99} capes joined the collection while the boss log stayed quieter.",
            "{y} belonged to the grind: {n99} skills hit 99.",
        ],
        "bossing": [
            "{y} was a bossing year — {nd} valuable drops banked {gp}.",
            "{y} was spent in boss rooms: {nd} drops worth {gp} logged.",
        ],
        "milestone": [
            "{y} brought one cape — {skill99} hit 99 — alongside {shots} screenshots of steady progress.",
            "{y} had a single headline: 99 {skill99}, with {shots} screenshots around it.",
        ],
        "steady": [
            "{y} was a steady year — {shots} screenshots of quiet progress, no fireworks.",
            "{y} kept the account ticking over: {shots} moments captured, nothing flashy.",
        ],
        "sparse": [
            "A quieter stretch on record — only {shots} screenshots survive from {y}.",
            "{y} left a thin trail: just {shots} screenshots on record.",
        ],
    }

    year_recaps = {}
    for _year, _s in _yr.items():
        _kind = _classify_year(_s)
        if _year == _current_year:
            _bits = [f"{_s['shots']} screenshots"]
            if _s["n99"]:
                _bits.append(f"{_s['n99']} new 99{'s' if _s['n99'] > 1 else ''}")
            if _s["gp"]:
                _bits.append(f"{fmt_gp(_s['gp'])} in drops")
            _prose = f"{_year} so far: " + ", ".join(_bits) + "."
        else:
            _pats = _YR_LEADS[_kind]
            _prose = _pats[_year % len(_pats)].format(
                y=_year, n99=_s["n99"], nd=_s["drops"], gp=fmt_gp(_s["gp"]),
                shots=_s["shots"],
                skill99=_s["skills99"][0] if _s["skills99"] else "a skill",
            )
        _details = []
        if _kind != "sparse" and _s["big_item"] and _s["big_val"] >= 1_000_000:
            _details.append(f"The biggest single haul: {_s['big_item']} ({fmt_gp(_s['big_val'])}) in {_s['big_month']}.")
        if _kind in ("skilling", "big") and 2 <= _s["n99"] <= 5:
            _names = _s["skills99"][::-1]  # chronological order within the year
            _details.append("The 99s: " + ", ".join(_names) + ".")
        if _kind in ("bossing", "sparse") and _s["n99"]:
            _names = _s["skills99"][::-1]
            if len(_names) == 1:
                _details.append(f"Still, a cape landed: 99 {_names[0]}.")
            else:
                _details.append("Capes landed too: " + ", ".join(_names) + ".")
        if _s["pets"]:
            _details.append(f"{_s['pets']} pet{'s' if _s['pets'] > 1 else ''} showed up along the way.")
        if _s["hof"]:
            _hof_show = _s["hof"][:2]
            _more = len(_s["hof"]) - len(_hof_show)
            _details.append("Hall of Fame: " + ", ".join(_hof_show)
                            + (f" — and {_more} more." if _more > 0 else "."))
        if _details:
            _prose += " " + " ".join(_details)
        _chips = [[str(_s["shots"]), "screenshots"]]
        if _s["n99"]:
            _chips.append([str(_s["n99"]), "99s"])
        if _s["gp"]:
            _chips.append([fmt_gp(_s["gp"]), "GP in drops"])
        if _s["pets"]:
            _chips.append([str(_s["pets"]), "pets"])
        year_recaps[str(_year)] = {"chips": _chips[:4], "prose": _prose}
    year_recaps_json = json.dumps(year_recaps)

    # Top value events — direct drops plus realized multi-component assemblies.
    # Pending assemblies stay in Wealth Progression so potential value never
    # competes with finalized account value or gets counted twice.
    top_value_events = []
    for d in data["drops"]:
        top_value_events.append({
            "label": (f"{d['qty']}x " if d["qty"] > 1 else "") + d["item"],
            "value": int(d["value"]),
            "kind": "Drop",
            "ts_str": d["ts_str"],
        })
    for event in economic_value.get("events", []):
        try:
            completed_at = datetime.fromisoformat(event["completed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        top_value_events.append({
            "label": event.get("label", "Completed assembly"),
            "value": int(event.get("value", 0)),
            "kind": "Assembled",
            "ts_str": completed_at.strftime("%b %d, %Y"),
        })
    top_value_events.sort(key=lambda event: event["value"], reverse=True)

    # Top 5 events for the stats page highlight
    top5_html = ""
    for i, event in enumerate(top_value_events[:5], start=1):
        top5_html += (
            '<div class="drop-top-row">'
            '<span class="drop-rank">' + f"{i:02d}" + '</span>'
            '<span class="drop-name">' + event["label"] + ' <span class="drop-kind">' + event["kind"] + '</span></span>'
            '<span class="drop-value">' + fmt_gp(event["value"]) + '</span>'
            '<span class="drop-date">' + event["ts_str"] + '</span>'
            '</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{PLAYER_NAME} — OSRS Dashboard</title>
<style>{font_css}</style>
<script>{chart_js}</script>
<style>
  :root {{
    --gold: #c8a45a;
    --gold-bright: #f0c040;
    --gold-dim: #8a6c30;
    --bg: #0a0804;
    --bg-card: #120e08;
    --bg-card-hover: #1a1510;
    --border: #3a2d18;
    --border-bright: #6a4f28;
    --text: #d4c4a0;
    --text-muted: #b3a17e;
    --text-dim: #8f7d5e;
    --red: #9b2020;
    --green: #3a6b20;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{
    scrollbar-width: thin;
    scrollbar-color: var(--gold-dim) #0d0b07;
  }}
  * {{
    scrollbar-width: thin;
    scrollbar-color: var(--gold-dim) #0d0b07;
  }}
  *::-webkit-scrollbar {{ width: 10px; height: 10px; }}
  *::-webkit-scrollbar-track {{
    background: #0d0b07;
    border: 1px solid #2c2417;
  }}
  *::-webkit-scrollbar-thumb {{
    background: linear-gradient(180deg, #927333, #624a20);
    border: 2px solid #0d0b07;
    border-radius: 1px;
  }}
  *::-webkit-scrollbar-thumb:hover {{ background: var(--gold); }}
  *::-webkit-scrollbar-corner {{ background: #0d0b07; }}
  body {{
    font-family: 'Crimson Text', Georgia, serif;
    background: var(--bg);
    background-image:
      radial-gradient(ellipse at top, #1a120840 0%, transparent 55%),
      radial-gradient(ellipse at bottom, #0d0a0640 0%, transparent 55%),
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='80'%3E%3Crect width='160' height='80' fill='%23080603'/%3E%3Crect x='1' y='1' width='74' height='36' fill='%230e0c08' rx='1'/%3E%3Crect x='77' y='1' width='82' height='36' fill='%230c0a06' rx='1'/%3E%3Crect x='1' y='39' width='52' height='40' fill='%230d0b07' rx='1'/%3E%3Crect x='55' y='39' width='65' height='40' fill='%230f0c08' rx='1'/%3E%3Crect x='122' y='39' width='37' height='40' fill='%230c0a07' rx='1'/%3E%3C/svg%3E");
    background-size: auto, auto, 160px 80px;
    color: var(--text);
    min-height: 100vh;
  }}

  /* NAV */
  nav {{
    display: flex;
    background: #0e0a05;
    border-bottom: 2px solid var(--border-bright);
    padding: 0 40px;
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: 0 2px 12px rgba(0,0,0,0.6);
  }}
  nav button {{
    font-family: 'Cinzel', serif;
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 0.78rem;
    font-weight: 600;
    padding: 14px 24px;
    cursor: pointer;
    letter-spacing: 1px;
    text-transform: uppercase;
    transition: color 0.15s;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
  }}
  nav button:hover {{ color: var(--gold); }}
  nav button.active {{ color: var(--gold); border-bottom-color: var(--gold); }}

  /* HEADER */
  header {{
    background: linear-gradient(180deg, #1a1208 0%, #0d0a06 100%);
    border-bottom: 1px solid var(--border);
    padding: 32px 40px 28px;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: end;
    gap: 24px;
    position: relative;
    overflow: hidden;
  }}
  header::before {{
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(ellipse at 30% 50%, #2a1e0a22, transparent 70%);
    pointer-events: none;
  }}
  .header-title h1 {{
    font-family: 'Cinzel', serif;
    font-size: 2.6rem;
    font-weight: 700;
    color: var(--gold);
    letter-spacing: 3px;
    text-shadow: 0 0 30px #c8a45a44, 0 2px 4px rgba(0,0,0,0.8);
  }}
  .header-title p {{
    font-family: 'Cinzel', serif;
    color: var(--text-muted);
    font-size: 0.72rem;
    letter-spacing: 2px;
    margin-top: 6px;
    text-transform: uppercase;
  }}
  .header-title {{ grid-column: 1; justify-self: start; position: relative; z-index: 1; }}
  .header-meta {{
    grid-column: 3;
    grid-row: 1;
    justify-self: end;
    text-align: right;
    color: var(--text-muted);
    font-size: 0.85rem;
    line-height: 1.9;
    position: relative;
    z-index: 1;
  }}
  .header-meta span {{ color: var(--gold); font-weight: 600; }}
  .app-controls {{
    display: none;
    flex-direction: column;
    grid-column: 2;
    grid-row: 1;
    justify-self: center;
    align-items: center;
    gap: 6px;
    min-width: 190px;
    position: relative;
    z-index: 1;
  }}
  .app-controls.ready {{ display: flex; }}
  .refresh-btn {{
    background: #2a1e08;
    border: 1px solid var(--gold-dim);
    color: var(--gold);
    padding: 8px 14px;
    font-family: 'Cinzel', serif;
    font-size: 0.68rem;
    letter-spacing: 0.8px;
    cursor: pointer;
  }}
  .refresh-btn:hover {{ border-color: var(--gold); color: var(--gold-bright); }}
  .refresh-btn:disabled {{ cursor: wait; opacity: 0.65; }}
  .app-status {{ color: #b3a17e; font-size: 0.74rem; text-align: center; max-width: 260px; }}
  .app-status:empty {{ display: none; }}

  /* PAGES */
  .page {{ display: none; padding: 32px 40px; max-width: 1400px; margin: 0 auto; }}
  .page.active {{ display: block; }}

  /* STAT CARDS */
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 12px;
    margin-bottom: 12px;
  }}
  .clue-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 28px;
  }}
  @media (max-width: 900px) {{
    .stat-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .clue-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
  .stat-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-top: 2px solid var(--gold-dim);
    padding: 18px 16px;
    text-align: center;
    position: relative;
    clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 10px 100%, 0 calc(100% - 10px));
  }}
  .stat-card .value {{
    font-family: 'Cinzel', serif;
    font-size: 1.9rem;
    font-weight: 700;
    color: var(--gold);
    line-height: 1;
    text-shadow: 0 0 20px #c8a45a33;
  }}
  .stat-card .label {{
    font-family: 'Cinzel', serif;
    font-size: 0.7rem;
    font-weight: 600;
    color: #c8bfae;
    margin-top: 8px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }}

  /* ACCOUNT PULSE */
  .account-pulse {{ margin-bottom: 24px; }}
  .pulse-header {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    margin-bottom: 18px;
  }}
  .pulse-kicker {{
    font-family: 'Cinzel', serif;
    color: #c8bfae;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 1.7px;
    text-transform: uppercase;
    margin-bottom: 6px;
  }}
  .pulse-verdict {{
    font-family: 'Cinzel', serif;
    color: var(--gold);
    font-size: 1.28rem;
    line-height: 1.25;
  }}
  .pulse-meta {{ color: #c8bfae; font-size: 0.82rem; margin-top: 7px; }}
  .pulse-window-controls {{ display: flex; gap: 7px; flex-shrink: 0; flex-wrap: wrap; }}
  .pulse-window-btn {{
    background: #0d0a06;
    border: 1px solid var(--border-bright);
    color: #c8bfae;
    font-family: 'Cinzel', serif;
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.9px;
    padding: 7px 12px;
    cursor: pointer;
    text-transform: uppercase;
  }}
  .pulse-window-btn:hover {{ color: var(--gold); border-color: var(--gold); }}
  .pulse-window-btn.active {{ color: var(--gold-bright); border-color: var(--gold); background: #3a2a0b; box-shadow: inset 0 0 0 1px #c8a45a33; }}
  .pulse-metrics {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
    margin-bottom: 14px;
  }}
  .pulse-metric {{ background: #0d0a06; border: 1px solid var(--border); padding: 14px 15px; }}
  .pulse-metric-value {{
    font-family: 'Cinzel', serif;
    color: var(--gold-bright);
    font-size: 1.45rem;
    line-height: 1;
  }}
  .pulse-metric-label {{
    color: #c8bfae;
    font-size: 0.76rem;
    font-weight: 600;
    letter-spacing: 0.8px;
    text-transform: uppercase;
    margin-top: 7px;
  }}
  .pulse-breakdowns {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  .pulse-panel {{ background: #0d0a06; border: 1px solid var(--border); padding: 15px; }}
  .pulse-panel-title {{
    font-family: 'Cinzel', serif;
    color: #e3d8c2;
    font-size: 0.74rem;
    font-weight: 600;
    letter-spacing: 1.4px;
    text-transform: uppercase;
    margin-bottom: 11px;
  }}
  .pulse-row {{ margin-bottom: 10px; }}
  .pulse-row:last-child {{ margin-bottom: 0; }}
  .pulse-row-head {{ display: flex; justify-content: space-between; gap: 12px; margin-bottom: 5px; }}
  .pulse-row-name {{ color: #e3d8c2; font-size: 0.82rem; }}
  .pulse-row-value {{ color: #c8bfae; font-size: 0.8rem; font-weight: 600; text-align: right; }}
  .pulse-bar {{ height: 6px; background: #1a1208; border: 1px solid var(--border); overflow: hidden; }}
  .pulse-bar-fill {{ height: 100%; background: var(--gold-dim); transition: width 0.35s ease; }}
  .pulse-empty {{ color: #c8bfae; font-size: 0.84rem; font-style: italic; }}

  /* LAYOUT GRIDS */
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .grid-3 {{ display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 20px; margin-bottom: 20px; }}

  .card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    padding: 22px;
    position: relative;
  }}
  .card::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--gold-dim), transparent);
  }}
  .card h2 {{
    font-family: 'Cinzel', serif;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--gold);
    margin-bottom: 16px;
    font-weight: 600;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }}
  canvas {{ width: 100% !important; }}

  /* WEALTH PROGRESSION */
  .wealth-card {{ margin-bottom: 24px; position: relative; }}
  .wealth-tip {{ position: absolute; z-index: 40; pointer-events: none; display: none; width: 250px; background: #16110a; border: 1px solid var(--border-bright); padding: 10px 12px; font-size: 0.76rem; color: #d8cdb8; box-shadow: 0 6px 18px rgba(0,0,0,0.55); }}
  .wealth-tip .wt-head {{ display: flex; justify-content: space-between; gap: 10px; font-family: 'Cinzel', serif; color: var(--gold); margin-bottom: 4px; }}
  .wealth-tip .wt-line {{ color: #c8bfae; margin-bottom: 4px; }}
  .wealth-tip img {{ width: 100%; height: auto; display: block; border: 1px solid var(--border); margin: 6px 0; }}
  .wealth-tip .wt-row {{ display: flex; justify-content: space-between; gap: 10px; padding: 2px 0; }}
  .wealth-tip .wt-row span:last-child {{ color: var(--gold); white-space: nowrap; }}
  .wealth-tip .wt-pend span {{ color: #a89878; }}
  .wealth-tip .wt-pend span:last-child {{ color: #cfae5c; font-style: italic; }}
  .wealth-tip .wt-more {{ color: var(--text-muted); margin-top: 4px; }}
  .wealth-tip .wt-open {{ color: var(--text-muted); font-style: italic; margin-top: 6px; }}
  .wealth-unresolved {{ margin-top: 12px; font-size: 0.76rem; color: #c8bfae; }}
  .wealth-unresolved summary {{ cursor: pointer; color: var(--text-muted); font-family: 'Cinzel', serif; font-size: 0.66rem; letter-spacing: 0.8px; }}
  .wealth-unresolved summary:hover {{ color: var(--gold); }}
  .wealth-unresolved ul {{ margin: 8px 0 0; padding-left: 18px; line-height: 1.6; }}
  .wealth-unresolved li span {{ color: var(--text-muted); }}
  .wealth-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; margin-bottom: 14px; }}
  .wealth-summary {{ color: var(--text-muted); font-size: 0.8rem; line-height: 1.55; }}
  .wealth-summary b {{ color: var(--gold-bright); font-family: 'Cinzel', serif; font-weight: 600; }}
  .wealth-controls {{ display: flex; gap: 7px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }}
  .wealth-control {{ background: #0d0a06; border: 1px solid var(--border-bright); color: #d8cdb8; padding: 6px 9px; font-family: 'Cinzel', serif; font-size: 0.68rem; font-weight: 600; letter-spacing: 0.6px; cursor: pointer; }}
  .wealth-control:hover {{ border-color: var(--gold-dim); color: var(--gold); }}
  .wealth-control.active {{ color: var(--gold); border-color: var(--gold); background: #2a1e08; }}
  .wealth-chart-wrap {{ height: 285px; position: relative; }}
  .wealth-gain-wrap {{ height: 108px; position: relative; margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }}
  .wealth-chart-note {{ color: #c8bfae; font-size: 0.76rem; margin: 10px 0 0; }}
  .wealth-pending {{ margin-top: 20px; border-top: 1px solid var(--border); padding-top: 14px; }}
  .wealth-pending-label {{ font-family: 'Cinzel', serif; color: var(--text-muted); font-size: 0.65rem; letter-spacing: 1.2px; text-transform: uppercase; margin-bottom: 8px; }}
  .wealth-pending-row {{ display: flex; justify-content: space-between; gap: 18px; padding: 10px 0; border-bottom: 1px solid var(--border); }}
  .wealth-pending-row:last-child {{ border-bottom: none; }}
  .wealth-pending-title {{ color: var(--text); font-size: 0.9rem; }}
  .wealth-pending-parts {{ color: #c8bfae; font-size: 0.74rem; margin-top: 4px; }}
  .wealth-pending-meta {{ color: var(--text-muted); font-size: 0.74rem; text-align: right; white-space: nowrap; }}
  .wealth-pending-meta span {{ display: block; color: var(--gold); margin-top: 4px; }}
  .wealth-empty {{ color: #c8bfae; font-size: 0.84rem; font-style: italic; margin: 0; }}
  .wealth-catalog-note {{ color: #c8bfae; font-size: 0.76rem; line-height: 1.45; margin: 12px 0 0; }}
  @media (max-width: 700px) {{
    .wealth-head, .wealth-pending-row {{ flex-direction: column; }}
    .wealth-controls {{ justify-content: flex-start; }}
    .wealth-pending-meta {{ text-align: left; white-space: normal; }}
    .wealth-chart-wrap {{ height: 245px; }}
  }}

  /* MILESTONES */
  .milestone-item {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
  }}
  .milestone-item:last-child {{ border-bottom: none; }}
  .milestone-badge {{
    font-family: 'Cinzel', serif;
    font-size: 0.68rem;
    font-weight: 700;
    color: #000;
    border-radius: 2px;
    padding: 2px 7px;
    min-width: 36px;
    text-align: center;
  }}
  .milestone-skill {{ flex: 1; font-size: 1rem; color: var(--text); }}
  .milestone-date {{ font-size: 0.78rem; color: var(--text-dim); }}

  /* PETS */
  .pet-thumb-grid {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }}
  .pet-thumb {{
    width: 90px;
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--bg);
    overflow: hidden;
    transition: border-color 0.15s;
  }}
  .pet-thumb:hover {{ border-color: var(--gold); }}
  .pet-thumb img {{
    width: 100%;
    height: 70px;
    object-fit: cover;
    display: block;
  }}
  .pet-thumb-date {{
    font-size: 0.62rem;
    color: var(--text-dim);
    text-align: center;
    padding: 4px 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  /* CLUES */
  .clue-inline-row {{ font-size: 0.88rem; color: var(--text); line-height: 1.8; }}
  .clue-inline-tier {{ font-family: 'Cinzel', serif; font-size: 0.75rem; letter-spacing: 0.5px; }}
  .clue-inline-count {{ font-weight: 700; color: var(--gold); margin-left: 4px; }}
  .clue-inline-sep {{ color: var(--text-dim); }}

  .empty-note {{ color: var(--text-dim); font-size: 0.9rem; font-style: italic; }}

  /* GALLERY */
  .gallery-controls {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 20px;
    align-items: center;
  }}
  .gallery-controls input {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 12px;
    font-size: 0.9rem;
    font-family: 'Crimson Text', serif;
    width: 240px;
    outline: none;
  }}
  .gallery-controls input:focus {{ border-color: var(--gold-dim); }}
  .filter-btn {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-muted);
    padding: 6px 14px;
    font-size: 0.72rem;
    font-family: 'Cinzel', serif;
    letter-spacing: 0.5px;
    cursor: pointer;
    transition: all 0.15s;
  }}
  .filter-btn:hover {{ border-color: var(--gold-dim); color: var(--text); }}
  .filter-btn.active {{ background: #2a1e08; border-color: var(--gold); color: var(--gold); }}

  .gallery-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 10px;
  }}
  .gallery-item {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    overflow: hidden;
    cursor: pointer;
    transition: border-color 0.15s, transform 0.1s;
    position: relative;
  }}
  .gallery-item:hover {{ border-color: var(--gold); transform: translateY(-2px); }}
  .gallery-item img {{
    width: 100%;
    aspect-ratio: 16/10;
    object-fit: cover;
    display: block;
    background: #080604;
  }}
  .gallery-item .thumb-info {{ padding: 8px 10px; }}
  .gallery-item .thumb-cat {{
    font-family: 'Cinzel', serif;
    font-size: 0.62rem;
    color: var(--gold);
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }}
  .gallery-item .thumb-label {{
    font-size: 0.85rem;
    color: var(--text);
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .gallery-item .thumb-date {{
    font-size: 0.72rem;
    color: var(--text-dim);
    margin-top: 2px;
  }}
  .gallery-more-wrap {{ text-align: center; padding: 24px 0 4px; }}
  .gallery-more-btn {{
    background: #2a1e08;
    border: 1px solid var(--gold-dim);
    color: var(--gold);
    padding: 9px 20px;
    font-family: 'Cinzel', serif;
    font-size: 0.72rem;
    letter-spacing: 0.8px;
    cursor: pointer;
  }}
  .gallery-more-btn:hover {{ border-color: var(--gold); color: var(--gold-bright); }}
  .favorite-showcase {{ margin-bottom: 20px; }}
  .favorite-showcase-head {{ display: flex; justify-content: space-between; gap: 16px; align-items: baseline; }}
  .favorite-showcase-count {{ color: #b3a17e; font-size: 0.78rem; }}
  .favorite-heart {{
    position: absolute;
    top: 8px;
    right: 8px;
    width: 34px;
    height: 34px;
    border-radius: 50%;
    border: 1px solid #c8a45a99;
    background: rgba(8, 6, 4, 0.88);
    color: #e3d8c2;
    font-size: 1.25rem;
    line-height: 1;
    cursor: pointer;
    z-index: 2;
  }}
  .favorite-heart:hover {{ border-color: var(--gold); color: var(--gold-bright); }}
  .favorite-heart.active {{ color: #d96b72; border-color: #d96b72; background: rgba(35, 10, 12, 0.92); }}

  /* LIGHTBOX */
  #lightbox {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.92);
    z-index: 999;
    align-items: center;
    justify-content: center;
    flex-direction: column;
  }}
  #lightbox.open {{ display: flex; }}
  #lightbox img {{
    max-width: 90vw;
    max-height: 80vh;
    border: 1px solid var(--border-bright);
    object-fit: contain;
  }}
  #lightbox .lb-info {{
    color: var(--text-muted);
    font-size: 0.9rem;
    margin-top: 14px;
    text-align: center;
    font-family: 'Cinzel', serif;
    letter-spacing: 0.5px;
  }}
  #lightbox .lb-favorite {{
    position: static;
    margin-top: 12px;
  }}
  #lightbox .lb-close {{
    position: absolute;
    top: 20px;
    right: 28px;
    font-size: 2rem;
    color: var(--text-muted);
    cursor: pointer;
    background: none;
    border: none;
    line-height: 1;
  }}
  #lightbox .lb-close:hover {{ color: var(--gold); }}
  #lightbox .lb-nav {{
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    font-size: 2.5rem;
    color: var(--text-muted);
    cursor: pointer;
    background: none;
    border: none;
    padding: 10px;
    transition: color 0.1s;
    font-family: 'Cinzel', serif;
  }}
  #lightbox .lb-nav:hover {{ color: var(--gold); }}
  #lightbox .lb-prev {{ left: 20px; }}
  #lightbox .lb-next {{ right: 20px; }}

  .gallery-count {{ color: var(--text-dim); font-size: 0.8rem; margin-left: auto; font-family: 'Cinzel', serif; letter-spacing: 0.5px; }}

  /* DROPS */
  .drop-top-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
  }}
  .drop-top-row:last-child {{ border-bottom: none; }}
  .drop-rank {{ font-family: 'Cinzel', serif; font-size: 0.78rem; font-weight: 700; color: var(--gold); min-width: 28px; }}
  .drop-name {{ flex: 1; font-size: 1rem; color: var(--text); }}
  .drop-kind {{ color: #c8bfae; font-family: 'Cinzel', serif; font-size: 0.62rem; font-weight: 600; letter-spacing: 0.7px; text-transform: uppercase; white-space: nowrap; }}
  .drop-value {{ font-size: 0.95rem; font-weight: 700; color: var(--gold-bright); min-width: 60px; text-align: right; font-family: 'Cinzel', serif; }}
  .drop-date {{ font-size: 0.78rem; color: var(--text-dim); min-width: 90px; text-align: right; }}

  .loot-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 10px;
    margin-top: 4px;
  }}
  .loot-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    overflow: hidden;
    transition: border-color 0.15s;
    cursor: pointer;
  }}
  .loot-card:hover {{ border-color: var(--gold); }}
  .loot-card img {{
    width: 100%;
    aspect-ratio: 16/10;
    object-fit: cover;
    display: block;
    background: #080604;
  }}
  .loot-card .loot-info {{ padding: 10px 12px; }}
  .loot-card .loot-item {{ font-size: 1rem; color: var(--text); font-weight: 600; }}
  .loot-card .loot-gp {{ font-family: 'Cinzel', serif; font-size: 0.85rem; color: var(--gold-bright); font-weight: 700; margin-top: 4px; }}
  .loot-card .loot-date {{ font-size: 0.75rem; color: var(--text-dim); margin-top: 3px; }}
  .loot-sort-controls {{ display: flex; gap: 8px; margin-bottom: 16px; }}

  /* HALL OF FAME */
  .hof-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 12px;
  }}
  .hof-item {{
    background: linear-gradient(135deg, #1a1508, #120f05);
    border: 1px solid var(--gold-dim);
    border-top: 2px solid var(--gold);
    padding: 14px 16px;
    clip-path: polygon(0 0, calc(100% - 8px) 0, 100% 8px, 100% 100%, 8px 100%, 0 calc(100% - 8px));
  }}
  .hof-item[onclick]:hover {{ border-color: var(--gold); box-shadow: 0 0 8px rgba(201,156,49,0.25); }}
  .hof-label {{ font-family: 'Cinzel', serif; font-size: 0.82rem; font-weight: 700; color: var(--gold); letter-spacing: 0.5px; }}
  .hof-note {{ color: #c8bfae; font-size: 0.8rem; margin: -5px 0 14px; }}
  .hof-source {{ font-size: 0.82rem; color: var(--text-muted); margin-top: 5px; }}
  .hof-date {{ font-family: 'Cinzel', serif; font-size: 0.7rem; color: var(--text-dim); margin-top: 8px; letter-spacing: 0.5px; }}

  /* 99s TIMELINE */
  #nineties-timeline {{
    display: flex;
    flex-direction: column;
    gap: 0;
    position: relative;
    padding-left: 20px;
  }}
  #nineties-timeline::before {{
    content: '';
    position: absolute;
    left: 6px;
    top: 8px;
    bottom: 8px;
    width: 2px;
    background: var(--border-bright);
  }}
  .nt-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 5px 0;
    position: relative;
  }}
  .nt-dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-left: -24px;
    border: 2px solid var(--bg-card);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.1);
  }}
  .nt-skill {{ font-size: 0.88rem; color: var(--text); min-width: 110px; }}
  .nt-date {{ font-family: 'Cinzel', serif; font-size: 0.72rem; color: var(--text-dim); letter-spacing: 0.5px; }}
  .nt-empty {{ color: var(--text-dim); font-size: 0.85rem; padding: 12px 0; }}

  /* ROAD TO MAX */
  .rtm-row {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 5px 0;
  }}
  .rtm-skill {{ font-size: 0.88rem; color: var(--text); min-width: 90px; }}
  .rtm-bar-bg {{
    flex: 1;
    height: 6px;
    background: #1a1208;
    border: 1px solid var(--border);
    overflow: hidden;
  }}
  .rtm-bar-fill {{
    height: 100%;
    transition: width 0.4s ease;
  }}
  .rtm-level {{ font-family: 'Cinzel', serif; font-size: 0.78rem; color: var(--gold); min-width: 28px; text-align: right; }}
  .rtm-remaining {{ font-size: 0.72rem; color: var(--text-dim); min-width: 42px; text-align: right; }}

  /* ROAD TO MAX TAB */
  .rtm-pace-note {{ font-size: 0.78rem; color: var(--text-dim); margin: -8px 0 20px 2px; }}
  .methodology-note {{ margin: -8px 0 20px 2px; color: var(--text-dim); font-size: 0.78rem; }}
  .methodology-note summary {{ color: var(--text-muted); cursor: pointer; width: fit-content; }}
  .methodology-note summary:hover {{ color: var(--gold); }}
  .methodology-note p {{ max-width: 1050px; margin-top: 8px; line-height: 1.45; }}
  .rtm-detail-row {{ padding: 13px 4px; border-bottom: 1px solid var(--border); }}
  .rtm-detail-row:last-child {{ border-bottom: none; }}
  .rtm-detail-top {{ display: flex; align-items: center; gap: 10px; margin-bottom: 7px; }}
  .rtm-skill-lg {{ font-family: 'Cinzel', serif; font-size: 1rem; font-weight: 700; }}
  .rtm-detail-level {{ margin-left: auto; font-family: 'Cinzel', serif; font-size: 0.85rem; color: var(--gold); }}
  .rtm-bar-lg {{ height: 10px; }}
  .rtm-detail-meta {{ display: flex; justify-content: space-between; gap: 12px; font-size: 0.75rem; color: var(--text-dim); margin-top: 6px; }}
  .rtm-badge {{ font-size: 0.62rem; font-weight: 700; padding: 2px 8px; border: 1px solid var(--border); text-transform: uppercase; letter-spacing: 1px; }}
  .rtm-badge-active {{ color: #2ecc71; border-color: #2ecc71; }}

  /* LUCK TAB */
  #page-luck.active {{ display: flex; flex-direction: column; gap: 10px; }}
  #page-luck > * {{ margin-bottom: 0 !important; }}
  .luck-summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }}
  .luck-summary-card {{ min-height: 86px; padding: 12px 14px; display: grid; grid-template-columns: 42px minmax(0,1fr); gap: 11px; align-items: center; background: linear-gradient(145deg,#17170f,#10110e); border: 1px solid #45371d; }}
  .luck-orbit {{ width: 40px; height: 40px; display: grid; place-items: center; border: 1px solid var(--gold-dim); border-radius: 50%; box-shadow: inset 0 0 0 4px #111007; color: var(--gold-bright); font: 600 9px 'Cinzel',serif; }}
  .luck-summary-card strong,.luck-summary-card span,.luck-summary-card small {{ display: block; }}
  .luck-summary-card strong {{ color: var(--gold-bright); font: 600 1.35rem 'Cinzel',serif; line-height: 1; }}
  .luck-summary-card span {{ margin-top: 5px; color: var(--text); font: 600 .61rem 'Cinzel',serif; letter-spacing: .6px; text-transform: uppercase; }}
  .luck-summary-card small {{ margin-top: 3px; color: #c8bfae; font-size: .69rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .luck-methodology {{ padding: 0 14px; background: linear-gradient(145deg,#15140e,#0f100d); border: 1px solid #45371d; }}
  .luck-methodology summary {{ padding: 10px 0; width: fit-content; cursor: pointer; color: var(--gold-bright); font: 600 .66rem 'Cinzel',serif; letter-spacing: .7px; text-transform: uppercase; }}
  .luck-methodology summary:hover {{ color: var(--gold); }}
  .luck-methodology p {{ margin: 0; padding: 10px 0 12px; border-top: 1px solid #49391c; color: #c8bfae; font-size: .78rem; line-height: 1.5; }}
  .luck-workspace {{ padding: 14px 16px; }}
  .luck-workspace-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding-bottom: 11px; border-bottom: 1px solid #49391c; }}
  .luck-workspace-head h2 {{ margin: 0; padding: 0; border: 0; font-size: 1rem; }}
  .luck-workspace-head p {{ margin: 4px 0 0; color: #c8bfae; font-size: .76rem; }}
  .luck-sort-controls {{ display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; }}
  .luck-sort-controls::-webkit-scrollbar {{ display: none; }}
  .luck-sort-controls button {{ height: 31px; padding: 0 10px; flex: none; border: 1px solid var(--border-bright); background: #0d0e0b; color: #c8bfae; cursor: pointer; font: 600 .58rem 'Cinzel',serif; letter-spacing: .45px; text-transform: uppercase; }}
  .luck-sort-controls button:hover {{ color: var(--gold); border-color: var(--gold-dim); }}
  .luck-sort-controls button.active {{ color: var(--gold-bright); background: #2a210d; border-color: var(--gold); }}
  .luck-browser {{ display: grid; grid-template-columns: minmax(390px,.9fr) minmax(420px,1.1fr); gap: 14px; padding-top: 12px; }}
  .luck-list-panel {{ min-width: 0; }}
  .luck-list-key {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 7px; padding: 0 2px; }}
  .luck-list-key strong {{ color: var(--gold-bright); font: 600 .64rem 'Cinzel',serif; letter-spacing: .65px; text-transform: uppercase; }}
  .luck-list-key span {{ color: #c8bfae; font-size: .69rem; }}
  .luck-list {{ display: grid; gap: 6px; align-content: start; max-height: 612px; overflow-y: auto; padding-right: 4px; }}
  .luck-row {{ display: grid; grid-template-columns: 46px minmax(0,1fr) 104px; gap: 9px; align-items: center; min-height: 66px; padding: 8px; border: 1px solid #44371f; background: #0d0f0c; color: inherit; cursor: pointer; text-align: left; font-family: inherit; }}
  .luck-row:hover,.luck-row:focus-visible,.luck-row.active {{ border-color: var(--gold); background: #18150d; outline: none; }}
  .luck-row.active {{ box-shadow: inset 3px 0 0 var(--gold-bright); }}
  .luck-score {{ width: 41px; height: 41px; display: grid; place-items: center; border: 1px solid var(--score-color); border-radius: 50%; background: #11120e; color: var(--score-color); font: 600 .79rem 'Cinzel',serif; }}
  .luck-row-copy strong,.luck-row-copy span {{ display: block; }}
  .luck-row-copy strong {{ color: var(--text); font: 600 .72rem 'Cinzel',serif; }}
  .luck-row-copy span {{ margin-top: 3px; color: #c8bfae; font-size: .67rem; }}
  .luck-row-meter {{ height: 5px; margin-top: 6px; overflow: hidden; background: #2d2a20; }}
  .luck-row-meter i {{ display: block; height: 100%; width: var(--score); background: var(--score-color); }}
  .luck-row-numbers {{ text-align: right; }}
  .luck-row-numbers strong,.luck-row-numbers span {{ display: block; }}
  .luck-row-numbers strong {{ color: var(--gold-bright); font: 600 .68rem 'Cinzel',serif; }}
  .luck-row-numbers span {{ margin-top: 3px; color: #c8bfae; font-size: .61rem; }}
  .luck-gap {{ display: inline-block; margin-top: 5px; padding: 2px 5px; border: 1px solid #72533d; color: #d7ae90; font: 600 .5rem 'Cinzel',serif; text-transform: uppercase; }}
  .luck-detail {{ min-width: 0; padding-left: 14px; border-left: 1px solid #49391c; }}
  .luck-detail-hero {{ display: grid; grid-template-columns: 92px minmax(0,1fr); gap: 13px; min-height: 92px; padding-bottom: 11px; border-bottom: 1px solid #49391c; }}
  .luck-detail-visual {{ width: 92px; height: 92px; display: grid; place-items: center; border: 1px solid var(--gold-dim); background: radial-gradient(circle,#28200e,#0b0c0a); color: var(--gold-bright); font: 600 1.45rem 'Cinzel',serif; }}
  .luck-detail-image {{ width:92px; height:92px; padding:0; border:1px solid var(--gold-dim); background:#0b0c0a; cursor:pointer; overflow:hidden; }}
  .luck-detail-image img {{ width:100%; height:100%; object-fit:cover; display:block; }}
  .luck-detail-image:hover,.luck-detail-image:focus-visible {{ border-color:var(--gold-bright); outline:none; }}
  .luck-detail-copy h2 {{ margin: 0; padding: 0; border: 0; color: var(--text); font-size: 1.05rem; }}
  .luck-detail-copy p {{ margin: 5px 0 0; color: #c8bfae; font-size: .72rem; line-height: 1.45; }}
  .luck-verdict {{ display: inline-block; margin-top: 8px; padding: 3px 7px; border: 1px solid var(--score-color); color: var(--score-color); font: 600 .56rem 'Cinzel',serif; letter-spacing: .55px; text-transform: uppercase; }}
  .luck-equation {{ display: grid; grid-template-columns: 1fr auto 1fr; gap: 10px; align-items: center; margin: 11px 0; padding: 10px; border: 1px solid #44371f; background: #0d0e0b; text-align: center; }}
  .luck-equation strong,.luck-equation span {{ display: block; }}
  .luck-equation strong {{ color: var(--gold-bright); font: 600 1.05rem 'Cinzel',serif; }}
  .luck-equation span {{ margin-top: 3px; color: #c8bfae; font-size: .62rem; }}
  .luck-equation i {{ color: var(--text-dim); font-size: .65rem; font-style: normal; text-transform: uppercase; }}
  .luck-detail-section header {{ position: static; height: auto; margin: 0 0 8px; padding: 0; display: flex; align-items: baseline; justify-content: space-between; gap: 10px; background: none; border: 0; box-shadow: none; }}
  .luck-detail-section header strong {{ color: var(--text); font: 600 .66rem 'Cinzel',serif; letter-spacing: .5px; text-transform: uppercase; }}
  .luck-detail-section header span {{ color: #c8bfae; font-size: .66rem; }}
  .luck-item-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
  .luck-item {{ min-width: 0; padding: 8px 9px; border: 1px solid #44371f; background: #0d0f0c; }}
  .luck-item.owned {{ border-color: #78601f; background: #17150b; }}
  button.luck-item {{ width:100%; color:inherit; text-align:left; font:inherit; }}
  .luck-item.evidence-linked {{ cursor:pointer; position:relative; }}
  .luck-item.evidence-linked:hover,.luck-item.evidence-linked:focus-visible {{ border-color:var(--gold-bright); background:#211b0c; outline:none; box-shadow:inset 3px 0 0 var(--gold-bright); }}
  .luck-item strong,.luck-item span,.luck-item small {{ display: block; overflow: hidden; text-overflow: ellipsis; }}
  .luck-item strong {{ color: var(--text); font-size: .72rem; white-space: nowrap; }}
  .luck-item.owned strong {{ color: var(--gold-bright); }}
  .luck-item span {{ margin-top: 3px; color: #c8bfae; font-size: .62rem; }}
  .luck-item small {{ margin-top: 4px; color: var(--text-dim); font-size: .61rem; white-space: nowrap; }}
  .luck-gap-note {{ margin: 10px 0 0; padding: 9px; border: 1px solid #694b36; background: #18110d; color: #d7ae90; font-size: .69rem; line-height: 1.4; }}
  /* Crimson Text loses definition at microcopy sizes in Chromium. Keep the
     display typefaces for hierarchy, but render small supporting labels in
     the local UI font so they remain crisp at every browser scale. */
  .luck-summary-card small,
  .luck-methodology p,
  .luck-workspace-head p,
  .luck-list-key span,
  .luck-row-copy span,
  .luck-row-numbers span,
  .luck-detail-copy p,
  .luck-equation span,
  .luck-equation i,
  .luck-detail-section header span,
  .luck-item span,
  .luck-item small,
  .luck-gap-note {{
    font-family: 'Segoe UI', Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }}
  .luck-equation span {{ color: #d8cdb8; font-size: .7rem; }}
  .luck-equation i {{ font-size: .68rem; }}
  .luck-row-numbers span,.luck-item span,.luck-item small {{ font-size: .66rem; }}
  @media (max-width: 1180px) {{
    .luck-summary-grid {{ grid-template-columns: 1fr 1fr; }}
    .luck-browser {{ grid-template-columns: minmax(340px,.85fr) minmax(360px,1.15fr); }}
  }}
  @media (max-width: 900px) {{
    .luck-workspace-head {{ display: block; }}
    .luck-sort-controls {{ margin-top: 10px; }}
    .luck-browser {{ grid-template-columns: 1fr; }}
    .luck-list {{ max-height: 390px; }}
    .luck-detail {{ padding: 12px 0 0; border: 0; border-top: 1px solid #49391c; }}
  }}
  @media (max-width: 600px) {{
    .luck-summary-grid {{ gap: 7px; }}
    .luck-summary-card {{ min-height: 74px; padding: 9px; grid-template-columns: 32px minmax(0,1fr); gap: 8px; }}
    .luck-orbit {{ width: 30px; height: 30px; font-size: .45rem; }}
    .luck-summary-card strong {{ font-size: 1rem; }}
    .luck-sort-controls button {{ flex: 1; }}
    .luck-row {{ grid-template-columns: 40px minmax(0,1fr) 82px; }}
    .luck-score {{ width: 36px; height: 36px; font-size: .7rem; }}
    .luck-item-grid {{ grid-template-columns: 1fr; }}
  }}
  @media (max-width: 430px) {{
    .luck-summary-card {{ grid-template-columns: 1fr; }}
    .luck-orbit {{ display: none; }}
    .luck-workspace {{ padding: 11px; }}
    .luck-row {{ grid-template-columns: 38px minmax(0,1fr); }}
    .luck-row-numbers {{ grid-column: 2; display: flex; gap: 10px; text-align: left; }}
    .luck-detail-hero {{ grid-template-columns: 66px minmax(0,1fr); }}
    .luck-detail-visual,.luck-detail-image {{ width: 66px; height: 66px; font-size: 1rem; }}
  }}
  .fav-name {{ font-size: 0.78rem; min-width: 150px; max-width: 150px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}

  /* BOSS TAB — compact buttons */
  .boss-cat-header {{
    font-family: 'Cinzel', serif;
    font-size: 0.85rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--gold);
    margin: 22px 0 10px 0;
    padding: 7px 16px 7px 14px;
    border-left: 3px solid var(--gold-dim);
    border-bottom: 1px solid var(--border);
    background: linear-gradient(90deg, #261c0d 0%, #1a1208 50%, transparent 100%);
  }}
  .boss-cat-header:first-child {{ margin-top: 4px; }}
  .boss-btn-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: 8px;
    margin-bottom: 4px;
  }}
  .boss-btn {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-top: 2px solid var(--gold-dim);
    padding: 14px 16px;
    cursor: pointer;
    text-align: left;
    transition: border-color 0.15s, background 0.15s;
    font-family: inherit;
  }}
  .boss-btn:hover {{ border-color: var(--gold); background: var(--bg-card-hover); }}
  .boss-btn.active {{ border-top-color: var(--gold); border-color: var(--gold); background: #1a1508; }}
  .boss-btn .btn-name {{
    font-family: 'Cinzel', serif;
    font-size: 0.75rem;
    color: var(--gold);
    font-weight: 600;
    letter-spacing: 0.4px;
    display: block;
    margin-bottom: 6px;
  }}
  .boss-btn .btn-kc {{
    font-family: 'Cinzel', serif;
    font-size: 1.3rem;
    color: var(--gold-bright);
    font-weight: 700;
  }}
  .boss-btn .btn-kc-label {{
    font-size: 0.62rem;
    color: var(--text-muted);
    margin-left: 4px;
    letter-spacing: 1px;
  }}
  .boss-btn .btn-drops {{
    font-size: 0.7rem;
    color: var(--text-dim);
    margin-top: 5px;
  }}

  /* Boss detail panel */
  .boss-detail-panel {{
    background: var(--bg-card);
    border: 1px solid var(--border-bright);
    border-left: 3px solid var(--gold);
    margin-bottom: 20px;
    padding: 20px 22px;
  }}
  .boss-detail-header {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }}
  .boss-detail-name {{
    font-family: 'Cinzel', serif;
    font-size: 1.3rem;
    color: var(--gold);
    font-weight: 700;
    letter-spacing: 1px;
  }}
  .boss-detail-kc {{
    font-family: 'Cinzel', serif;
    font-size: 0.82rem;
    color: var(--text-muted);
    margin-top: 5px;
    letter-spacing: 0.5px;
  }}
  .boss-detail-close {{
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 1.6rem;
    cursor: pointer;
    line-height: 1;
    padding: 0 4px;
  }}
  .boss-detail-close:hover {{ color: var(--gold); }}
  .boss-shots-expanded {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 8px;
  }}
  .boss-shot {{
    background: var(--bg);
    border: 1px solid var(--border);
    cursor: pointer;
    transition: border-color 0.15s;
  }}
  .boss-shot:hover {{ border-color: var(--gold); }}
  .boss-shot img {{
    width: 100%;
    aspect-ratio: 16/10;
    object-fit: cover;
    display: block;
  }}
  .boss-shot-label {{
    font-size: 0.7rem;
    color: var(--text-muted);
    padding: 5px 8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .boss-shot-gp {{
    color: var(--gold-bright);
    margin-left: 4px;
    font-family: 'Cinzel', serif;
    font-size: 0.65rem;
  }}
  .boss-shot-source {{
    font-size: 0.62rem;
    color: var(--text-dim);
    font-style: italic;
    padding: 0 8px 5px 8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    border-top: 1px dashed var(--border);
    margin-top: -2px;
    padding-top: 4px;
  }}
  .boss-no-drops {{
    font-size: 0.85rem;
    color: var(--text-dim);
    font-style: italic;
    padding: 8px 0;
  }}
  .boss-achievement-section {{
    margin-top: 18px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
  }}
  .boss-achievement-header {{
    font-family: 'Cinzel', serif;
    font-size: 0.68rem;
    color: #6a9a40;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    margin-bottom: 10px;
  }}
  .boss-achievement-shot {{
    background: var(--bg);
    border: 1px solid #2a4a15;
    cursor: pointer;
    transition: border-color 0.15s;
  }}
  .boss-achievement-shot:hover {{ border-color: #6a9a40; }}

  .total-gp-banner {{
    background: linear-gradient(135deg, #1a1508, #120f05);
    border: 1px solid var(--gold-dim);
    border-left: 3px solid var(--gold);
    padding: 16px 24px;
    margin-bottom: 20px;
    display: flex;
    align-items: baseline;
    gap: 12px;
  }}
  .total-gp-banner .gp-label {{ font-family: 'Cinzel', serif; color: var(--text-muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 1.5px; }}
  .total-gp-banner .gp-value {{ font-family: 'Cinzel', serif; font-size: 2.2rem; font-weight: 700; color: var(--gold-bright); text-shadow: 0 0 20px #f0c04044; }}
  .total-gp-banner .gp-count {{ font-family: 'Cinzel', serif; color: var(--text-dim); font-size: 0.8rem; margin-left: auto; letter-spacing: 1px; }}

  /* ── Chronicle ──────────────────────────────────────────────── */
  /* THIS WEEK IN GIELINOR */
  .memory-week {{ margin-bottom: 24px; }}
  .memory-week-head {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    margin-bottom: 16px;
  }}
  .memory-week-kicker {{
    font-family: 'Cinzel', serif;
    color: var(--gold);
    font-size: 0.78rem;
    letter-spacing: 1.8px;
    text-transform: uppercase;
  }}
  .memory-week-meta {{ color: #c8bfae; font-size: 0.82rem; margin-top: 5px; }}
  .memory-nav {{ display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
  .memory-nav button {{
    width: 30px;
    height: 30px;
    background: #0d0a06;
    border: 1px solid var(--border-bright);
    color: #d8cdb8;
    cursor: pointer;
    font-size: 1rem;
  }}
  .memory-nav button:hover {{ color: var(--gold-bright); border-color: var(--gold); }}
  .memory-nav button:disabled {{ color: #756a58; border-color: var(--border); cursor: default; }}
  .memory-page-label {{
    color: #c8bfae;
    font-family: 'Cinzel', serif;
    font-size: 0.66rem;
    min-width: 56px;
    text-align: center;
    letter-spacing: 0.7px;
  }}
  .memory-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  .memory-card {{
    background: #0d0a06;
    border: 1px solid var(--border);
    cursor: pointer;
    min-width: 0;
    transition: border-color 0.15s, transform 0.15s;
  }}
  .memory-card:hover {{ border-color: var(--gold); transform: translateY(-2px); }}
  .memory-card img {{
    width: 100%;
    aspect-ratio: 16/10;
    object-fit: cover;
    display: block;
    border-bottom: 1px solid var(--border);
  }}
  .memory-card-body {{ padding: 11px 12px 12px; }}
  .memory-card-top {{ display: flex; align-items: center; gap: 7px; margin-bottom: 7px; }}
  .memory-badge {{
    font-family: 'Cinzel', serif;
    font-size: 0.58rem;
    letter-spacing: 1px;
    padding: 2px 6px;
    border: 1px solid currentColor;
  }}
  .memory-year {{ color: #c8bfae; font-size: 0.72rem; margin-left: auto; }}
  .memory-title {{
    font-family: 'Cinzel', serif;
    color: #e6dbc4;
    font-size: 0.8rem;
    line-height: 1.35;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .memory-sub {{ color: #c8bfae; font-size: 0.76rem; margin-top: 4px; }}
  .memory-date {{ color: #b6aa94; font-size: 0.7rem; margin-top: 8px; }}

  .chron-controls {{
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .chron-filter-btn {{
    font-family: 'Cinzel', serif;
    background: var(--bg-card);
    border: 1px solid var(--border-bright);
    color: var(--text-muted);
    font-size: 0.68rem;
    padding: 6px 14px;
    cursor: pointer;
    letter-spacing: 1px;
    text-transform: uppercase;
    transition: all 0.15s;
  }}
  .chron-filter-btn:hover {{ color: var(--gold); border-color: var(--gold-dim); }}
  .chron-filter-btn.active {{ color: var(--gold); border-color: var(--gold); background: #1a1510; }}
  .chron-count {{ color: var(--text-dim); font-size: 0.78rem; margin-left: 8px; }}
  .chron-year-nav {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 24px;
  }}
  .chron-year-chip {{
    font-family: 'Cinzel', serif;
    font-size: 0.68rem;
    padding: 4px 10px;
    border: 1px solid var(--border);
    background: var(--bg-card);
    color: var(--text-muted);
    cursor: pointer;
    letter-spacing: 1px;
    transition: all 0.15s;
  }}
  .chron-year-chip:hover {{ color: var(--gold); border-color: var(--gold-dim); }}
  .chron-timeline {{
    position: relative;
    padding-left: 32px;
  }}
  .chron-line {{
    position: absolute;
    left: 8px;
    top: 0;
    bottom: 0;
    width: 2px;
    background: linear-gradient(to bottom, transparent 0%, var(--border-bright) 3%, var(--border-bright) 97%, transparent 100%);
  }}
  .chron-year-section {{ margin-bottom: 28px; }}
  .chron-year-heading {{
    font-family: 'Cinzel', serif;
    font-size: 0.78rem;
    color: var(--gold-dim);
    letter-spacing: 3px;
    margin-bottom: 10px;
    text-transform: uppercase;
  }}
  .yr-recap {{
    border: 1px solid var(--border);
    border-left: 3px solid var(--gold-dim);
    background: linear-gradient(90deg, #1d150a 0%, #15100a 60%, transparent 100%);
    padding: 12px 16px;
    margin: 0 0 16px 0;
  }}
  .yr-chips {{ display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 7px; }}
  .yr-chip {{ font-size: 0.72rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; }}
  .yr-chip b {{ font-family: 'Cinzel', serif; font-size: 0.95rem; color: var(--gold); margin-right: 5px; }}
  .yr-prose {{ font-size: 0.82rem; line-height: 1.55; margin: 0; }}
  .chron-entry {{
    display: flex;
    align-items: flex-start;
    margin-bottom: 8px;
    position: relative;
  }}
  .chron-dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    position: absolute;
    left: -27px;
    top: 10px;
    border: 2px solid var(--bg);
    flex-shrink: 0;
  }}
  .chron-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    padding: 10px 14px;
    flex: 1;
    display: flex;
    gap: 12px;
    align-items: center;
    cursor: pointer;
    transition: border-color 0.15s;
  }}
  .chron-card:hover {{ border-color: var(--border-bright); }}
  .chron-card.no-shot {{ cursor: default; }}
  .chron-card.no-shot:hover {{ border-color: var(--border); }}
  .chron-thumb {{
    width: 72px;
    height: 52px;
    object-fit: cover;
    flex-shrink: 0;
    border: 1px solid var(--border);
  }}
  .chron-body {{ flex: 1; min-width: 0; }}
  .chron-badge {{
    font-family: 'Cinzel', serif;
    font-size: 0.58rem;
    padding: 2px 6px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    margin-bottom: 4px;
    display: inline-block;
  }}
  .chron-title {{
    font-family: 'Cinzel', serif;
    font-size: 0.82rem;
    color: var(--text);
    margin-bottom: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .chron-sub {{ font-size: 0.78rem; color: var(--text-muted); }}
  .chron-date {{
    font-size: 0.72rem;
    color: var(--text-dim);
    font-family: 'Cinzel', serif;
    letter-spacing: 0.5px;
    white-space: nowrap;
    margin-left: auto;
    padding-left: 12px;
    flex-shrink: 0;
  }}

  footer {{
    text-align: center;
    padding: 24px;
    color: var(--text-dim);
    font-family: 'Cinzel', serif;
    font-size: 0.65rem;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    border-top: 1px solid var(--border);
    margin-top: 16px;
  }}

  /* Feedback lives in the navigation rail, which is always on screen, so
     reporting something never depends on scrolling to the bottom of a long
     page. Hidden entirely unless the local service is running: a statically
     opened HTML file cannot gather diagnostics, and a Report Issue button
     that files a report with no version in it is worse than no button. */
  .rail-foot {{ margin-top: auto; }}
  .rail-feedback {{ display: none; flex-direction: column; gap: 2px; padding: 11px 0 2px; border-top: 1px solid #342a18; }}
  .rail-feedback.ready {{ display: flex; }}
  .rail-fb-btn {{
    display: flex;
    align-items: center;
    gap: 9px;
    width: 100%;
    height: 30px;
    padding: 0 11px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 2px;
    color: var(--text-dim);
    font: 600 9px 'Cinzel', serif;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    text-align: left;
    cursor: pointer;
    transition: 0.16s ease;
  }}
  .rail-fb-btn:hover {{ color: var(--gold-bright); background: #18150e; border-color: #3f331e; }}
  .rail-fb-btn svg {{ width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; flex: none; }}

  .feedback-btn {{
    display: inline-flex;
    align-items: center;
    gap: 7px;
    height: 32px;
    padding: 0 13px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-muted);
    font: 600 9px 'Cinzel', serif;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
  }}
  .feedback-btn:hover {{ border-color: var(--gold-dim); color: var(--gold-bright); }}

  .fb-modal {{
    display: none;
    position: fixed;
    inset: 0;
    z-index: 200;
    background: rgba(4, 3, 1, 0.82);
    padding: 24px;
    overflow-y: auto;
  }}
  .fb-modal.open {{ display: block; }}
  .fb-panel {{
    max-width: 620px;
    margin: 48px auto;
    background: var(--bg-card);
    border: 1px solid var(--border-bright);
    padding: 22px 24px 24px;
  }}
  .fb-panel h3 {{
    font-family: 'Cinzel', serif;
    font-size: 0.95rem;
    color: var(--gold-bright);
    letter-spacing: 1px;
    margin: 0 0 6px;
  }}
  .fb-lede {{ color: var(--text-dim); font-size: 0.76rem; margin: 0 0 16px; line-height: 1.5; }}
  .fb-field {{ margin-bottom: 14px; }}
  .fb-field label {{
    display: block;
    font: 600 9px 'Cinzel', serif;
    letter-spacing: 0.7px;
    text-transform: uppercase;
    color: var(--text-muted);
    margin-bottom: 5px;
  }}
  .fb-field input, .fb-field textarea {{
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    font-family: 'Crimson Text', Georgia, serif;
    font-size: 0.88rem;
    padding: 8px 10px;
    box-sizing: border-box;
  }}
  .fb-field textarea {{ min-height: 132px; resize: vertical; line-height: 1.5; }}
  .fb-field input:focus, .fb-field textarea:focus {{ outline: none; border-color: var(--gold-dim); }}
  .fb-diag {{
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 9px 11px;
    color: var(--text-dim);
    font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
    font-size: 0.7rem;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }}
  .fb-actions {{ display: flex; justify-content: flex-end; gap: 9px; margin-top: 18px; }}
  .fb-actions .feedback-btn {{ height: 34px; }}
  .fb-actions .fb-primary {{
    background: #1c160a;
    border-color: var(--border-bright);
    color: var(--gold-bright);
  }}
  .fb-actions .fb-primary:hover {{ border-color: var(--gold); }}
  .fb-note {{ color: var(--text-dim); font-size: 0.72rem; margin: 14px 0 0; line-height: 1.5; }}

  .fb-release {{ border-top: 1px solid var(--border); padding-top: 14px; margin-top: 14px; }}
  .fb-release:first-of-type {{ border-top: none; padding-top: 0; margin-top: 0; }}
  .fb-release-head {{ display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }}
  .fb-release-tag {{ font: 600 0.82rem 'Cinzel', serif; color: var(--gold-bright); letter-spacing: 0.5px; }}
  .fb-release-date {{ color: var(--text-dim); font-size: 0.72rem; }}
  .fb-release-current {{
    font: 600 8px 'Cinzel', serif; letter-spacing: 0.6px; text-transform: uppercase;
    color: var(--gold); border: 1px solid var(--gold-dim); padding: 1px 6px;
  }}
  .fb-release-notes {{
    color: var(--text-muted); font-size: 0.82rem; line-height: 1.6;
    margin: 7px 0 0; white-space: pre-wrap; word-break: break-word;
  }}

  @media (max-width: 760px) {{
    .fb-panel {{ margin: 16px auto; padding: 18px; }}
  }}

  @media (max-width: 900px) {{
    .grid-2, .grid-3 {{ grid-template-columns: 1fr; }}
    .pulse-header {{ flex-direction: column; }}
    .pulse-metrics {{ grid-template-columns: repeat(2, 1fr); }}
    .pulse-breakdowns {{ grid-template-columns: 1fr; }}
    .memory-week-head {{ flex-direction: column; }}
    .memory-grid {{ grid-template-columns: repeat(2, 1fr); }}
    header {{ grid-template-columns: 1fr; align-items: start; }}
    .header-title, .app-controls, .header-meta {{ grid-column: 1; grid-row: auto; justify-self: start; }}
    .header-meta {{ text-align: left; }}
    .app-controls {{ align-items: flex-start; }}
    .page {{ padding: 20px; }}
    nav {{ padding: 0 16px; }}
  }}
  /* APPROVED REDESIGN SHELL — isolated implementation worktree. */
  .svg-defs {{ position:absolute; width:0; height:0; overflow:hidden; }}
  #side-rail {{ position:fixed; inset:0 auto 0 0; width:188px; padding:18px 12px 15px; display:flex; flex-direction:column; background:linear-gradient(180deg,#12100c,#0c0b08); border:0; border-right:1px solid var(--border); box-shadow:10px 0 28px rgba(0,0,0,.3); z-index:140; }}
  .nav-brand {{ display:flex; align-items:center; gap:10px; padding:2px 7px 19px; border-bottom:1px solid #342a18; }}
  .nav-brand-mark {{ width:38px; height:38px; display:grid; place-items:center; transform:rotate(45deg); border:1px solid var(--border-bright); background:#201907; box-shadow:inset 0 0 0 3px #0e0b06; flex:none; }}
  .nav-brand-mark span {{ transform:rotate(-45deg); font:600 18px 'Cinzel',serif; color:var(--gold-bright); }}
  .nav-brand-title {{ font:600 15px 'Cinzel',serif; color:var(--gold-bright); letter-spacing:.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .nav-brand-subtitle {{ font-size:11px; color:var(--text-muted); white-space:nowrap; }}
  #side-rail .nav-stack {{ display:flex; flex-direction:column; gap:5px; margin-top:18px; padding:0; background:none; border:0; box-shadow:none; position:static; }}
  #side-rail .nav-item {{ height:44px; width:100%; display:flex; align-items:center; gap:11px; padding:0 11px; margin:0; color:var(--text-muted); background:transparent; border:1px solid transparent; border-radius:2px; cursor:pointer; text-align:left; font:600 10px 'Cinzel',serif; letter-spacing:.4px; text-transform:uppercase; transition:.16s ease; }}
  #side-rail .nav-item svg, .menu-btn svg, .refresh-btn svg {{ width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.6; stroke-linecap:round; stroke-linejoin:round; flex:none; }}
  #side-rail .nav-item:hover {{ color:var(--text); background:#18150e; border-color:#3f331e; }}
  #side-rail .nav-item.active {{ color:var(--gold-bright); background:linear-gradient(90deg,#2a210d,#18150e); border-color:#76591c; box-shadow:inset 3px 0 0 var(--gold-bright); }}
  .rail-state {{ padding:13px 7px 0; border-top:1px solid #342a18; display:flex; gap:9px; align-items:flex-start; }}
  .rail-state-dot {{ width:7px; height:7px; border-radius:50%; background:#79a780; box-shadow:0 0 0 3px rgba(121,167,128,.12); margin-top:4px; flex:none; }}
  .rail-state strong,.rail-state span {{ display:block; }}
  .rail-state strong {{ font:600 8px 'Cinzel',serif; color:var(--text); letter-spacing:.35px; text-transform:uppercase; }}
  .rail-state span {{ color:var(--text-dim); font-size:10px; margin-top:3px; }}
  header {{ height:66px; margin-left:188px; padding:0; position:sticky; top:0; z-index:120; overflow:visible; display:block; background:rgba(10,9,7,.96); border-bottom:1px solid var(--border); box-shadow:none; }}
  .header-inner {{ width:min(1600px,100%); height:100%; margin:0 auto; padding:0 14px; display:grid; grid-template-columns:minmax(0,1fr) auto auto; align-items:center; gap:18px; }}
  header::before {{ display:none; }}
  .header-title {{ grid-column:1; grid-row:1; justify-self:start; min-width:0; }}
  .header-title h1 {{ margin:0; font:600 20px 'Cinzel',serif; color:var(--gold-bright); letter-spacing:.55px; text-shadow:none; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .header-title h1 .page-context {{ color:var(--text); font-size:.72em; }}
  .header-title p {{ display:none; }}
  .menu-btn {{ display:none; width:36px; height:36px; place-items:center; background:#17130b; color:var(--gold-bright); border:1px solid var(--border-bright); cursor:pointer; }}
  .app-controls {{ grid-column:2; grid-row:1; min-width:0; flex-direction:row; gap:8px; }}
  .app-controls.ready {{ display:flex; }}
  .refresh-btn {{ height:34px; display:flex; align-items:center; gap:7px; padding:0 11px; color:var(--gold-bright); border:1px solid var(--border-bright); background:#1c160a; font:600 9px 'Cinzel',serif; letter-spacing:.4px; }}
  .app-status {{ position:absolute; top:48px; right:20px; max-width:360px; padding:5px 8px; background:#17130b; border:1px solid var(--border); font-size:11px; }}
  .header-meta {{ grid-column:3; grid-row:1; justify-self:end; display:flex; gap:14px; text-align:right; font-size:10px; line-height:1.25; color:var(--text-dim); }}
  .header-meta div {{ min-width:92px; }}
  .header-meta div:nth-child(n+3) {{ display:none; }}
  .header-meta span {{ display:block; margin-top:2px; color:var(--text-muted); font-size:11px; font-weight:400; }}
  .page {{ width:min(1600px,calc(100% - 188px)); max-width:none; margin:0 0 0 max(188px,calc(50% - 706px)); padding:14px; }}
  .card {{ background:linear-gradient(145deg,#15140e,#0f100d); border-color:#45371d; box-shadow:inset 0 0 0 1px rgba(0,0,0,.32); }}
  .card h2 {{ color:var(--gold-bright); font-size:1rem; letter-spacing:.5px; }}
  #page-stats.active {{ display:flex; flex-direction:column; gap:10px; }}
  #page-stats > * {{ margin-bottom:0 !important; }}
  .account-pulse {{ display:grid; grid-template-columns:minmax(245px,.75fr) minmax(0,2.25fr); gap:0; padding:14px 16px; }}
  .pulse-header {{ display:flex; flex-direction:column; justify-content:space-between; margin:0; padding-right:16px; border-right:1px solid #49391c; }}
  .pulse-window-controls {{ margin-top:16px; }}
  #pulse-content {{ padding-left:16px; min-width:0; }}
  .pulse-metrics {{ margin-bottom:12px; gap:0; border:1px solid #4d3b1c; }}
  .pulse-metric {{ border:0; border-right:1px solid #4d3b1c; background:#0d0e0b; padding:11px 12px; }}
  .pulse-metric:last-child {{ border-right:0; }}
  .pulse-breakdowns {{ gap:14px; }}
  .pulse-panel {{ background:transparent; border:0; padding:0; }}
  .pulse-panel-title {{ color:var(--text); }}
  .pulse-verdict {{ font-size:1.06rem; line-height:1.35; }}
  .pulse-meta {{ color:var(--text-muted); }}
  .pulse-bar {{ height:5px; }}
  .stats-lifetime-grid {{ grid-template-columns:repeat(6,1fr); gap:8px; margin:0; }}
  .stats-lifetime-grid .stat-card,.clue-grid .stat-card {{ padding:13px 10px; clip-path:none; text-align:left; background:linear-gradient(145deg,#17170f,#10110e); border-top-width:1px; }}
  .stats-lifetime-grid .stat-card .value,.clue-grid .stat-card .value {{ font-size:1.45rem; }}
  .stats-lifetime-grid .stat-card .label,.clue-grid .stat-card .label {{ font-size:.58rem; color:var(--text); margin-top:6px; }}
  .clue-grid {{ gap:8px; margin:0; }}
  .wealth-card {{ padding:14px 16px; }}
  .wealth-head {{ border-bottom:1px solid #49391c; padding-bottom:10px; }}
  .wealth-summary b {{ font:600 22px 'Cinzel',serif; color:var(--gold-bright); }}
  .wealth-control {{ height:31px; padding:0 10px; }}
  .wealth-chart-wrap {{ height:245px; }}
  .wealth-gain-wrap {{ height:82px; }}
  .wealth-pending {{ border-top:1px solid #49391c; padding-top:10px; }}
  .grid-2 {{ gap:10px; }}
  #page-stats .grid-2 .card {{ padding:14px; }}
  #page-stats canvas {{ max-width:100%; }}
  footer {{ margin-left:188px; padding:16px 24px; }}
  .legacy-nav {{ display:none !important; }}
  .nav-scrim {{ display:none; }}
  @media (max-width:1180px) {{
    .stats-lifetime-grid {{ grid-template-columns:repeat(3,1fr); }}
    .account-pulse {{ grid-template-columns:1fr; }}
    .pulse-header {{ padding:0 0 12px; border:0; border-bottom:1px solid #49391c; }}
    #pulse-content {{ padding:12px 0 0; }}
    .header-meta div:nth-child(2) {{ display:none; }}
  }}
  @media (max-width:760px) {{
    #side-rail {{ width:218px; transform:translateX(-105%); transition:transform .2s ease; }}
    #side-rail.open {{ transform:none; }}
    .nav-scrim.open {{ display:block; position:fixed; inset:0; background:rgba(0,0,0,.68); z-index:130; }}
    header {{ margin-left:0; height:62px; }}
    .header-inner {{ width:100%; padding:0 12px; grid-template-columns:auto minmax(0,1fr) auto; gap:10px; }}
    .menu-btn {{ display:grid; grid-column:1; grid-row:1; }}
    .header-title {{ grid-column:2; }}
    .header-title h1 {{ font-size:17px; }}
    .header-title h1 .page-context {{ display:none; }}
    .app-controls {{ grid-column:3; }}
    .refresh-btn {{ width:36px; padding:0; justify-content:center; }}
    .refresh-btn span {{ display:none; }}
    .header-meta {{ display:none; }}
    .page {{ width:100%; margin-left:0; padding:10px; }}
    footer {{ margin-left:0; }}
    .stats-lifetime-grid {{ grid-template-columns:repeat(2,1fr); }}
    .pulse-metrics {{ grid-template-columns:repeat(2,1fr); }}
    .pulse-metric:nth-child(2) {{ border-right:0; }}
    .pulse-metric:nth-child(-n+2) {{ border-bottom:1px solid #4d3b1c; }}
    .wealth-head {{ align-items:flex-start; flex-direction:column; }}
    .wealth-controls {{ width:100%; overflow-x:auto; padding-bottom:2px; }}
    .wealth-control {{ flex:1 0 auto; }}
    .wealth-chart-wrap {{ height:210px; }}
  }}
  @media (max-width:430px) {{
    .stats-lifetime-grid,.clue-grid {{ grid-template-columns:repeat(2,1fr); }}
    .pulse-breakdowns,.grid-2 {{ grid-template-columns:1fr; }}
  }}

  /* COMPLETE REDESIGN PAGE SYSTEM */
  #page-max.active,#page-bosses.active,#page-loot.active,#page-gallery.active,#page-chronicle.active {{ display:flex; flex-direction:column; gap:10px; }}
  #page-max > *,#page-bosses > *,#page-loot > *,#page-gallery > *,#page-chronicle > * {{ margin-bottom:0 !important; }}
  .section-heading,.journey-toolbar,.skill-journey-heading,.boss-directory-head,.loot-ledger-head,.stats-card-heading {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }}
  .section-heading,.journey-toolbar,.skill-journey-heading,.boss-directory-head,.loot-ledger-head {{ padding-bottom:10px; border-bottom:1px solid #49391c; }}
  .section-heading h2,.journey-toolbar h2,.skill-journey-heading h2,.boss-directory-head h2,.loot-ledger-head h2 {{ margin:0; padding:0; border:0; color:var(--gold-bright); font-size:1rem; }}
  .section-heading p,.journey-toolbar p,.boss-directory-head p,.loot-ledger-head p {{ margin:4px 0 0; color:#c8bfae; font:11px 'Segoe UI',Arial,sans-serif; }}
  .section-heading > span {{ color:var(--text-dim); font:10px 'Segoe UI',Arial,sans-serif; }}

  /* Stats fidelity */
  #page-stats {{ background:transparent; }}
  #page-stats .account-pulse {{ min-height:210px; padding:16px 18px; }}
  #page-stats .stats-lifetime-grid .stat-card {{ min-height:76px; border-left:2px solid #806225; }}
  #page-stats .clue-grid {{ grid-template-columns:repeat(4,1fr); }}
  #page-stats .wealth-card {{ display:grid; grid-template-columns:minmax(0,1fr); }}
  #page-stats .grid-2 > .card {{ min-width:0; }}
  .stats-card-heading {{ align-items:center; margin-bottom:12px; border-bottom:1px solid var(--border); }}
  .stats-card-heading h2 {{ border:0; margin:0; }}
  .stats-card-heading button {{ border:0; background:none; color:var(--gold); cursor:pointer; font:600 9px 'Cinzel',serif; text-transform:uppercase; }}
  .stats-card-heading button:hover {{ color:var(--gold-bright); }}
  .stats-activity-card {{ padding:14px 16px; }}
  .stats-activity-card canvas {{ max-height:220px; }}
  .stats-story-row {{ grid-template-columns:1.05fr .95fr; }}
  .stats-hof-card .hof-grid {{ grid-template-columns:repeat(4,1fr); }}

  /* Homepage — account cover, not a reskinned ledger */
  #page-stats.active {{ gap:12px; }}
  .home-cover {{
    order:1; min-height:385px; display:grid; grid-template-columns:minmax(285px,.68fr) minmax(0,1.32fr);
    padding:0; overflow:hidden; background:
      radial-gradient(circle at 16% 30%,rgba(200,164,90,.13),transparent 36%),
      linear-gradient(135deg,#18150d 0%,#0b0d0a 52%,#111008 100%);
  }}
  .home-cover-copy {{ display:flex; flex-direction:column; padding:30px 28px 24px; border-right:1px solid #59451f; }}
  .home-eyebrow,.home-section-head > div > span {{ color:var(--gold); font:600 9px 'Cinzel',serif; letter-spacing:1.5px; text-transform:uppercase; }}
  .home-cover-copy h2 {{ max-width:520px; margin:18px 0 10px; padding:0; border:0; color:#f0d18a; font-size:2.1rem; line-height:1.08; letter-spacing:.2px; }}
  .home-cover-copy > p {{ max-width:440px; margin:0; color:#d7cbb6; font:13px/1.55 'Segoe UI',Arial,sans-serif; }}
  .home-window-controls {{ margin-top:auto; padding-top:22px; }}
  .home-cover-actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }}
  .home-cover-actions button,.home-text-link {{ border:1px solid #6b5123; background:#11110d; color:var(--gold); padding:9px 12px; cursor:pointer; font:600 9px 'Cinzel',serif; letter-spacing:.45px; text-transform:uppercase; }}
  .home-cover-actions button:first-child {{ color:#161109; background:linear-gradient(135deg,#e2b84d,#9c6f21); border-color:#e4bd59; }}
  .home-cover-actions button:hover,.home-text-link:hover {{ border-color:var(--gold-bright); color:var(--gold-bright); }}
  .home-cover-actions button:first-child:hover {{ color:#0b0905; }}
  .home-cover-foot {{ display:flex; gap:15px; margin-top:17px; color:#b9aa8e; font:10px 'Segoe UI',Arial,sans-serif; }}
  .home-cover-foot span + span::before {{ content:'•'; margin-right:15px; color:#82672f; }}
  .home-moment-grid {{ display:grid; grid-template-columns:repeat(12,1fr); grid-template-rows:repeat(2,minmax(165px,1fr)); gap:5px; min-width:0; background:#080906; }}
  .home-moment {{ position:relative; min-width:0; overflow:hidden; padding:0; border:0; background:#10110d; color:inherit; cursor:pointer; text-align:left; }}
  .home-moment:nth-child(1) {{ grid-column:1 / 8; grid-row:1 / 3; }}
  .home-moment:nth-child(2) {{ grid-column:8 / 11; grid-row:1; }}
  .home-moment:nth-child(3) {{ grid-column:11 / 13; grid-row:1; }}
  .home-moment:nth-child(4) {{ grid-column:8 / 10; grid-row:2; }}
  .home-moment:nth-child(5) {{ grid-column:10 / 13; grid-row:2; }}
  .home-moment img {{ width:100%; height:100%; object-fit:cover; filter:saturate(.82) brightness(.78); transition:transform .25s ease,filter .25s ease; }}
  .home-moment:hover img {{ transform:scale(1.025); filter:saturate(1) brightness(.9); }}
  .home-moment::after {{ content:''; position:absolute; inset:0; background:linear-gradient(180deg,transparent 35%,rgba(3,4,2,.92)); pointer-events:none; }}
  .home-moment > span {{ position:absolute; z-index:1; left:11px; right:11px; bottom:10px; }}
  .home-moment small,.home-moment strong,.home-moment em {{ display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .home-moment small {{ color:#e1b952; font:600 8px 'Segoe UI',Arial,sans-serif; letter-spacing:1px; text-transform:uppercase; }}
  .home-moment strong {{ margin-top:3px; color:#f0eadf; font:600 10px 'Cinzel',serif; }}
  .home-moment em {{ margin-top:3px; color:#c8bfae; font:normal 9px 'Segoe UI',Arial,sans-serif; }}
  .home-moment-featured > span {{ left:18px; right:18px; bottom:16px; }}
  .home-moment-featured strong {{ font-size:1.05rem; }}
  .home-record-strip {{ order:2; display:grid; grid-template-columns:repeat(6,1fr); border:1px solid #49391c; background:linear-gradient(90deg,#10110d,#17150e,#10110d); }}
  .home-record-strip article {{ min-width:0; padding:14px 15px; border-right:1px solid #49391c; }}
  .home-record-strip article:last-child {{ border-right:0; }}
  .home-record-strip span,.home-record-strip small {{ display:block; color:#c8bfae; font:10px 'Segoe UI',Arial,sans-serif; }}
  .home-record-strip span {{ color:#d8c595; font:600 8px 'Cinzel',serif; letter-spacing:.8px; text-transform:uppercase; }}
  .home-record-strip strong {{ display:block; margin:5px 0 2px; color:var(--gold-bright); font:600 1.45rem 'Cinzel',serif; }}
  .home-record-strip strong em {{ color:#97886b; font:normal .7rem 'Segoe UI',Arial,sans-serif; }}
  .home-momentum-card {{ order:3; padding:16px 18px; }}
  .home-section-head {{ display:flex; justify-content:space-between; align-items:flex-start; gap:14px; margin-bottom:12px; padding-bottom:10px; border-bottom:1px solid #49391c; }}
  .home-section-head h2 {{ margin:4px 0 0; padding:0; border:0; color:var(--gold-bright); font-size:1rem; }}
  .home-section-head > small {{ color:#b9aa8e; font:10px 'Segoe UI',Arial,sans-serif; }}
  .home-section-head .home-text-link {{ padding:7px 9px; border:0; background:transparent; white-space:nowrap; }}
  .home-direction-grid {{ order:4; display:grid; grid-template-columns:1.1fr .9fr; gap:12px; }}
  .home-next-card,.home-clue-card {{ padding:16px 18px; }}
  .home-next-number {{ color:var(--gold-bright); font:600 2rem 'Cinzel',serif; }}
  .home-next-card > p {{ margin:2px 0 14px; color:#c8bfae; font:11px 'Segoe UI',Arial,sans-serif; }}
  .home-next-stats {{ display:grid; grid-template-columns:1fr 1fr; margin-bottom:12px; border:1px solid #49391c; }}
  .home-next-stats div {{ padding:10px; border-right:1px solid #49391c; }}
  .home-next-stats div:last-child {{ border:0; }}
  .home-next-stats strong,.home-next-stats span {{ display:block; }}
  .home-next-stats strong {{ color:#dfc474; font:600 12px 'Cinzel',serif; }}
  .home-next-stats span {{ margin-top:4px; color:#b9aa8e; font:9px 'Segoe UI',Arial,sans-serif; }}
  .home-clue-ledger {{ display:grid; grid-template-columns:repeat(4,1fr); min-height:94px; border:1px solid #49391c; }}
  .home-clue-ledger div {{ display:flex; flex-direction:column; justify-content:center; align-items:center; border-right:1px solid #49391c; }}
  .home-clue-ledger div:last-child {{ border:0; }}
  .home-clue-ledger strong {{ font:600 1.5rem 'Cinzel',serif; }}
  .home-clue-ledger span {{ margin-top:5px; color:#c8bfae; font:8px 'Cinzel',serif; text-transform:uppercase; }}
  .home-wealth-card {{ order:5; }}
  .home-wealth-details {{ margin-top:10px; border-top:1px solid #49391c; }}
  .home-wealth-details summary {{ padding:12px 0 2px; color:#d4c4a0; cursor:pointer; font:600 9px 'Cinzel',serif; letter-spacing:.5px; text-transform:uppercase; }}
  .home-wealth-details .wealth-pending {{ margin-top:10px; }}
  .home-legacy-grid {{ order:6; margin:0 !important; }}
  .home-scroll-card {{ min-height:0; padding:15px 16px !important; }}
  .home-scroll-card #nineties-timeline,.home-scroll-card #fav-bosses {{ max-height:340px; overflow:auto; padding-right:7px; }}
  .home-activity-card {{ order:7; margin:0 !important; padding:15px 16px; }}
  .home-activity-card canvas {{ max-height:190px; }}
  .stats-story-row {{ order:8; margin:0 !important; }}
  .stats-pets-card,.stats-value-card,.stats-hof-card {{ padding:15px 16px !important; }}
  #page-stats .stats-pets-card .pet-thumb-grid {{ display:grid; grid-template-columns:repeat(5,1fr); gap:8px; }}
  #page-stats .stats-pets-card .pet-thumb {{ width:auto; min-width:0; }}
  #page-stats .stats-pets-card .pet-thumb:nth-child(n+6) {{ display:none; }}
  #page-stats .stats-pets-card .pet-thumb img {{ height:92px; }}
  .stats-hof-card {{ order:9; }}
  .stats-hof-card .hof-grid {{ grid-template-columns:repeat(4,1fr); }}
  .stats-hof-card .hof-item {{ min-width:0; padding:7px; clip-path:none; }}
  .hof-shot {{ width:100%; height:112px; object-fit:cover; margin-bottom:8px; border:1px solid #5a451e; }}
  .stats-hof-card .hof-label {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}

  /* Maxing Journey */
  .journey-summary-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }}
  .journey-summary-card {{ min-height:82px; display:grid; grid-template-columns:58px minmax(0,1fr); gap:12px; align-items:center; padding:12px 14px; border:1px solid #45371d; background:linear-gradient(145deg,#17170f,#10110e); }}
  .journey-summary-card > span {{ width:56px; height:56px; display:grid; place-items:center; border:1px solid var(--gold-dim); border-radius:50%; background:#12130f; color:var(--gold-bright); font:700 10px 'Cinzel',serif; letter-spacing:.15px; }}
  .journey-summary-card strong,.journey-summary-card small {{ display:block; }}
  .journey-summary-card strong {{ color:var(--gold-bright); font:600 1.2rem 'Cinzel',serif; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .journey-summary-card small {{ margin-top:4px; color:#d8cdb8; font:10px 'Segoe UI',Arial,sans-serif; text-transform:uppercase; }}
  .journey-road-card,.account-journey-card,.skill-journey-card {{ padding:14px 16px; }}
  .journey-road-layout {{ display:grid; grid-template-columns:minmax(0,1.25fr) minmax(350px,.75fr); gap:18px; padding-top:11px; }}
  #rtm-detail {{ display:grid; grid-template-columns:1fr 1fr; gap:5px 14px; align-content:start; }}
  #rtm-detail .rtm-detail-row {{ padding:7px 8px; border:1px solid #40351f; background:#0d0f0c; }}
  #rtm-detail .rtm-detail-top {{ margin-bottom:5px; }}
  #rtm-detail .rtm-detail-meta {{ font-family:'Segoe UI',Arial,sans-serif; font-size:10px; }}
  .journey-xp-panel {{ min-width:0; padding-left:16px; border-left:1px solid #49391c; }}
  .journey-xp-panel h3 {{ margin-bottom:9px; color:var(--text); font:600 10px 'Cinzel',serif; text-transform:uppercase; }}
  .journey-xp-panel canvas {{ max-height:240px; }}
  .journey-controls {{ display:grid; gap:6px; }}
  .journey-controls > div {{ display:flex; justify-content:flex-end; gap:5px; }}
  .journey-controls button,.boss-category-tabs button,.boss-controls button {{ height:30px; padding:0 9px; border:1px solid var(--border-bright); background:#0d0e0b; color:#c8bfae; cursor:pointer; font:600 8px 'Cinzel',serif; text-transform:uppercase; }}
  .journey-controls button:hover,.journey-controls button.active,.boss-category-tabs button:hover,.boss-category-tabs button.active,.boss-controls button:hover {{ color:var(--gold-bright); border-color:var(--gold); background:#2a210d; }}
  .journey-focus-shell {{ overflow:auto; padding-top:11px; }}
  #journey-focus-map {{ min-width:760px; }}
  .journey-axis,.journey-focus-row {{ width:100%; box-sizing:border-box; display:grid; grid-template-columns:100px repeat(var(--month-count),minmax(18px,1fr)); gap:3px; align-items:center; }}
  .journey-axis {{ margin-bottom:5px; }}
  .journey-axis span {{ color:var(--text-dim); font:9px 'Segoe UI',Arial,sans-serif; text-align:center; }}
  .journey-axis strong,.journey-focus-row > strong {{ color:var(--text); font:600 9px 'Cinzel',serif; }}
  .journey-focus-row {{ min-height:26px; border-bottom:1px solid #2e291d; }}
  .journey-cell {{ height:16px; border:1px solid transparent; background:#11110d; }}
  button.journey-cell {{ cursor:pointer; background:var(--skill-color); border-color:color-mix(in srgb,var(--skill-color) 70%,#fff 15%); box-shadow:0 0 5px color-mix(in srgb,var(--skill-color) 40%,transparent); }}
  button.journey-cell:hover,button.journey-cell:focus-visible {{ filter:brightness(1.25); outline:1px solid var(--gold); }}
  .journey-coverage {{ margin-top:9px; color:#c8bfae; font:10px 'Segoe UI',Arial,sans-serif; }}
  .skill-journey-heading > div > span {{ color:var(--gold); font:600 8px 'Cinzel',serif; text-transform:uppercase; }}
  .skill-journey-heading label {{ color:#c8bfae; font:10px 'Segoe UI',Arial,sans-serif; }}
  .skill-journey-heading select {{ margin-left:7px; height:30px; min-width:145px; border:1px solid var(--border-bright); background:#0d0e0b; color:var(--gold-bright); }}
  #journey-sequence {{ display:flex; gap:8px; overflow:auto; padding:18px 2px 8px; }}
  .journey-level,.journey-gap {{ flex:none; text-align:center; }}
  .journey-level button {{ width:42px; height:42px; border-radius:50%; border:1px solid var(--gold); background:#b98c24; color:#0d0d09; cursor:pointer; font:600 13px 'Cinzel',serif; }}
  .journey-level span {{ display:block; margin-bottom:4px; color:#c8bfae; font:9px 'Segoe UI',Arial,sans-serif; }}
  .journey-gap {{ min-width:92px; height:42px; margin-top:16px; display:grid; place-items:center; border:1px dashed var(--gold-dim); color:#c8bfae; font:9px 'Segoe UI',Arial,sans-serif; }}

  /* Bosses */
  .boss-summary-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }}
  .boss-summary-grid article,.loot-summary-grid article {{ min-height:76px; padding:12px 14px; border:1px solid #45371d; background:linear-gradient(145deg,#17170f,#10110e); }}
  .boss-summary-grid strong,.boss-summary-grid span,.boss-summary-grid small,.loot-summary-grid strong,.loot-summary-grid span,.loot-summary-grid small {{ display:block; }}
  .boss-summary-grid strong,.loot-summary-grid strong {{ color:var(--gold-bright); font:600 1.25rem 'Cinzel',serif; }}
  .boss-summary-grid span,.loot-summary-grid span {{ margin-top:5px; color:var(--text); font:600 9px 'Cinzel',serif; text-transform:uppercase; }}
  .boss-summary-grid small,.loot-summary-grid small {{ margin-top:3px; color:#c8bfae; font:10px 'Segoe UI',Arial,sans-serif; }}
  .boss-workspace {{ padding:12px 14px; }}
  .boss-category-tabs {{ display:flex; gap:5px; overflow:auto; padding-bottom:9px; border-bottom:1px solid #49391c; }}
  .boss-browser {{ display:grid; grid-template-columns:minmax(470px,.95fr) minmax(430px,1.05fr); gap:14px; padding-top:11px; }}
  .boss-directory-pane {{ min-width:0; }}
  .boss-controls {{ display:flex; gap:6px; }}
  .boss-controls input {{ width:180px; height:30px; padding:0 9px; border:1px solid var(--border-bright); background:#0d0e0b; color:var(--text); font:11px 'Segoe UI',Arial,sans-serif; }}
  .boss-card-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:6px; max-height:620px; overflow:auto; padding:8px 4px 0 0; }}
  .boss-directory-card {{ min-height:82px; display:grid; grid-template-columns:64px minmax(0,1fr); gap:9px; padding:7px; border:1px solid #44371f; background:#0d0f0c; color:inherit; cursor:pointer; text-align:left; }}
  .boss-directory-card:hover,.boss-directory-card.active {{ border-color:var(--gold); background:#18150d; }}
  .boss-directory-card.active {{ box-shadow:inset 3px 0 0 var(--gold-bright); }}
  .boss-directory-card img,.boss-card-monogram {{ width:64px; height:64px; object-fit:cover; border:1px solid #59451f; background:#171309; }}
  .boss-card-monogram {{ display:grid; place-items:center; color:var(--gold-bright); font:600 16px 'Cinzel',serif; }}
  .boss-directory-card strong {{ display:block; color:var(--text); font:600 10px 'Cinzel',serif; }}
  .boss-directory-card span,.boss-directory-card small {{ display:block; margin-top:4px; color:#c8bfae; font:10px 'Segoe UI',Arial,sans-serif; }}
  .boss-detail-pane {{ min-width:0; padding-left:14px; border-left:1px solid #49391c; }}
  .boss-detail-hero-new {{ display:grid; grid-template-columns:118px minmax(0,1fr); gap:12px; padding-bottom:11px; border-bottom:1px solid #49391c; }}
  .boss-detail-hero-new img,.boss-detail-monogram {{ width:118px; height:90px; object-fit:cover; border:1px solid var(--gold-dim); background:#171309; }}
  .boss-hero-shot {{ width:118px; height:90px; padding:0; border:0; background:none; cursor:pointer; }}
  .boss-hero-shot:hover img,.boss-hero-shot:focus-visible img {{ border-color:var(--gold-bright); }}
  .boss-detail-monogram {{ display:grid; place-items:center; color:var(--gold-bright); font:600 24px 'Cinzel',serif; }}
  .boss-detail-hero-new h2 {{ margin:0; padding:0; border:0; font-size:1.05rem; }}
  .boss-detail-hero-new p {{ margin:5px 0 0; color:#c8bfae; font:11px 'Segoe UI',Arial,sans-serif; }}
  .boss-evidence-stats {{ display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin:10px 0; }}
  .boss-evidence-stats div {{ padding:8px; border:1px solid #44371f; background:#0d0e0b; text-align:center; }}
  .boss-evidence-stats strong,.boss-evidence-stats span {{ display:block; }}
  .boss-evidence-stats strong {{ color:var(--gold-bright); font:600 14px 'Cinzel',serif; }}
  .boss-evidence-stats span {{ margin-top:3px; color:#c8bfae; font:9px 'Segoe UI',Arial,sans-serif; }}
  .boss-evidence-section {{ margin-top:10px; }}
  .boss-evidence-section h3 {{ margin-bottom:6px; color:var(--text); font:600 9px 'Cinzel',serif; text-transform:uppercase; }}
  .boss-evidence-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:6px; }}
  .boss-evidence-card {{ min-width:0; border:1px solid #44371f; background:#0d0f0c; cursor:pointer; color:inherit; text-align:left; }}
  .boss-evidence-card:hover {{ border-color:var(--gold); }}
  .boss-evidence-card img {{ width:100%; aspect-ratio:16/9; object-fit:cover; display:block; }}
  .boss-evidence-card span {{ display:block; padding:6px; overflow:hidden; color:#d8cdb8; font:10px 'Segoe UI',Arial,sans-serif; white-space:nowrap; text-overflow:ellipsis; }}

  /* Loot ledger */
  .loot-summary-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }}
  .loot-ledger {{ padding:14px 16px; }}
  .loot-ledger-head > div:first-child > span {{ color:var(--gold); font:600 8px 'Cinzel',serif; text-transform:uppercase; }}
  .loot-table-head,.loot-row {{ display:grid; grid-template-columns:minmax(220px,1.6fr) 130px 130px; gap:12px; align-items:center; }}
  .loot-table-head {{ padding:8px 10px; color:var(--text-dim); font:600 8px 'Cinzel',serif; text-transform:uppercase; }}
  .loot-row {{ min-height:76px; padding:8px 10px; border-top:1px solid #3f341f; }}
  .loot-row:hover {{ background:#15130c; }}
  .loot-row-main {{ display:grid; grid-template-columns:92px minmax(0,1fr); gap:10px; align-items:center; }}
  .loot-thumb-btn {{ width:92px; height:56px; padding:0; overflow:hidden; border:1px solid #59451f; background:#090a08; cursor:pointer; }}
  .loot-thumb-btn img {{ width:100%; height:100%; display:block; object-fit:cover; transition:transform .16s ease; }}
  .loot-thumb-btn:hover,.loot-thumb-btn:focus-visible {{ border-color:var(--gold-bright); outline:none; box-shadow:0 0 0 2px rgba(200,146,42,.18); }}
  .loot-thumb-btn:hover img,.loot-thumb-btn:focus-visible img {{ transform:scale(1.035); }}
  .loot-row strong,.loot-row span {{ display:block; }}
  .loot-row strong {{ color:var(--text); font:600 11px 'Cinzel',serif; }}
  .loot-row span {{ color:#c8bfae; font:10px 'Segoe UI',Arial,sans-serif; }}
  .loot-row-value {{ color:var(--gold-bright) !important; font:600 12px 'Cinzel',serif !important; }}

  /* Gallery and Chronicle */
  #page-gallery .gallery-controls {{ position:sticky; top:76px; z-index:20; margin:0; padding:10px; border:1px solid #45371d; background:rgba(15,16,13,.97); }}
  #page-gallery .gallery-grid {{ grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); }}
  #page-gallery .gallery-item {{ background:#0d0f0c; }}
  #page-gallery .gallery-item img {{ aspect-ratio:16/9; }}
  #page-chronicle .memory-week {{ padding:14px 16px; }}
  #page-chronicle .chron-controls {{ margin:0; padding:10px; border:1px solid #45371d; background:#11120e; }}
  .chron-story {{ position:relative; display:grid; grid-template-columns:220px minmax(0,1fr); gap:12px; margin-bottom:9px; padding:10px; border:1px solid #44371f; background:#0d0f0c; }}
  .chron-story-media {{ display:grid; grid-template-columns:2fr 1fr; grid-template-rows:1fr 1fr; gap:4px; min-height:126px; }}
  .chron-story-media button {{ padding:0; border:1px solid #49391c; background:#090a08; cursor:pointer; overflow:hidden; }}
  .chron-story-media button:first-child {{ grid-row:1/3; }}
  .chron-story-media img {{ width:100%; height:100%; object-fit:cover; display:block; }}
  .chron-story-media button:hover {{ border-color:var(--gold); }}
  .chron-story-body {{ min-width:0; }}
  .chron-story-date {{ color:var(--gold); font:600 9px 'Cinzel',serif; text-transform:uppercase; }}
  .chron-story-event {{ padding:7px 0; border-bottom:1px solid #2e291d; }}
  .chron-story-event:last-child {{ border-bottom:0; }}
  .chron-story-event strong,.chron-story-event span {{ display:block; }}
  .chron-story-event strong {{ color:var(--text); font:600 10px 'Cinzel',serif; }}
  .chron-story-event span {{ margin-top:3px; color:#c8bfae; font:10px 'Segoe UI',Arial,sans-serif; }}

  @media(max-width:1180px) {{
    .home-cover {{ grid-template-columns:minmax(255px,.78fr) minmax(0,1.22fr); }}
    .home-record-strip {{ grid-template-columns:repeat(3,1fr); }}
    .home-record-strip article:nth-child(3) {{ border-right:0; }}
    .home-record-strip article:nth-child(-n+3) {{ border-bottom:1px solid #49391c; }}
    .journey-summary-grid,.boss-summary-grid {{ grid-template-columns:1fr 1fr; }}
    .journey-road-layout {{ grid-template-columns:1fr; }}
    .journey-xp-panel {{ padding:12px 0 0; border:0; border-top:1px solid #49391c; }}
    .boss-browser {{ grid-template-columns:minmax(360px,.9fr) minmax(360px,1.1fr); }}
    .stats-hof-card .hof-grid {{ grid-template-columns:repeat(3,1fr); }}
  }}
  @media(max-width:900px) {{
    .home-cover {{ grid-template-columns:1fr; }}
    .home-cover-copy {{ min-height:300px; border:0; border-bottom:1px solid #59451f; }}
    .home-moment-grid {{ min-height:330px; }}
    .home-direction-grid {{ grid-template-columns:1fr; }}
    .boss-browser {{ grid-template-columns:1fr; }}
    .boss-card-grid {{ max-height:430px; }}
    .boss-detail-pane {{ padding:12px 0 0; border:0; border-top:1px solid #49391c; }}
    .loot-table-head {{ display:none; }}
    .loot-row {{ grid-template-columns:minmax(0,1fr) 110px 110px; }}
    .loot-row > span:nth-child(2),.loot-row > span:nth-child(3) {{ text-align:right; }}
    .chron-story {{ grid-template-columns:180px minmax(0,1fr); }}
  }}
  @media(max-width:760px) {{
    .home-cover-copy {{ padding:24px 20px 20px; }}
    .home-cover-copy h2 {{ font-size:1.65rem; }}
    .home-record-strip {{ grid-template-columns:1fr 1fr; }}
    .home-record-strip article:nth-child(3) {{ border-right:1px solid #49391c; }}
    .home-record-strip article:nth-child(2n) {{ border-right:0; }}
    .home-record-strip article:nth-child(-n+4) {{ border-bottom:1px solid #49391c; }}
    .home-moment-grid {{ grid-template-columns:1fr 1fr; grid-template-rows:210px 130px 130px; }}
    .home-moment:nth-child(1) {{ grid-column:1 / 3; grid-row:1; }}
    .home-moment:nth-child(2) {{ grid-column:1; grid-row:2; }}
    .home-moment:nth-child(3) {{ grid-column:2; grid-row:2; }}
    .home-moment:nth-child(4) {{ grid-column:1; grid-row:3; }}
    .home-moment:nth-child(5) {{ grid-column:2; grid-row:3; }}
    .home-clue-ledger {{ grid-template-columns:1fr 1fr; }}
    .home-clue-ledger div:nth-child(2) {{ border-right:0; }}
    .home-clue-ledger div:nth-child(-n+2) {{ border-bottom:1px solid #49391c; }}
    #page-stats .stats-pets-card .pet-thumb-grid {{ grid-template-columns:1fr 1fr; }}
    #page-stats .stats-pets-card .pet-thumb:nth-child(5) {{ display:none; }}
    #rtm-detail {{ grid-template-columns:1fr; }}
    .journey-toolbar,.skill-journey-heading,.boss-directory-head,.loot-ledger-head {{ display:block; }}
    .journey-controls,.boss-controls,.loot-sort-controls {{ margin-top:10px; }}
    .journey-controls > div {{ justify-content:flex-start; overflow:auto; }}
    .boss-controls input {{ flex:1; width:auto; }}
    .boss-card-grid {{ grid-template-columns:1fr; }}
    .boss-evidence-grid {{ grid-template-columns:1fr 1fr; }}
    .loot-summary-grid {{ grid-template-columns:1fr; }}
    .loot-row {{ grid-template-columns:1fr; gap:7px; }}
    .loot-row > span:nth-child(2),.loot-row > span:nth-child(3) {{ text-align:left; }}
    .chron-story {{ grid-template-columns:1fr; }}
    .chron-story-media {{ min-height:180px; }}
    .stats-story-row {{ grid-template-columns:1fr; }}
    .stats-hof-card .hof-grid {{ grid-template-columns:1fr 1fr; }}
  }}
  @media(max-width:430px) {{
    .home-cover-actions {{ display:grid; grid-template-columns:1fr; }}
    .home-cover-actions button {{ width:100%; }}
    .home-cover-foot {{ display:block; }}
    .home-cover-foot span {{ display:block; margin-top:4px; }}
    .home-cover-foot span + span::before {{ display:none; }}
    .home-section-head {{ display:block; }}
    .home-section-head .home-text-link {{ margin-top:7px; padding-left:0; }}
    .home-next-stats {{ grid-template-columns:1fr; }}
    .home-next-stats div {{ border:0; border-bottom:1px solid #49391c; }}
    #page-stats .clue-grid,.journey-summary-grid,.boss-summary-grid {{ grid-template-columns:1fr 1fr; }}
    .journey-summary-card {{ grid-template-columns:1fr; }}
    .journey-summary-card > span {{ display:none; }}
    .boss-detail-hero-new {{ grid-template-columns:86px minmax(0,1fr); }}
    .boss-detail-hero-new img,.boss-detail-monogram {{ width:86px; height:70px; }}
    .boss-evidence-stats {{ grid-template-columns:1fr; }}
    .boss-evidence-grid {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>

<svg class="svg-defs" aria-hidden="true">
  <symbol id="i-stats" viewBox="0 0 24 24"><path d="M4 19V10M10 19V5M16 19v-7M3 19h18"/></symbol>
  <symbol id="i-journey" viewBox="0 0 24 24"><path d="M5 19V6m0 0 3 3M5 6 2 9m7 10V9m0 0 3 3M9 9 6 12m7 7V4m0 0 3 3m-3-3-3 3m7 12v-8m0 0 3 3m-3-3-3 3"/></symbol>
  <symbol id="i-luck" viewBox="0 0 24 24"><path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8m0-12.8L5.6 18.4"/><circle cx="12" cy="12" r="3"/></symbol>
  <symbol id="i-boss" viewBox="0 0 24 24"><path d="m5 4 14 16M19 4 5 20M4 4l4 1-3 3-1-4Zm16 0-4 1 3 3 1-4Z"/></symbol>
  <symbol id="i-loot" viewBox="0 0 24 24"><path d="M4 8h16v11H4zM7 8V5h10v3M9 12h6M12 10v4"/></symbol>
  <symbol id="i-gallery" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="1"/><circle cx="8" cy="9" r="2"/><path d="m5 18 5-5 3 3 2-2 4 4"/></symbol>
  <symbol id="i-chronicle" viewBox="0 0 24 24"><path d="M5 4h12a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2V4Zm2 0v16M10 8h6m-6 4h6m-6 4h4"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M19 8a8 8 0 1 0 1 7M19 8V3m0 5h-5"/></symbol>
  <symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></symbol>
  <symbol id="i-report" viewBox="0 0 24 24"><path d="M12 4 2 20h20L12 4Zm0 6v5m0 3v.5"/></symbol>
  <symbol id="i-idea" viewBox="0 0 24 24"><path d="M9 18h6m-5 3h4M12 2a6 6 0 0 0-3.5 10.9V15h7v-2.1A6 6 0 0 0 12 2Z"/></symbol>
  <symbol id="i-cog" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M4.9 4.9l2.2 2.2m9.8 9.8 2.2 2.2M19.1 4.9l-2.2 2.2M7.1 16.9l-2.2 2.2"/></symbol>
  <symbol id="i-sparkle" viewBox="0 0 24 24"><path d="m12 3 2 5.5L19.5 10 14 12l-2 5.5L10 12 4.5 10 10 8.5 12 3Zm6.5 8.5.9 2.3 2.3.9-2.3.9-.9 2.3-.9-2.3-2.3-.9 2.3-.9.9-2.3Z"/></symbol>
</svg>

<aside id="side-rail" aria-label="Dashboard pages">
  <div class="nav-brand"><div class="nav-brand-mark"><span>{PLAYER_NAME[:1].upper()}</span></div><div><div class="nav-brand-title">{PLAYER_NAME}</div><div class="nav-brand-subtitle">Account Chronicle</div></div></div>
  <div class="nav-stack">
    <button class="nav-item active" onclick="switchPage('stats', this)"><svg><use href="#i-stats"/></svg><span>Stats</span></button>
    <button class="nav-item" onclick="switchPage('max', this)"><svg><use href="#i-journey"/></svg><span>Maxing Journey</span></button>
    <button class="nav-item" onclick="switchPage('luck', this)"><svg><use href="#i-luck"/></svg><span>Luck</span></button>
    <button class="nav-item" onclick="switchPage('bosses', this)"><svg><use href="#i-boss"/></svg><span>Bosses</span></button>
    <button class="nav-item" onclick="switchPage('loot', this)"><svg><use href="#i-loot"/></svg><span>Loot Log</span></button>
    <button class="nav-item" onclick="switchPage('gallery', this)"><svg><use href="#i-gallery"/></svg><span>Gallery</span></button>
    <button class="nav-item" onclick="switchPage('chronicle', this)"><svg><use href="#i-chronicle"/></svg><span>Chronicle</span></button>
  </div>
  <div class="rail-foot">
    <div class="rail-feedback" id="rail-feedback">
      <button class="rail-fb-btn" onclick="openReportDialog()"><svg><use href="#i-report"/></svg><span>Report an issue</span></button>
      <button class="rail-fb-btn" onclick="openFeatureDialog()"><svg><use href="#i-idea"/></svg><span>Request a feature</span></button>
      <button class="rail-fb-btn" onclick="openWhatsNew()"><svg><use href="#i-sparkle"/></svg><span>What&#39;s new</span></button>
      <button class="rail-fb-btn" onclick="openSettings()"><svg><use href="#i-cog"/></svg><span>Settings</span></button>
    </div>
    <div class="rail-state"><span class="rail-state-dot"></span><div><strong>Local dashboard</strong><span>Loopback only</span></div></div>
  </div>
</aside>
<div class="nav-scrim" id="nav-scrim" onclick="closeNavigation()"></div>

<header>
  <div class="header-inner">
    <button class="menu-btn" id="menu-btn" onclick="toggleNavigation()" aria-label="Open navigation"><svg><use href="#i-menu"/></svg></button>
    <div class="header-title">
      <h1><span id="page-heading-title">{PLAYER_NAME}</span> <span class="page-context" id="page-heading-subtitle">— Account Overview</span></h1>
      <p>Old School RuneScape · Screenshot Stats</p>
    </div>
    <div class="app-controls" id="app-controls">
      <button class="refresh-btn" id="refresh-btn" onclick="refreshDashboard()"><svg><use href="#i-refresh"/></svg><span>Refresh dashboard</span></button>
      <div class="app-status" id="app-status"></div>
    </div>
    <div class="header-meta">
      <div>Refreshed: <span>{refreshed_at_str}</span></div>
      <div>Total screenshots: <span>{data["total"]:,}</span></div>
      <div>First: <span>{first_str}</span></div>
      <div>Last active: <span>{last_str}</span></div>
      <div>{years_played}</div>
    </div>
  </div>
</header>

<nav class="legacy-nav" aria-hidden="true">
  <button class="active" onclick="switchPage('stats', this)">Stats</button>
  <button onclick="switchPage('max', this)">Road to Max</button>
  <button onclick="switchPage('luck', this)">Luck</button>
  <button onclick="switchPage('bosses', this)">Bosses</button>
  <button onclick="switchPage('loot', this)">Loot Log</button>
  <button onclick="switchPage('gallery', this)">Gallery</button>
  <button onclick="switchPage('chronicle', this)">Chronicle</button>
</nav>

<!-- STATS PAGE -->
<div id="page-stats" class="page active">
  <section class="card home-cover">
    <div class="home-cover-copy">
      <div class="home-eyebrow">The account right now</div>
      <h2 id="pulse-verdict">Building the current account story...</h2>
      <p id="pulse-meta">Recent progression will appear here automatically.</p>
      <div class="pulse-window-controls home-window-controls">
        <button class="pulse-window-btn active" onclick="setPulseWindow(7, this)">7 Days</button>
        <button class="pulse-window-btn" onclick="setPulseWindow(30, this)">30 Days</button>
      </div>
      <div class="home-cover-actions">
        <button onclick="goToPage('max')">Continue the maxing journey</button>
        <button onclick="goToPage('chronicle')">Open the Chronicle</button>
      </div>
      <div class="home-cover-foot"><span>{data["total"]:,} captured moments</span><span>{years_played}</span></div>
    </div>
    <div class="home-moment-grid">{home_moments_html}</div>
  </section>

  <section class="card home-momentum-card">
    <div class="home-section-head"><div><span>Recent movement</span><h2>Account Pulse</h2></div><small>Same source as Maxing Journey</small></div>
    <div id="pulse-content"></div>
  </section>

  <section class="home-record-strip" aria-label="Lifetime account record">
    <article><span>Total level</span><strong>{total_level}</strong><small>Current hiscores</small></article>
    <article><span>Skills mastered</span><strong>{max99s}<em>/24</em></strong><small>{len(road_to_max)} still ahead</small></article>
    <article><span>Pet drops</span><strong>{len(pets)}</strong><small>Screenshot backed</small></article>
    <article><span>Realized wealth</span><strong>{total_gp_str}</strong><small>Named evidence</small></article>
    <article><span>Collection log</span><strong>{clog_card_str}</strong><small>Slots recorded</small></article>
    <article><span>Luck percentile</span><strong>{luck_pct_str}</strong><small>{luck_verdict_str if luck_bosses else "No data yet"}</small></article>
  </section>

  <section class="home-direction-grid">
    <article class="card home-next-card">
      <div class="home-section-head"><div><span>The road ahead</span><h2>Next Chapter</h2></div></div>
      <div class="home-next-number">{total_xp_rem_str}</div><p>XP remains across {len(road_to_max)} skills.</p>
      <div class="home-next-stats"><div><strong>{pace_headline}</strong><span>Current pace</span></div><div><strong>{max_eta_str or "—"}</strong><span>{max_eta_label}</span></div></div>
      <button class="home-text-link" onclick="goToPage('max')">See every remaining skill →</button>
    </article>
    <article class="card home-clue-card">
      <div class="home-section-head"><div><span>Treasure trail record</span><h2>Clue Archive</h2></div></div>
      <div class="home-clue-ledger">{"".join(f'<div><strong style="color:{tier_colors[t]}">{clue_tiers.get(t,0):,}</strong><span>{t}</span></div>' for t in ["Medium","Hard","Elite","Master"])}</div>
    </article>
  </section>

  <div class="card wealth-card home-wealth-card">
    <h2>Wealth Progression</h2>
    <div class="wealth-head">
      <div class="wealth-summary">
        <b>{total_gp_str}</b> realized GP logged · {indirect_gp_str} from completed indirect drops<br>
        <span id="wealth-period-summary">Loading selected period...</span>
      </div>
      <div class="wealth-controls" aria-label="Wealth chart range">
        <button class="wealth-control" data-months="1" onclick="setWealthRange(1, this)">1 Month</button>
        <button class="wealth-control" data-months="6" onclick="setWealthRange(6, this)">6 Months</button>
        <button class="wealth-control active" data-months="ytd" onclick="setWealthRange('ytd', this)">YTD</button>
        <button class="wealth-control" data-months="24" onclick="setWealthRange(24, this)">2 Years</button>
        <button class="wealth-control" data-months="0" onclick="setWealthRange(0, this)">All Time</button>
      </div>
    </div>
    <div class="wealth-chart-wrap"><canvas id="gpChart"></canvas></div>
    <p class="wealth-chart-note" id="wealth-scale-note"></p>
    <div class="wealth-gain-wrap"><canvas id="gpGainChart"></canvas></div>
    <div class="wealth-tip" id="wealth-tooltip"></div>
    <details class="home-wealth-details">
    <summary>Pending assemblies and coverage · {pending_value_str} estimated</summary>
    <div class="wealth-pending">
      <div class="wealth-pending-label">Indirect value pending · {pending_value_str} estimated · {price_note}</div>
      {pending_html}
      {discovery_html}
    </div>
    </details>
  </div>

  <div class="grid-2 home-legacy-grid">
    <div class="card home-scroll-card">
      <div class="home-section-head"><div><span>Mastery</span><h2>99s Timeline</h2></div><button class="home-text-link" onclick="goToPage('max')">Full journey →</button></div>
      <div id="nineties-timeline"></div>
    </div>
    <div class="card home-scroll-card">
      <div class="home-section-head"><div><span>{f"{total_boss_kc:,} lifetime kills" if total_boss_kc else "Current hiscores"}</span><h2>Bossing Record</h2></div><button class="home-text-link" onclick="goToPage('bosses')">Boss directory →</button></div>
      <div id="fav-bosses"></div>
    </div>
  </div>

  <div class="card stats-activity-card home-activity-card">
    <div class="home-section-head"><div><span>Long-view rhythm</span><h2>Screenshot Activity</h2></div></div>
    <canvas id="timelineChart" height="120"></canvas>
  </div>

  <div class="grid-2 stats-story-row" style="margin-bottom:24px">
    <div class="card stats-pets-card">
      <div class="home-section-head"><div><span>Companions collected</span><h2>Pet Archive</h2></div><button class="home-text-link" onclick="openPetItem(0)">View all pets →</button></div>
      <div class="pet-thumb-grid">{pet_html}</div>
    </div>
    <div class="card stats-value-card">
      <div class="home-section-head"><div><span>The drops that changed the account</span><h2>Defining Value Events</h2></div><button class="home-text-link" onclick="goToPage('loot')">Full loot log →</button></div>
      {top5_html if top5_html else '<p class="empty-note">No valuable drop data parsed.</p>'}
    </div>
  </div>

    <div class="card stats-hof-card">
      <div class="home-section-head"><div><span>The moments worth remembering</span><h2>Hall of Fame</h2></div><button class="home-text-link" onclick="goToPage('chronicle')">Full Chronicle →</button></div>
      <div class="hof-grid">{hof_html}</div>
  </div>
</div>

<!-- ROAD TO MAX PAGE -->
<div id="page-max" class="page">
  <section class="journey-summary-grid">
    <article class="journey-summary-card"><span>XP</span><div><strong>{total_xp_rem_str}</strong><small>XP to max</small></div></article>
    <article class="journey-summary-card"><span>Skills</span><div><strong>{len(road_to_max)}</strong><small>Skills left</small></div></article>
    <article class="journey-summary-card"><span>Pace</span><div><strong>{pace_headline}</strong><small>XP per day</small></div></article>
    <article class="journey-summary-card"><span>ETA</span><div><strong>{max_eta_str or "—"}</strong><small>{max_eta_label}</small></div></article>
  </section>
  <section class="card journey-road-card">
    <div class="section-heading"><div><h2>Road to Max</h2><p>Current hiscores, recent pace, and XP-based progress.</p></div><span>Current hiscores snapshot</span></div>
    <div class="journey-road-layout"><div id="rtm-detail"></div><div class="journey-xp-panel"><h3>Daily XP Gained</h3><canvas id="xpTrendChart" height="160"></canvas><p class="empty-note" id="xp-trend-note" style="display:none">No trend to draw yet — a gain bar appears once you've refreshed on two different days.</p></div></div>
  </section>
  <section class="card account-journey-card">
    <div class="journey-toolbar"><div><h2>Account Journey</h2><p>Captured level-ups show where the account's attention moved over time.</p></div><div class="journey-controls"><div><button data-journey-range="1">1 Year</button><button class="active" data-journey-range="3">3 Years</button><button data-journey-range="all">All Time</button></div><div><button class="active" data-journey-mode="all">All Skills</button><button data-journey-mode="one">One Skill</button></div></div></div>
    <div class="journey-focus-shell"><div id="journey-focus-map"></div></div>
    <div class="journey-coverage" id="journey-coverage"></div>
  </section>
  <section class="card skill-journey-card">
    <div class="skill-journey-heading"><div><span>Screenshot-backed drilldown</span><h2 id="journey-skill-title">Skill Journey</h2></div><label>Skill <select id="journey-skill-select"></select></label></div>
    <div id="journey-sequence"></div>
  </section>
</div>

<!-- LUCK PAGE -->
<div id="page-luck" class="page">
  <section class="luck-summary-grid" aria-label="Luck summary">
    <article class="luck-summary-card"><div class="luck-orbit">Luck</div><div><strong>{luck_pct_str}</strong><span>Overall luck percentile</span><small>{luck_verdict_str if luck_bosses else "No data yet"}</small></div></article>
    <article class="luck-summary-card"><div class="luck-orbit">Σ</div><div><strong>{luck_uniques_str}</strong><span>Uniques · got / expected</span><small>Screenshot-backed ownership</small></div></article>
    <article class="luck-summary-card"><div class="luck-orbit">Flex</div><div><strong>{luck_rarest_value}</strong><span>Rarest flex</span><small>{luck_rarest_label.replace("Rarest Flex · ", "")}</small></div></article>
    <article class="luck-summary-card"><div class="luck-orbit">Dry</div><div><strong>{luck_driest_value}</strong><span>Driest grind</span><small>{luck_driest_label.replace("Driest · ", "")}</small></div></article>
  </section>
  <details class="luck-methodology">
    <summary>How luck is calculated</summary>
    <p>Expected uniques = tracked KC × wiki drop rate. Only bosses with {LUCK_MIN_KC}+ tracked kills and per-kill loot are measured. Raids and contribution bosses are excluded. Bosses with meaningful KC but no screenshot evidence are flagged and left out of the headline number. “Rarest flex” is the owned item—or copy count—fewest players at your KC would have.</p>
  </details>
  <section class="card luck-workspace">
    <div class="luck-workspace-head">
      <div><h2>Boss by Boss</h2><p>Expected uniques versus screenshot-backed ownership at the current tracked KC.</p></div>
      <div class="luck-sort-controls" aria-label="Sort bosses">
        <button class="active" data-luck-sort="blessed" onclick="sortLuck('blessed', this)">Most Blessed</button>
        <button data-luck-sort="cursed" onclick="sortLuck('cursed', this)">Most Cursed</button>
        <button data-luck-sort="gp" onclick="sortLuck('gp', this)">Most Profitable</button>
        <button data-luck-sort="kc" onclick="sortLuck('kc', this)">Highest KC</button>
      </div>
    </div>
    <div class="luck-browser">
      <section class="luck-list-panel" aria-label="Boss luck rankings">
        <div class="luck-list-key"><strong>Luck percentile</strong><span>Higher means luckier</span></div>
        <div class="luck-list" id="luck-list"></div>
      </section>
      <aside class="luck-detail" id="luck-detail" aria-live="polite"></aside>
    </div>
  </section>
</div>

<!-- GALLERY PAGE -->
<div id="page-gallery" class="page">
  <div class="card favorite-showcase" id="favorite-showcase" style="display:none">
    <div class="favorite-showcase-head">
      <h2>Favorite Memories</h2>
      <span class="favorite-showcase-count" id="favorite-showcase-count"></span>
    </div>
    <div class="gallery-grid" id="favorite-showcase-grid"></div>
  </div>
  <div class="gallery-controls">
    <input type="text" id="gallery-search" placeholder="Search screenshots..." oninput="filterGallery()">
    <button class="filter-btn active" data-cat="All" onclick="setFilter('All', this)">All</button>
    <button class="filter-btn" data-cat="Favorites" id="favorites-filter" style="display:none" onclick="setFilter('Favorites', this)">Favorites</button>
    {cat_filter_buttons}
    <span class="gallery-count" id="gallery-count"></span>
  </div>
  <div class="gallery-grid" id="gallery-grid"></div>
  <div class="gallery-more-wrap" id="gallery-more-wrap">
    <button class="gallery-more-btn" id="gallery-more-btn" onclick="loadMoreGallery()">Load more screenshots</button>
  </div>
</div>

<!-- BOSS PAGE -->
<div id="page-bosses" class="page">
  <section class="boss-summary-grid" id="boss-summary"></section>
  <section class="card boss-workspace">
    <div class="boss-category-tabs" id="boss-category-tabs"></div>
    <div class="boss-browser">
      <section class="boss-directory-pane">
        <div class="boss-directory-head"><div><h2>Boss Directory</h2><p>Choose an encounter to inspect its captured story.</p></div><div class="boss-controls"><input id="boss-search" type="search" placeholder="Search bosses..."><button id="boss-sort" type="button" onclick="toggleBossSort(this)">Most evidence</button></div></div>
        <div class="boss-card-grid" id="boss-grid"></div>
      </section>
      <aside class="boss-detail-pane" id="boss-detail-panel" aria-live="polite"></aside>
    </div>
  </section>
</div>

<!-- LOOT PAGE -->
<div id="page-loot" class="page">
  <section class="loot-summary-grid">
    <article><strong>{total_gp_str}</strong><span>Total GP logged</span><small>Realized screenshot evidence</small></article>
    <article><strong>{total_drops:,}</strong><span>Drop screenshots</span><small>Every row opens its evidence</small></article>
    <article><strong>{fmt_gp(top_value_events[0]['value']) if top_value_events else '—'}</strong><span>Largest value event</span><small>{top_value_events[0]['label'] if top_value_events else 'No parsed event'}</small></article>
  </section>
  <section class="card loot-ledger">
    <div class="loot-ledger-head"><div><span>Screenshot-backed wealth evidence</span><h2>The Drops Behind the Number</h2><p>Pending assemblies stay out of this realized-value ledger.</p></div><div class="loot-sort-controls"><button class="filter-btn active" onclick="sortLoot('value', this)">Highest Value</button><button class="filter-btn" onclick="sortLoot('date', this)">Newest First</button></div></div>
    <div class="loot-table-head"><span>Drop</span><span>Captured</span><span>Realized value</span></div>
    <div class="loot-rows" id="loot-grid"></div>
  </section>
</div>

<!-- CHRONICLE PAGE -->
<div id="page-chronicle" class="page">
  <div id="memory-week" class="card memory-week">
    <div class="memory-week-head">
      <div>
        <div class="memory-week-kicker">This Week in Gielinor</div>
        <div class="memory-week-meta">Moments from {memory_window_label} across past years</div>
      </div>
      <div class="memory-nav">
        <button id="memory-prev" onclick="changeMemoryPage(-1)" aria-label="Previous memories">‹</button>
        <span class="memory-page-label" id="memory-page-label"></span>
        <button id="memory-next" onclick="changeMemoryPage(1)" aria-label="Next memories">›</button>
      </div>
    </div>
    <div class="memory-grid" id="memory-grid"></div>
  </div>

  <div class="chron-controls">
    <button class="chron-filter-btn active" onclick="setChronFilter('all', this)">All</button>
    <button class="chron-filter-btn" onclick="setChronFilter('quest', this)">Quests</button>
    <button class="chron-filter-btn" onclick="setChronFilter('skill', this)">99s</button>
    <button class="chron-filter-btn" onclick="setChronFilter('pet', this)">Pets</button>
    <button class="chron-filter-btn" onclick="setChronFilter('drop', this)">Drops</button>
    <span class="chron-count" id="chron-count"></span>
  </div>
  <div class="chron-year-nav" id="chron-year-nav"></div>
  <div class="chron-timeline">
    <div class="chron-line"></div>
    <div id="chron-body"></div>
  </div>
</div>

<!-- LIGHTBOX -->
<div id="lightbox">
  <button class="lb-close" onclick="closeLightbox()">×</button>
  <button class="lb-nav lb-prev" onclick="lbNav(-1)">‹</button>
  <img id="lb-img" src="" alt="">
  <div class="lb-info" id="lb-info"></div>
  <button class="favorite-heart lb-favorite" id="lb-favorite" style="display:none" onclick="toggleLightboxFavorite(event)" aria-label="Favorite screenshot">&#9825;</button>
  <button class="lb-nav lb-next" onclick="lbNav(1)">›</button>
</div>

<div class="fb-modal" id="fb-modal" onclick="dismissFeedbackModal(event)">
  <div class="fb-panel" id="fb-panel" role="dialog" aria-modal="true" aria-labelledby="fb-title">
    <h3 id="fb-title"></h3>
    <p class="fb-lede" id="fb-lede"></p>
    <div id="fb-form">
      <div class="fb-field">
        <label for="fb-subject">Summary</label>
        <input type="text" id="fb-subject" maxlength="120" autocomplete="off">
      </div>
      <div class="fb-field">
        <label for="fb-detail" id="fb-detail-label">Details</label>
        <textarea id="fb-detail" maxlength="4000"></textarea>
      </div>
      <div class="fb-field">
        <label>Included automatically</label>
        <div class="fb-diag" id="fb-diag"></div>
      </div>
      <p class="fb-note">This opens GitHub with the form already filled in. Nothing is sent until you press Submit there, and you can edit anything first. No account name, file path, or screenshot leaves your machine.</p>
    </div>
    <div id="fb-releases"></div>
    <div class="fb-actions">
      <button class="feedback-btn" onclick="closeFeedbackModal()">Close</button>
      <button class="feedback-btn fb-primary" id="fb-submit" onclick="submitFeedback()">Continue to GitHub</button>
    </div>
  </div>
</div>

<footer>Generated by osrs_dashboard.py · {PLAYER_NAME} · {datetime.now().strftime("%B %d, %Y")}</footer>

<script>
Chart.defaults.color = '#8a7a60';
Chart.defaults.borderColor = '#3a2d18';

// ── Charts ──────────────────────────────────────────────────────────
try {{
  new Chart(document.getElementById('timelineChart'), {{
    type: 'bar',
    data: {{
      labels: {json.dumps(tl_display_labels)},
      datasets: [{{ label: 'Screenshots', data: {json.dumps(tl_values)}, backgroundColor: '#e8631a', borderRadius: 3 }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ maxRotation: 0, autoSkip: false, font: {{ size: 11 }} }}, grid: {{ display: false }} }},
        y: {{ ticks: {{ font: {{ size: 11 }} }} }}
      }}
    }}
  }});
}} catch(e) {{ console.error('Timeline chart error:', e); }}

const WEALTH = {wealth_chart_json};
let wealthChart = null;
let wealthGainChart = null;
let wealthRangeStart = 0;
let wealthView = WEALTH;
let wealthPeriodUnit = 'month';

function escWealth(value) {{
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function wealthTip(context, kind) {{
  const tip = document.getElementById('wealth-tooltip');
  if (!tip) return;
  const tooltip = context.tooltip;
  if (!tooltip || tooltip.opacity === 0 || !tooltip.dataPoints || !tooltip.dataPoints.length) {{
    tip.style.display = 'none';
    return;
  }}
  const index = wealthRangeStart + tooltip.dataPoints[0].dataIndex;
  const contrib = (wealthView.contrib || [])[index] || {{top: [], more_n: 0, more_v: 0}};
  const top = contrib.top || [];
  const pendingMode = kind === 'line' && tooltip.dataPoints[0].datasetIndex === 1;
  const pendingEntries = ((wealthView.pend || [])[index] || []);
  const pendingValue = pendingEntries.reduce((sum, entry) => sum + (entry.v || 0), 0);
  let html = '<div class="wt-head"><span>' + escWealth(tooltip.dataPoints[0].label) + '</span>'
    + '<span>' + (pendingMode ? fmtWealth(pendingValue) + ' pending' : '+' + fmtWealth((wealthView.monthly || [])[index] || 0)) + '</span></div>';
  if (pendingMode) {{
    html += '<div class="wt-line">Estimated incomplete-assembly value · current prices</div>';
    const pendingShots = pendingEntries.flatMap(entry => entry.shots || []);
    if (pendingShots.length) {{
      html += '<img src="' + escWealth(encodeURI(pendingShots[0].s)) + '" alt="" loading="lazy">';
    }}
    pendingEntries.forEach(entry => {{
      const attested = (entry.a || []).length
        ? ' · ' + escWealth(entry.a.join(', ')) + ' attested, no screenshot'
        : '';
      html += '<div class="wt-row wt-pend"><span>' + escWealth(entry.l) + ' (' + entry.h + '/' + entry.n + ')' + attested + '</span>'
        + '<span>' + fmtWealth(entry.v) + '</span></div>';
    }});
    if (pendingShots.length) {{
      html += '<div class="wt-open">Click the dashed point to browse component screenshots</div>';
    }} else {{
      html += '<div class="wt-more">No screenshot evidence for this pending state.</div>';
    }}
  }} else if (kind === 'line') {{
    html += '<div class="wt-line">Realized ' + fmtWealth((wealthView.cumulative || [])[index] || 0) + '</div>';
  }}
  if (!pendingMode && top.length && top[0].s) {{
    html += '<img src="' + escWealth(encodeURI(top[0].s)) + '" alt="" loading="lazy">';
  }}
  if (!pendingMode) {{
    top.forEach(entry => {{
      html += '<div class="wt-row"><span>' + escWealth(entry.l) + '</span><span>' + fmtWealth(entry.v) + '</span></div>';
    }});
    if (contrib.more_n) {{
      html += '<div class="wt-more">+' + contrib.more_n + ' more · ' + fmtWealth(contrib.more_v) + '</div>';
    }}
    if (!top.length) {{
      html += '<div class="wt-more">No logged gains this ' + wealthPeriodUnit + '.</div>';
    }} else if (top.some(entry => entry.s)) {{
      html += '<div class="wt-open">Click the point to browse screenshots</div>';
    }}
  }}
  tip.innerHTML = html;
  tip.style.display = 'block';
  const card = tip.parentElement.getBoundingClientRect();
  const canvasRect = context.chart.canvas.getBoundingClientRect();
  let x = canvasRect.left - card.left + tooltip.caretX + 14;
  if (x + tip.offsetWidth > card.width - 8) {{
    x = canvasRect.left - card.left + tooltip.caretX - tip.offsetWidth - 14;
  }}
  let y = canvasRect.top - card.top + tooltip.caretY - 24;
  y = Math.max(4, Math.min(y, card.height - tip.offsetHeight - 4));
  tip.style.left = Math.max(4, x) + 'px';
  tip.style.top = y + 'px';
}}

function openWealthMonth(rangeIndex, pendingMode) {{
  const fullIndex = wealthRangeStart + rangeIndex;
  if (pendingMode) {{
    const items = ((wealthView.pend || [])[fullIndex] || []).flatMap(entry =>
      (entry.shots || []).map(shot => ({{src: shot.s, label: shot.l, ts: shot.ts || ''}}))
    );
    if (!items.length) return;
    lbItems = items;
    lbIndex = 0;
    showLb();
    document.getElementById('lightbox').classList.add('open');
    return;
  }}
  const contrib = (wealthView.contrib || [])[fullIndex];
  if (!contrib) return;
  const contributors = contrib.items || contrib.top || [];
  const items = [];
  contributors.forEach(entry => {{
    const shots = (entry.shots && entry.shots.length)
      ? entry.shots
      : (entry.s ? [{{s: entry.s, l: entry.l, ts: entry.ts}}] : []);
    shots.forEach(shot => items.push({{
      src: shot.s,
      label: shot.l || entry.l,
      ts: shot.ts || entry.ts || ''
    }}));
  }});
  if (!items.length) return;
  lbItems = items;
  lbIndex = 0;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

function wealthClick(_event, elements) {{
  if (elements && elements.length) {{
    openWealthMonth(elements[0].index, elements[0].datasetIndex === 1);
  }}
}}

function fmtWealth(value) {{
  const absolute = Math.abs(value || 0);
  const sign = value < 0 ? '-' : '';
  if (absolute >= 1e9) return sign + (absolute / 1e9).toFixed(2).replace(/\.00$/, '') + 'B';
  if (absolute >= 1e6) return sign + (absolute / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (absolute >= 1e3) return sign + Math.round(absolute / 1e3) + 'K';
  return sign + Math.round(absolute).toLocaleString();
}}

function setWealthRange(range, button) {{
  document.querySelectorAll('.wealth-control').forEach(control => control.classList.remove('active'));
  button.classList.add('active');
  renderWealthProgression(range);
}}

function renderWealthProgression(range) {{
  const dailyMode = range === 1 && WEALTH.daily;
  wealthView = dailyMode ? WEALTH.daily : WEALTH;
  wealthPeriodUnit = dailyMode ? 'day' : 'month';
  const labels = wealthView.labels || [];
  const keys = wealthView.keys || [];
  const cumulative = wealthView.cumulative || [];
  const monthly = wealthView.monthly || [];
  const summary = document.getElementById('wealth-period-summary');
  const note = document.getElementById('wealth-scale-note');
  if (!labels.length || !cumulative.length) {{
    if (summary) summary.textContent = 'No valuable-drop history yet.';
    if (note) note.textContent = '';
    return;
  }}
  let start = 0;
  let rangeName = 'all time';
  if (dailyMode) {{
    rangeName = '1 month';
  }} else if (range === 'ytd') {{
    const latestYear = keys.length ? keys[keys.length - 1].slice(0, 4) : '';
    const yearStart = keys.findIndex(key => key.startsWith(latestYear + '-'));
    start = yearStart >= 0 ? yearStart : 0;
    rangeName = 'YTD';
  }} else if (range) {{
    start = Math.max(0, labels.length - range);
    rangeName = range === 1 ? '1 month' : range === 6 ? '6 months' : (range / 12) + ' years';
  }}
  const rangeLabels = labels.slice(start);
  const rangeCumulative = cumulative.slice(start);
  const rangeMonthly = monthly.slice(start);
  const before = start ? cumulative[start - 1] : Number(wealthView.baseline || 0);
  const periodGain = rangeCumulative[rangeCumulative.length - 1] - before;
  if (summary) summary.textContent = '+' + fmtWealth(periodGain) + ' logged over the selected ' + rangeName + '.';

  let suggestedMin = 0;
  if (range && rangeCumulative.length) {{
    const low = Math.min.apply(null, rangeCumulative);
    const high = Math.max.apply(null, rangeCumulative);
    const padding = Math.max(1000000, (high - low) * 0.15);
    const rawMin = Math.max(0, low - padding);
    const unit = rawMin >= 1e9 ? 100000000 : rawMin >= 1e8 ? 25000000 : 5000000;
    suggestedMin = Math.floor(rawMin / unit) * unit;
    if (note) note.textContent = 'Scale begins at ' + fmtWealth(suggestedMin) + ' to make the selected movement readable.';
  }} else if (note) {{
    note.textContent = 'All-time view begins at zero and keeps every calendar month in proportion.';
  }}

  wealthRangeStart = start;
  if (wealthChart) wealthChart.destroy();
  if (wealthGainChart) wealthGainChart.destroy();
  const tip = document.getElementById('wealth-tooltip');
  if (tip) tip.style.display = 'none';
  try {{
    const realizedPointFills = rangeLabels.map((_label, offset) => {{
      const entry = (wealthView.contrib || [])[start + offset] || {{}};
      const contributors = entry.items || entry.top || [];
      return contributors.some(item => (item.shots || []).length || item.s)
        ? '#e6bd56' : 'rgba(0,0,0,0)';
    }});
    const pendingPointFills = rangeLabels.map((_label, offset) => {{
      const entries = ((wealthView.pend || [])[start + offset] || []);
      return entries.some(entry => (entry.shots || []).length)
        ? '#e6bd56' : 'rgba(0,0,0,0)';
    }});
    const lineDatasets = [{{
      label: 'Realized GP', data: rangeCumulative,
      borderColor: '#c99d31', backgroundColor: 'rgba(201,157,49,0.12)',
      borderWidth: 2.5, fill: true, pointRadius: 3,
      pointHoverRadius: 5, pointHitRadius: 4, pointBackgroundColor: realizedPointFills,
      pointBorderColor: '#c99d31', pointBorderWidth: 1.5,
      cubicInterpolationMode: 'monotone'
    }}];
    if (wealthView.potential) {{
      lineDatasets.push({{
        label: 'With pending assemblies (current prices)',
        data: wealthView.potential.slice(start),
        borderColor: '#e6bd56', borderDash: [6, 4], borderWidth: 1.8,
        fill: false, pointRadius: 3, pointHoverRadius: 5, pointHitRadius: 4,
        pointBackgroundColor: pendingPointFills,
        pointBorderColor: '#e6bd56', pointBorderWidth: 1.5,
        cubicInterpolationMode: 'monotone'
      }});
    }}
    wealthChart = new Chart(document.getElementById('gpChart'), {{
      type: 'line',
      data: {{ labels: rangeLabels, datasets: lineDatasets }},
      options: {{ responsive: true, maintainAspectRatio: false,
        interaction: {{ mode: 'point', intersect: true }},
        onClick: wealthClick,
        plugins: {{
          legend: {{ display: !!wealthView.potential, labels: {{ color: '#c8bfae', boxWidth: 16, font: {{ size: 10 }} }} }},
          tooltip: {{ enabled: false, external: context => wealthTip(context, 'line') }}
        }},
        scales: {{
          x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 11 }}, maxTicksLimit: 9, maxRotation: 0 }} }},
          y: {{ suggestedMin: suggestedMin, beginAtZero: !range, ticks: {{ font: {{ size: 11 }}, callback: value => fmtWealth(value) }} }}
        }}
      }}
    }});
    wealthGainChart = new Chart(document.getElementById('gpGainChart'), {{
      type: 'bar',
      data: {{ labels: rangeLabels, datasets: [{{
        label: 'GP logged that ' + wealthPeriodUnit, data: rangeMonthly,
        backgroundColor: '#7f5b1e', borderColor: '#c99d31', borderWidth: 1
      }}] }},
      options: {{ responsive: true, maintainAspectRatio: false,
        interaction: {{ mode: 'nearest', intersect: true }},
        onClick: wealthClick,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{ enabled: false, external: context => wealthTip(context, 'bar') }}
        }},
        scales: {{
          x: {{ display: false, grid: {{ display: false }} }},
          y: {{ grid: {{ color: 'rgba(200,164,90,0.12)' }}, ticks: {{ font: {{ size: 10 }}, maxTicksLimit: 3, callback: value => fmtWealth(value) }} }}
        }}
      }}
    }});
  }} catch(e) {{ console.error('Wealth chart error:', e); }}
}}

renderWealthProgression('ytd');

(function() {{
  const NINETIES = {json.dumps(nineties_json)};
  const el = document.getElementById('nineties-timeline');
  if (!el) return;
  if (NINETIES.length === 0) {{
    el.innerHTML = '<p class="nt-empty">No 99 screenshots captured yet. Go get some levels!</p>';
    return;
  }}
  let html = '';
  NINETIES.forEach((n, i) => {{
    html += '<div class="nt-row">'
      + '<div class="nt-dot" style="background:' + n.color + '"></div>'
      + '<span class="nt-skill">' + n.skill + '</span>'
      + '<span class="nt-date">' + n.ts_str + '</span>'
      + '</div>';
  }});
  // Summary line at the bottom
  html += '<div class="nt-row" style="margin-top:8px;border-top:1px solid var(--border);padding-top:10px">'
    + '<div class="nt-dot" style="background:var(--gold)"></div>'
    + '<span class="nt-skill" style="color:var(--gold)">' + NINETIES.length + ' / 24 skills</span>'
    + '<span class="nt-date">' + (NINETIES.length === 24 ? '🎉 Maxed!' : (24 - NINETIES.length) + ' to go') + '</span>'
    + '</div>';
  el.innerHTML = html;
}})();

// ── Gallery ──────────────────────────────────────────────────────────
const GALLERY = {json.dumps(gallery_json)};
const HOME_MOMENTS = {home_moments_json};
let activeFilter = 'All';
let activeItems = [];
let lbItems = [];  // what the lightbox is currently browsing
let lbIndex = 0;
const GALLERY_BATCH_SIZE = 72;
let galleryVisibleCount = GALLERY_BATCH_SIZE;
let appInteractive = false;
const PRIMARY_FOLDER = {primary_folder_json};
let favoritePaths = new Set();
let appDiagnostics = {{}};
let feedbackMode = null;
const NEW_ISSUE_URL = '{NEW_ISSUE_URL}';

function openHomeMoment(idx) {{
  lbItems = HOME_MOMENTS;
  lbIndex = idx;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}
const dashboardClientId = (window.crypto && crypto.randomUUID)
  ? crypto.randomUUID()
  : 'dashboard-' + Date.now() + '-' + Math.random().toString(16).slice(2);

function setAppStatus(message) {{
  const status = document.getElementById('app-status');
  if (status) status.textContent = message;
}}

function updateFavoriteCountLabels() {{
  const filter = document.getElementById('favorites-filter');
  if (filter) {{
    filter.textContent = 'Favorites' + (favoritePaths.size ? ' (' + favoritePaths.size + ')' : '');
  }}
}}

async function maintainPresence() {{
  // Hold an open connection to the local service for the life of this tab.
  // The server treats the dropped connection as this tab closing — no unload
  // events or beacons involved, so nothing the browser throttles or drops
  // can strand the launcher window.
  let retryDelay = 2000;
  while (true) {{
    try {{
      const response = await fetch(
        '/api/presence?client_id=' + encodeURIComponent(dashboardClientId),
        {{cache: 'no-store'}}
      );
      if (!response.ok || !response.body) throw new Error('presence unavailable');
      retryDelay = 2000;
      const reader = response.body.getReader();
      while (!(await reader.read()).done) {{ /* server pings ~1/s; keep the line open */ }}
    }} catch (_error) {{ /* service unreachable; retry with backoff */ }}
    await new Promise(resolve => setTimeout(resolve, retryDelay));
    retryDelay = Math.min(retryDelay * 2, 15000);
  }}
}}

if (window.location.protocol.indexOf('http') === 0) maintainPresence();

async function initializeDashboardApp() {{
  try {{
    const statusResponse = await fetch('/api/status', {{cache: 'no-store'}});
    if (!statusResponse.ok) throw new Error('Interactive service unavailable');
    const status = await statusResponse.json();
    appInteractive = !!status.interactive;
    if (appInteractive) {{
      document.getElementById('app-controls').classList.add('ready');
      document.getElementById('favorites-filter').style.display = '';
      appDiagnostics = status.diagnostics || {{}};
      document.getElementById('rail-feedback').classList.add('ready');
      const favoritesResponse = await fetch('/api/favorites', {{cache: 'no-store'}});
      if (favoritesResponse.ok) {{
        const payload = await favoritesResponse.json();
        favoritePaths = new Set(payload.favorites || []);
      }}
      updateFavoriteCountLabels();
      setAppStatus('');
    }}
  }} catch (_error) {{
    appInteractive = false;
  }}
  filterGallery();
  renderFavoriteShowcase();
}}

// ── Feedback bar ────────────────────────────────────────────────────
// Report Issue and Request Feature hand off to GitHub with the form already
// filled in. Deliberately no API call and no token: the user signs in and
// presses Submit themselves, so nothing is ever posted on their behalf.

function diagnosticsLines() {{
  const d = appDiagnostics || {{}};
  const shots = (typeof d.screenshots === 'number') ? d.screenshots.toLocaleString() : 'unknown';
  let hiscores = 'unknown';
  if (d.hiscores_ok === true) hiscores = 'reachable';
  else if (d.hiscores_ok === false) hiscores = 'unreachable at last build';
  return [
    'Dashboard version: ' + (d.version || 'unknown'),
    'Running as: ' + (d.packaged ? 'packaged executable' : 'source (Python ' + (d.python || '?') + ')'),
    'Operating system: ' + (d.os || 'unknown'),
    'Screenshots scanned: ' + shots,
    'Hiscores: ' + hiscores,
    'Dashboard last built: ' + (d.built_at || 'unknown')
  ];
}}

function openFeedbackDialog(mode) {{
  // At narrow widths the rail is a drawer over the page; the button that was
  // just tapped lives inside it, so dismiss it before the dialog opens.
  closeNavigation();
  feedbackMode = mode;
  const isBug = mode === 'bug';
  document.getElementById('fb-title').textContent = isBug ? 'Report an issue' : 'Request a feature';
  document.getElementById('fb-lede').textContent = isBug
    ? 'Describe what happened and what you expected instead. Details about your setup are attached for you.'
    : 'Describe what you would like the dashboard to do, and what it would help you see or track.';
  document.getElementById('fb-detail-label').textContent = isBug
    ? 'What happened'
    : 'What you would like';
  document.getElementById('fb-subject').value = '';
  document.getElementById('fb-detail').value = '';
  document.getElementById('fb-diag').textContent = diagnosticsLines().join('\\n');
  document.getElementById('fb-form').style.display = '';
  document.getElementById('fb-releases').style.display = 'none';
  const submit = document.getElementById('fb-submit');
  submit.style.display = '';
  submit.textContent = 'Continue to GitHub';
  document.getElementById('fb-modal').classList.add('open');
  document.getElementById('fb-subject').focus();
}}

function openReportDialog() {{ openFeedbackDialog('bug'); }}
function openFeatureDialog() {{ openFeedbackDialog('feature'); }}

function closeFeedbackModal() {{
  document.getElementById('fb-modal').classList.remove('open');
  feedbackMode = null;
}}

function dismissFeedbackModal(event) {{
  // Only a click on the backdrop itself closes; clicks inside the panel bubble
  // up here too and must not discard what the user has typed.
  if (event.target && event.target.id === 'fb-modal') closeFeedbackModal();
}}

function submitFeedback() {{
  if (!feedbackMode) return;
  const isBug = feedbackMode === 'bug';
  const subject = document.getElementById('fb-subject').value.trim();
  const detail = document.getElementById('fb-detail').value.trim();
  if (!subject) {{
    document.getElementById('fb-subject').focus();
    return;
  }}
  const heading = isBug ? 'What happened' : 'What I would like';
  const body = [
    '### ' + heading,
    '',
    detail || '(not described)',
    '',
    '### Setup',
    '',
    diagnosticsLines().map(line => '- ' + line).join('\\n'),
    '',
    '_Filed from the dashboard._'
  ].join('\\n');
  const params = new URLSearchParams({{
    title: (isBug ? '[Bug] ' : '[Feature] ') + subject,
    body: body
  }});
  window.open(NEW_ISSUE_URL + '?' + params.toString(), '_blank', 'noopener');
  closeFeedbackModal();
}}

async function openSettings() {{
  closeNavigation();
  document.getElementById('fb-title').textContent = 'Settings';
  document.getElementById('fb-lede').textContent = 'Which character this dashboard is built from, and how its data is kept up to date.';
  document.getElementById('fb-form').style.display = 'none';
  document.getElementById('fb-submit').style.display = 'none';
  const holder = document.getElementById('fb-releases');
  holder.style.display = '';
  holder.textContent = 'Loading...';
  document.getElementById('fb-modal').classList.add('open');
  let data = null;
  try {{
    const response = await fetch('/api/settings', {{cache: 'no-store'}});
    if (response.ok) data = await response.json();
  }} catch (_error) {{ /* handled below */ }}
  renderSettings(holder, data);
}}

function settingsRow(holder, label) {{
  const wrap = document.createElement('div');
  wrap.className = 'fb-release';
  const head = document.createElement('div');
  head.className = 'fb-release-head';
  const tag = document.createElement('span');
  tag.className = 'fb-release-tag';
  tag.textContent = label;
  head.appendChild(tag);
  wrap.appendChild(head);
  holder.appendChild(wrap);
  return wrap;
}}

function renderSettings(holder, data) {{
  holder.textContent = '';
  if (!data || !data.ok) {{
    const note = document.createElement('p');
    note.className = 'fb-note';
    note.textContent = 'Settings need the local service, which is not running. Open the dashboard through the app rather than opening the file directly.';
    holder.appendChild(note);
    return;
  }}

  const account = settingsRow(holder, 'Character');
  const current = document.createElement('p');
  current.className = 'fb-release-notes';
  current.textContent = 'Currently showing ' + data.current + '.';
  account.appendChild(current);

  const others = (data.options || []).filter(name => name !== data.current);
  if (others.length) {{
    const picker = document.createElement('div');
    picker.className = 'fb-actions';
    picker.style.justifyContent = 'flex-start';
    picker.style.flexWrap = 'wrap';
    others.forEach(name => {{
      const button = document.createElement('button');
      button.className = 'feedback-btn';
      button.textContent = 'Switch to ' + name;
      button.onclick = () => switchCharacter(name, account, data.current);
      picker.appendChild(button);
    }});
    account.appendChild(picker);
  }} else {{
    const only = document.createElement('p');
    only.className = 'fb-note';
    only.textContent = 'No other characters with screenshots were found.';
    account.appendChild(only);
  }}

  const boss = settingsRow(holder, 'Boss data');
  const bossNote = document.createElement('p');
  bossNote.className = 'fb-release-notes';
  bossNote.textContent = 'Bosses this app has not seen are looked up automatically as you play. A full refresh re-reads the wiki for every boss you have kills on, takes a few minutes, and is worth doing after a game update changes drops you care about.';
  boss.appendChild(bossNote);
  const bossActions = document.createElement('div');
  bossActions.className = 'fb-actions';
  bossActions.style.justifyContent = 'flex-start';
  const bossButton = document.createElement('button');
  bossButton.className = 'feedback-btn';
  bossButton.textContent = 'Refresh boss data now';
  bossButton.onclick = () => refreshBossData(bossButton);
  bossActions.appendChild(bossButton);
  boss.appendChild(bossActions);

  const files = settingsRow(holder, 'Files');
  const filesNote = document.createElement('p');
  filesNote.className = 'fb-release-notes';
  filesNote.textContent = 'Log: ' + (data.log || 'unavailable') +
    String.fromCharCode(10) + 'Settings: ' + (data.settings_file || 'unavailable');
  files.appendChild(filesNote);
}}

function switchNote(container) {{
  // One note per panel, reused. Appending a fresh one per click stacked them
  // up and left contradictory messages on screen at the same time.
  let note = container.querySelector('.switch-note');
  if (!note) {{
    note = document.createElement('p');
    note.className = 'fb-note switch-note';
    container.appendChild(note);
  }}
  note.textContent = '';
  return note;
}}

async function switchCharacter(name, container, previous) {{
  const note = switchNote(container);
  note.textContent = 'Saving...';
  try {{
    const response = await fetch('/api/settings/character', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{character: name}})
    }});
    let result = null;
    try {{
      result = await response.json();
    }} catch (_parse) {{
      // A non-JSON body means the service answered with something unexpected.
      // Say what actually happened rather than blaming the connection.
      note.textContent = 'The local service returned an unexpected response (HTTP ' +
        response.status + '). Check your log file for details.';
      return;
    }}
    if (!result.ok) {{
      note.textContent = result.message || 'That did not work.';
      return;
    }}
    if (!result.restart_required) {{
      note.textContent = 'Already showing ' + name + '.';
      return;
    }}
    note.textContent = name + ' will be used next time. Close this window and open the dashboard again to switch. ';
    // Switching writes immediately, so a misclick needs a way back. Offering
    // an undo afterwards keeps the common case one click, where a confirm
    // step would tax everyone to protect the occasional slip.
    if (previous && previous !== name) {{
      const undo = document.createElement('button');
      undo.className = 'feedback-btn';
      undo.style.marginLeft = '4px';
      undo.textContent = 'Keep ' + previous + ' instead';
      undo.onclick = async () => {{
        undo.disabled = true;
        try {{
          const back = await fetch('/api/settings/character', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{character: previous}})
          }});
          const undone = await back.json();
          note.textContent = undone.ok
            ? 'Staying on ' + previous + '. Nothing will change.'
            : (undone.message || 'Could not undo that.');
        }} catch (_undoError) {{
          note.textContent = 'Could not reach the local service to undo that.';
        }}
      }};
      note.appendChild(undo);
    }}
  }} catch (_error) {{
    note.textContent = 'Could not reach the local service. It may have stopped; check your log file.';
  }}
}}

async function refreshBossData(button) {{
  if (!appInteractive) return;
  button.disabled = true;
  button.textContent = 'Refreshing, this takes a few minutes...';
  setAppStatus('Re-reading boss drop tables from the OSRS Wiki');
  try {{
    const response = await fetch('/api/refresh', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{boss_data: true}})
    }});
    const result = await response.json();
    if (result.ok) {{
      button.textContent = 'Done. Reloading...';
      window.location.reload();
    }} else {{
      button.disabled = false;
      button.textContent = 'Refresh boss data now';
      setAppStatus(result.message || 'Boss data refresh failed.');
    }}
  }} catch (_error) {{
    button.disabled = false;
    button.textContent = 'Refresh boss data now';
    setAppStatus('Could not reach the local service.');
  }}
}}

async function openWhatsNew() {{
  closeNavigation();
  document.getElementById('fb-title').textContent = 'What' + String.fromCharCode(39) + 's new';
  document.getElementById('fb-lede').textContent = 'Recent releases and what changed in each.';
  document.getElementById('fb-form').style.display = 'none';
  document.getElementById('fb-submit').style.display = 'none';
  const holder = document.getElementById('fb-releases');
  holder.style.display = '';
  holder.textContent = 'Loading...';
  document.getElementById('fb-modal').classList.add('open');
  let payload = null;
  try {{
    const response = await fetch('/api/releases', {{cache: 'no-store'}});
    if (response.ok) payload = await response.json();
  }} catch (_error) {{ /* offline is an ordinary outcome, not an error state */ }}
  renderReleases(holder, payload);
}}

function renderReleases(holder, payload) {{
  holder.textContent = '';
  const releases = (payload && payload.releases) || [];
  if (!releases.length) {{
    const note = document.createElement('p');
    note.className = 'fb-note';
    note.textContent = 'Release notes are not available right now. They need a connection the first time, and are kept on disk after that.';
    holder.appendChild(note);
    return;
  }}
  const current = (payload && payload.current) || '';
  releases.forEach(release => {{
    const wrap = document.createElement('div');
    wrap.className = 'fb-release';
    const head = document.createElement('div');
    head.className = 'fb-release-head';
    const tag = document.createElement('span');
    tag.className = 'fb-release-tag';
    tag.textContent = release.name || release.tag || 'Release';
    head.appendChild(tag);
    if (release.published_at) {{
      const date = document.createElement('span');
      date.className = 'fb-release-date';
      date.textContent = release.published_at;
      head.appendChild(date);
    }}
    if (current && release.tag && release.tag.replace(/^v/i, '') === current) {{
      const badge = document.createElement('span');
      badge.className = 'fb-release-current';
      badge.textContent = 'You have this';
      head.appendChild(badge);
    }}
    wrap.appendChild(head);
    if (release.notes) {{
      const notes = document.createElement('p');
      notes.className = 'fb-release-notes';
      notes.textContent = release.notes.trim();
      wrap.appendChild(notes);
    }}
    holder.appendChild(wrap);
  }});
  if (payload && payload.stale) {{
    const note = document.createElement('p');
    note.className = 'fb-note';
    note.textContent = 'Showing the last notes saved to this machine. There may be newer ones.';
    holder.appendChild(note);
  }}
}}

document.addEventListener('keydown', event => {{
  if (event.key === 'Escape' && document.getElementById('fb-modal').classList.contains('open')) {{
    closeFeedbackModal();
  }}
}});

async function refreshDashboard() {{
  if (!appInteractive) return;
  const button = document.getElementById('refresh-btn');
  button.disabled = true;
  button.textContent = 'Refreshing...';
  setAppStatus('Scanning screenshots and updating account data');
  try {{
    const response = await fetch('/api/refresh', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{}})
    }});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || 'Refresh failed');
    setAppStatus(result.message);
    window.location.reload();
  }} catch (error) {{
    button.disabled = false;
    button.textContent = 'Refresh dashboard';
    setAppStatus(error.message || 'Refresh failed');
  }}
}}

function screenshotPath(item) {{
  // The favourite key, not the renderable path. A screenshot inside the
  // primary folder renders as 'Boss Kills/x.png' and one merged in from a
  // former name renders as '../OldName/Boss Kills/x.png', but both must be
  // remembered by the same identity whatever the account is composed of.
  // Deriving it here rather than stamping it onto every item collection means
  // the gallery, the lightbox and every showcase agree by construction.
  const src = (item && (item.path || item.src)) || '';
  if (!src) return '';
  if (src.indexOf('../') === 0) return src.slice(3);
  return PRIMARY_FOLDER + '/' + src;
}}

function makeFavoriteButton(item) {{
  const path = screenshotPath(item);
  const button = document.createElement('button');
  button.className = 'favorite-heart' + (favoritePaths.has(path) ? ' active' : '');
  button.textContent = favoritePaths.has(path) ? '\u2665' : '\u2661';
  button.setAttribute('aria-label', favoritePaths.has(path) ? 'Remove favorite' : 'Favorite screenshot');
  button.addEventListener('click', async event => {{
    event.stopPropagation();
    await toggleFavorite(path);
  }});
  return button;
}}

async function toggleFavorite(path) {{
  if (!appInteractive || !path) return;
  const shouldFavorite = !favoritePaths.has(path);
  try {{
    const response = await fetch('/api/favorite', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{path: path, favorite: shouldFavorite}})
    }});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || 'Could not save favorite');
    if (shouldFavorite) favoritePaths.add(path); else favoritePaths.delete(path);
    updateFavoriteCountLabels();
    setAppStatus('');
    filterGallery(false);
    renderFavoriteShowcase();
    showLb();
  }} catch (error) {{
    setAppStatus(error.message || 'Could not save favorite');
  }}
}}

async function toggleLightboxFavorite(event) {{
  event.stopPropagation();
  await toggleFavorite(screenshotPath(lbItems[lbIndex]));
}}

function filterGallery(reset = true) {{
  if (reset) galleryVisibleCount = GALLERY_BATCH_SIZE;
  const search = document.getElementById('gallery-search').value.toLowerCase();
  activeItems = GALLERY.filter(item => {{
    const matchCat = activeFilter === 'All'
      || (activeFilter === 'Favorites' && favoritePaths.has(screenshotPath(item)))
      || item.cat === activeFilter;
    const matchSearch = !search || item.label.toLowerCase().includes(search) || item.cat.toLowerCase().includes(search);
    return matchCat && matchSearch;
  }});
  renderGallery();
}}

function setFilter(cat, btn) {{
  activeFilter = cat;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  filterGallery();
}}

function renderGallery() {{
  const grid = document.getElementById('gallery-grid');
  const count = document.getElementById('gallery-count');
  const shownItems = activeItems.slice(0, galleryVisibleCount);
  count.textContent = 'Showing ' + shownItems.length + ' of ' + activeItems.length + ' screenshots';
  grid.innerHTML = '';
  shownItems.forEach((item, idx) => {{
    const div = document.createElement('div');
    div.className = 'gallery-item';
    div.onclick = () => openLightbox(idx);
    div.innerHTML = `
      <img src="${{item.src}}" alt="${{item.label}}" loading="lazy" onerror="this.style.display='none'">
      <div class="thumb-info">
        <div class="thumb-cat">${{item.cat}}</div>
        <div class="thumb-label">${{item.label}}</div>
        <div class="thumb-date">${{item.ts}}</div>
      </div>`;
    if (appInteractive) div.appendChild(makeFavoriteButton(item));
    grid.appendChild(div);
  }});
  document.getElementById('gallery-more-wrap').style.display =
    shownItems.length < activeItems.length ? 'block' : 'none';
}}

function loadMoreGallery() {{
  galleryVisibleCount += GALLERY_BATCH_SIZE;
  renderGallery();
}}

function renderFavoriteShowcase() {{
  const section = document.getElementById('favorite-showcase');
  const grid = document.getElementById('favorite-showcase-grid');
  const count = document.getElementById('favorite-showcase-count');
  if (!section || !grid || !appInteractive) return;
  const favorites = GALLERY
    .filter(item => favoritePaths.has(screenshotPath(item)))
    .sort((a, b) => (b.sort || '').localeCompare(a.sort || ''));
  section.style.display = favorites.length ? '' : 'none';
  count.textContent = favorites.length + ' saved';
  grid.innerHTML = '';
  favorites.slice(0, 8).forEach((item, idx) => {{
    const div = document.createElement('div');
    div.className = 'gallery-item';
    div.onclick = () => {{
      lbItems = favorites;
      lbIndex = idx;
      showLb();
      document.getElementById('lightbox').classList.add('open');
    }};
    div.innerHTML = `<img src="${{item.src}}" alt="${{item.label}}" loading="lazy" onerror="this.style.display='none'">
      <div class="thumb-info">
        <div class="thumb-cat">${{item.cat}}</div>
        <div class="thumb-label">${{item.label}}</div>
        <div class="thumb-date">${{item.ts}}</div>
      </div>`;
    div.appendChild(makeFavoriteButton(item));
    grid.appendChild(div);
  }});
}}

function openLightbox(idx) {{
  lbItems = activeItems;
  lbIndex = idx;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

function showLb() {{
  const item = lbItems[lbIndex];
  if (!item) return;
  document.getElementById('lb-img').src = item.src;
  const path = screenshotPath(item);
  const favoriteButton = document.getElementById('lb-favorite');
  const eligible = appInteractive && path.toLowerCase().endsWith('.png');
  favoriteButton.style.display = eligible ? '' : 'none';
  favoriteButton.classList.toggle('active', eligible && favoritePaths.has(path));
  favoriteButton.textContent = favoritePaths.has(path) ? '\u2665' : '\u2661';
  favoriteButton.setAttribute('aria-label', favoritePaths.has(path) ? 'Remove favorite' : 'Favorite screenshot');
  document.getElementById('lb-info').textContent = item.label + (item.ts ? ' · ' + item.ts : '');
}}

function lbNav(dir) {{
  lbIndex = (lbIndex + dir + lbItems.length) % lbItems.length;
  showLb();
}}

function closeLightbox() {{
  document.getElementById('lightbox').classList.remove('open');
}}

document.addEventListener('keydown', e => {{
  if (!document.getElementById('lightbox').classList.contains('open')) return;
  if (e.key === 'ArrowRight') lbNav(1);
  if (e.key === 'ArrowLeft') lbNav(-1);
  if (e.key === 'Escape') closeLightbox();
}});

document.getElementById('lightbox').addEventListener('click', e => {{
  if (e.target === document.getElementById('lightbox')) closeLightbox();
}});

// Account Pulse — recent movement from the same daily snapshots as Road to Max
const ACCOUNT_PULSE = {account_pulse_json};
let pulseWindow = 7;

function fmtPulseNumber(value) {{
  if (value >= 1e9) {{
    const digits = value >= 10e9 ? 0 : 1;
    const scaled = (value / 1e9).toFixed(digits);
    return (digits ? scaled.replace(/\.?0+$/, '') : scaled) + 'B';
  }}
  if (value >= 1e6) {{
    const digits = value >= 10e6 ? 0 : 2;
    const scaled = (value / 1e6).toFixed(digits);
    return (digits ? scaled.replace(/\.?0+$/, '') : scaled) + 'M';
  }}
  if (value >= 1e3) {{
    const digits = value >= 100e3 ? 0 : 1;
    const scaled = (value / 1e3).toFixed(digits);
    return (digits ? scaled.replace(/\.?0+$/, '') : scaled) + 'K';
  }}
  return value.toLocaleString();
}}

function fmtPulseDate(value) {{
  if (!value) return '';
  const date = new Date(value + 'T00:00:00');
  return date.toLocaleDateString(undefined, {{month: 'short', day: 'numeric'}});
}}

function pulseMetric(value, label) {{
  return '<div class="pulse-metric"><div class="pulse-metric-value">' + value
    + '</div><div class="pulse-metric-label">' + label + '</div></div>';
}}

function renderAccountPulse() {{
  const pulse = ACCOUNT_PULSE[String(pulseWindow)];
  const verdict = document.getElementById('pulse-verdict');
  const meta = document.getElementById('pulse-meta');
  const content = document.getElementById('pulse-content');
  if (!pulse || !pulse.available) {{
    verdict.textContent = 'Account Pulse is building its history.';
    meta.textContent = 'Refresh on at least two different days to unlock recent movement.';
    content.innerHTML = '<p class="pulse-empty">XP, boss KC, collection-log, and clue movement will appear here automatically.</p>';
    return;
  }}

  verdict.textContent = pulse.verdict;
  const coverage = fmtPulseDate(pulse.from_date) + '–' + fmtPulseDate(pulse.to_date);
  const availability = pulse.span_days < pulse.window
    ? pulse.span_days + ' days of history available'
    : pulse.window + '-day window';
  meta.textContent = coverage + ' · ' + availability;

  let html = '<div class="pulse-metrics">';
  html += pulseMetric(fmtPulseNumber(pulse.total_xp), 'XP Gained');
  html += pulseMetric(pulse.boss_kills.toLocaleString(), 'Boss Kills');
  html += pulseMetric(pulse.clog_gain === null ? '—' : '+' + pulse.clog_gain.toLocaleString(), 'Collection Log Slots');
  html += pulseMetric(pulse.clue_gain === null ? '—' : '+' + pulse.clue_gain.toLocaleString(), 'Clues Completed');
  html += '</div><div class="pulse-breakdowns">';

  html += '<div class="pulse-panel"><div class="pulse-panel-title">XP Focus</div>';
  if (pulse.top_skills.length) {{
    pulse.top_skills.forEach(skill => {{
      html += '<div class="pulse-row"><div class="pulse-row-head"><span class="pulse-row-name">'
        + skill.name + '</span><span class="pulse-row-value">' + fmtPulseNumber(skill.gain)
        + ' XP · ' + skill.share + '%</span></div><div class="pulse-bar"><div class="pulse-bar-fill" style="width:'
        + Math.max(skill.share, 2) + '%"></div></div></div>';
    }});
  }} else {{
    html += '<p class="pulse-empty">No XP movement in this window.</p>';
  }}
  html += '</div>';

  html += '<div class="pulse-panel"><div class="pulse-panel-title">Bossing Mix</div>';
  if (pulse.top_bosses.length) {{
    pulse.top_bosses.forEach(boss => {{
      html += '<div class="pulse-row"><div class="pulse-row-head"><span class="pulse-row-name">'
        + boss.name + '</span><span class="pulse-row-value">' + boss.kills.toLocaleString()
        + (boss.kills === 1 ? ' kill' : ' kills') + ' · ' + boss.share + '%</span></div><div class="pulse-bar"><div class="pulse-bar-fill" style="width:'
        + Math.max(boss.share, 2) + '%"></div></div></div>';
    }});
  }} else {{
    html += '<p class="pulse-empty">No boss KC movement in this window.</p>';
  }}
  html += '</div></div>';
  content.innerHTML = html;
}}

function setPulseWindow(days, btn) {{
  pulseWindow = days;
  document.querySelectorAll('.pulse-window-btn').forEach(button => button.classList.remove('active'));
  btn.classList.add('active');
  renderAccountPulse();
}}

renderAccountPulse();

// ── Road to Max ──────────────────────────────────────────────────────
const ROAD_TO_MAX = {json.dumps(road_to_max_json)};
const XP_TREND = {json.dumps(xp_trend)};

// Favorite Bosses widget on the Stats page — lifetime KC from hiscores
const FAV_BOSSES = {fav_bosses_json};
(function() {{
  const container = document.getElementById('fav-bosses');
  if (!container) return;
  if (FAV_BOSSES.length === 0) {{
    container.innerHTML = '<p class="empty-note">Hiscores unreachable — boss KC unavailable this refresh.</p>';
    return;
  }}
  const maxKc = FAV_BOSSES[0].kc || 1;
  let html = '';
  FAV_BOSSES.forEach(b => {{
    html += '<div class="rtm-row">'
      + '<span class="fav-name">' + b.boss + '</span>'
      + '<div class="rtm-bar-bg"><div class="rtm-bar-fill" style="width:' + Math.round(b.kc / maxKc * 100) + '%;background:var(--gold-dim)"></div></div>'
      + '<span class="rtm-remaining">' + b.kc.toLocaleString() + '</span>'
      + '</div>';
  }});
  container.innerHTML = html;
}})();

// Full detail rows on the Road to Max page
(function() {{
  const container = document.getElementById('rtm-detail');
  if (!container) return;
  if (ROAD_TO_MAX.length === 0) {{
    container.innerHTML = '<p style="color:#e8631a;font-weight:700;font-size:1rem;">All skills maxed! 🎉</p>';
    return;
  }}
  let html = '';
  ROAD_TO_MAX.forEach(s => {{
    let badge = '';
    if (s.active) badge = '<span class="rtm-badge rtm-badge-active">⚡ Active</span>';
    const left = s.xp_rem_str ? s.xp_rem_str + ' XP left (' + s.pct + '% there)' : s.remaining + ' levels left';
    const pace = s.rate > 0 ? '+' + s.rate.toLocaleString() + ' XP/day' : '';
    const eta = s.eta_label || 'Not currently training';
    html += '<div class="rtm-detail-row">'
      + '<div class="rtm-detail-top">'
      + '<span class="rtm-skill-lg" style="color:' + s.color + '">' + s.skill + '</span>'
      + badge
      + '<span class="rtm-detail-level">Lv ' + s.level + '</span>'
      + '</div>'
      + '<div class="rtm-bar-bg rtm-bar-lg"><div class="rtm-bar-fill" style="width:' + s.pct + '%;background:' + s.color + '"></div></div>'
      + '<div class="rtm-detail-meta">'
      + '<span>' + left + '</span>'
      + '<span>' + pace + '</span>'
      + '<span>' + eta + '</span>'
      + '</div>'
      + '</div>';
  }});
  container.innerHTML = html;
}})();

// Daily XP gained — velocity beats the near-flat cumulative total on the road
// to max. Bars are XP/day, normalized for irregular refresh spacing; the
// tooltip surfaces the raw gain and span when an interval covers many days.
(function() {{
  const el = document.getElementById('xpTrendChart');
  if (!el) return;
  if (XP_TREND.length < 1) {{
    el.style.display = 'none';
    const note = document.getElementById('xp-trend-note');
    if (note) note.style.display = 'block';
    return;
  }}
  const fmtXp = function(v) {{
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (a >= 1e3) return (v / 1e3).toFixed(0) + 'K';
    return String(Math.round(v));
  }};
  new Chart(el, {{
    type: 'bar',
    data: {{
      labels: XP_TREND.map(p => p.date),
      datasets: [{{
        label: 'XP / day',
        data: XP_TREND.map(p => p.per_day),
        backgroundColor: 'rgba(201,157,49,0.55)',
        borderColor: '#c99d31',
        borderWidth: 1,
        borderRadius: 4,
        maxBarThickness: 48
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: function(ctx) {{
              const p = XP_TREND[ctx.dataIndex];
              const lines = [fmtXp(p.per_day) + ' XP/day'];
              if (p.days > 1) lines.push(fmtXp(p.gained) + ' total over ' + p.days + ' days');
              return lines;
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ font: {{ size: 11 }}, maxTicksLimit: 12 }}, grid: {{ display: false }} }},
        y: {{
          beginAtZero: true,
          ticks: {{
            font: {{ size: 11 }},
            callback: function(val) {{ return fmtXp(val); }}
          }}
        }}
      }}
    }}
  }});
}})();

// ── Screenshot-backed Account Journey ─────────────────────────────────
const JOURNEY_EVENTS = {journey_json};
let journeyRange = '3';
let journeyMode = 'all';
let journeySkill = '';

function journeyMonthLabel(month) {{
  const parts = month.split('-');
  return new Date(Number(parts[0]), Number(parts[1]) - 1, 1).toLocaleDateString('en-US', {{month:'short', year:'2-digit'}});
}}

function journeyMonths() {{
  if (!JOURNEY_EVENTS.length) return [];
  const last = new Date();
  let first = new Date(JOURNEY_EVENTS[0].iso + 'T00:00:00');
  if (journeyRange !== 'all') first = new Date(last.getFullYear() - Number(journeyRange), last.getMonth(), 1);
  first = new Date(first.getFullYear(), first.getMonth(), 1);
  const end = new Date(last.getFullYear(), last.getMonth(), 1);
  const months = [];
  while (first <= end) {{ months.push(first.getFullYear() + '-' + String(first.getMonth() + 1).padStart(2, '0')); first.setMonth(first.getMonth() + 1); }}
  return months;
}}

function openJourneyEvents(events) {{
  if (!events.length) return;
  lbItems = events.map(event => ({{src:event.src,label:event.skill + ' level ' + event.level,ts:event.date}}));
  lbIndex = 0;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

function renderJourneyFocus() {{
  const map = document.getElementById('journey-focus-map');
  if (!map) return;
  if (!JOURNEY_EVENTS.length) {{ map.innerHTML = '<p class="empty-note">No dated level screenshots are available.</p>'; return; }}
  const months = journeyMonths();
  const fullTimelineWidth = 103 + (months.length * 21);
  map.style.width = Math.max(760, map.parentElement.clientWidth, fullTimelineWidth) + 'px';
  const monthSet = new Set(months);
  const inRange = JOURNEY_EVENTS.filter(event => monthSet.has(event.month));
  const skills = [...new Set(inRange.map(event => event.skill))].sort((a, b) => a.localeCompare(b));
  const shownSkills = journeyMode === 'one' && journeySkill ? skills.filter(skill => skill === journeySkill) : skills;
  let html = '<div class="journey-axis" style="--month-count:' + months.length + '"><strong>Skill</strong>' + months.map((month, idx) => '<span>' + (months.length <= 18 || idx % 3 === 0 ? journeyMonthLabel(month) : '') + '</span>').join('') + '</div>';
  shownSkills.forEach(skill => {{
    const color = (inRange.find(event => event.skill === skill) || {{color:'#c8a45a'}}).color;
    html += '<div class="journey-focus-row" style="--month-count:' + months.length + ';--skill-color:' + color + '"><strong>' + skill + '</strong>';
    months.forEach(month => {{
      const events = inRange.filter(event => event.skill === skill && event.month === month);
      html += events.length ? '<button class="journey-cell" data-journey-cell="' + skill + '|' + month + '" title="' + events.length + ' captured level' + (events.length === 1 ? '' : 's') + '"></button>' : '<span class="journey-cell"></span>';
    }});
    html += '</div>';
  }});
  map.innerHTML = html;
  map.querySelectorAll('[data-journey-cell]').forEach(button => button.addEventListener('click', () => {{
    const [skill, month] = button.dataset.journeyCell.split('|');
    openJourneyEvents(inRange.filter(event => event.skill === skill && event.month === month));
  }}));
  document.getElementById('journey-coverage').textContent = inRange.length + ' captured level events across ' + skills.length + ' skills · blank months remain intentionally empty.';
}}

function renderJourneySequence() {{
  const container = document.getElementById('journey-sequence');
  if (!container || !journeySkill) return;
  const events = JOURNEY_EVENTS.filter(event => event.skill === journeySkill).sort((a, b) => a.level - b.level || a.iso.localeCompare(b.iso));
  document.getElementById('journey-skill-title').textContent = journeySkill + ' Journey';
  let html = '';
  events.forEach((event, idx) => {{
    if (idx && event.level - events[idx - 1].level > 1) html += '<div class="journey-gap">' + (event.level - events[idx - 1].level - 1) + ' levels not captured</div>';
    html += '<div class="journey-level"><span>' + event.date + '</span><button data-journey-event="' + event.iso + '|' + event.level + '">' + event.level + '</button></div>';
  }});
  container.innerHTML = html || '<p class="empty-note">No captured levels for this skill.</p>';
  container.querySelectorAll('[data-journey-event]').forEach(button => button.addEventListener('click', () => {{
    const [iso, level] = button.dataset.journeyEvent.split('|');
    openJourneyEvents(events.filter(event => event.iso === iso && String(event.level) === level));
  }}));
}}

(function initJourney() {{
  const select = document.getElementById('journey-skill-select');
  if (!select || !JOURNEY_EVENTS.length) return;
  const counts = {{}};
  JOURNEY_EVENTS.forEach(event => counts[event.skill] = (counts[event.skill] || 0) + 1);
  const skills = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  journeySkill = skills[0];
  select.innerHTML = skills.map(skill => '<option value="' + skill + '">' + skill + '</option>').join('');
  select.value = journeySkill;
  select.addEventListener('change', () => {{ journeySkill = select.value; renderJourneyFocus(); renderJourneySequence(); }});
  document.querySelectorAll('[data-journey-range]').forEach(button => button.addEventListener('click', () => {{ journeyRange = button.dataset.journeyRange; document.querySelectorAll('[data-journey-range]').forEach(item => item.classList.toggle('active', item === button)); renderJourneyFocus(); }}));
  document.querySelectorAll('[data-journey-mode]').forEach(button => button.addEventListener('click', () => {{ journeyMode = button.dataset.journeyMode; document.querySelectorAll('[data-journey-mode]').forEach(item => item.classList.toggle('active', item === button)); renderJourneyFocus(); }}));
  renderJourneyFocus();
  renderJourneySequence();
}})();

// ── Luck engine ───────────────────────────────────────────────────────
const LUCK = {luck_json};
const LUCK_BOSS_IMAGES = {boss_image_json};
let luckSort = 'blessed';
let selectedLuckBoss = LUCK.bosses && LUCK.bosses.length ? LUCK.bosses[0].boss : '';

function sortLuck(mode, btn) {{
  luckSort = mode;
  document.querySelectorAll('[data-luck-sort]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderLuck();
}}

function luckVerdict(b) {{
  if (b.gap) return 'No screenshot evidence';
  if (b.z >= 1) return 'Blessed';
  if (b.z >= 0.4) return 'Warm';
  if (b.expected < 1.5 && b.z < 0) return 'Early days';
  if (b.z <= -1) return 'Cursed';
  if (b.z <= -0.4) return 'Dry';
  return 'On rate';
}}

function luckColor(b) {{
  if (b.gap) return '#c19a7a';
  if (b.pct >= 80) return '#8eae63';
  if (b.pct >= 40) return '#d4a52b';
  if (b.pct < 10) return '#c46a4a';
  return '#b88b3a';
}}

function renderLuckDetail(b) {{
  const detail = document.getElementById('luck-detail');
  if (!detail || !b) return;
  const color = luckColor(b);
  const initials = b.boss.split(/\s+/).map(word => word[0]).join('').slice(0, 2);
  const bossImage = LUCK_BOSS_IMAGES[b.boss] || '';
  const bossVisual = bossImage ? '<button class="luck-detail-image" data-luck-boss-image="true" aria-label="Open ' + b.boss + ' screenshot"><img src="' + bossImage + '" alt=""></button>' : '<div class="luck-detail-visual">' + initials + '</div>';
  const owned = b.items.filter(it => it.owned).length;
  const missing = b.items.length - owned;
  let items = '';
  b.items.forEach((it, itemIndex) => {{
    const copies = it.copies > 1 ? ' ×' + it.copies : '';
    const linkedEvidence = it.owned && Array.isArray(it.evidence) && it.evidence.length > 0;
    let evidence;
    if (it.owned) {{
      evidence = it.copies > 1 && it.tail_pct !== null
        ? (it.tail_pct <= 50 ? 'Only ' + it.tail_pct + '% would have this many' : 'About on rate')
        : it.p_have + '% have it by now';
    }} else {{
      evidence = it.exp_n >= 1 ? 'Missing · owed ' + it.exp_n : 'Missing · E ' + it.exp_n;
    }}
    items += (linkedEvidence ? '<button type="button" data-luck-item-index="' + itemIndex + '" aria-label="Open ' + it.item + ' drop screenshots"' : '<article')
      + ' class="luck-item ' + (it.owned ? 'owned' : 'missing') + (linkedEvidence ? ' evidence-linked' : '') + '">'
      + '<strong>' + (it.owned ? '✓ ' : '') + it.item + copies + '</strong>'
      + '<span>' + it.rate + '</span><small>' + evidence + (linkedEvidence ? ' · View screenshot' : (it.owned ? ' · No screenshot captured' : '')) + '</small>'
      + (linkedEvidence ? '</button>' : '</article>');
  }});
  const context = b.gap
    ? b.kc.toLocaleString() + ' tracked kills · excluded from the account headline'
    : b.kc.toLocaleString() + ' tracked kills · ' + b.pct + '% luck percentile' + (b.gp_str ? ' · ' + b.gp_str + ' logged' : '');
  detail.style.setProperty('--score-color', color);
  detail.innerHTML = '<div class="luck-detail-hero">' + bossVisual
    + '<div class="luck-detail-copy"><h2>' + b.boss + '</h2><p>' + context + '</p>'
    + '<span class="luck-verdict">' + luckVerdict(b) + '</span></div></div>'
    + '<div class="luck-equation"><div><strong>' + b.actual + '</strong><span>Owned uniques</span></div>'
    + '<i>versus</i><div><strong>' + b.expected.toFixed(1) + '</strong><span>Expected uniques</span></div></div>'
    + '<section class="luck-detail-section"><header><strong>Drop evidence</strong><span>' + owned + ' owned · ' + missing + ' missing</span></header>'
    + '<div class="luck-item-grid">' + items + '</div></section>'
    + (b.gap ? '<p class="luck-gap-note">This boss has meaningful KC but no trustworthy screenshot ownership coverage. It stays visible for context and is excluded from the account headline rather than counted as unlucky.</p>' : '');
  const bossImageButton = detail.querySelector('[data-luck-boss-image]');
  if (bossImageButton) bossImageButton.addEventListener('click', () => openLuckBossImage(b.boss));
  detail.querySelectorAll('[data-luck-item-index]').forEach(button => {{
    button.addEventListener('click', () => openLuckItemEvidence(b, Number(button.dataset.luckItemIndex)));
  }});
}}

function openLuckItemEvidence(boss, itemIndex) {{
  const item = boss && boss.items ? boss.items[itemIndex] : null;
  if (!item || !Array.isArray(item.evidence) || !item.evidence.length) return;
  lbItems = item.evidence;
  lbIndex = 0;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

function openLuckBossImage(boss) {{
  const src = LUCK_BOSS_IMAGES[boss];
  if (!src) return;
  lbItems = [{{src:src,label:boss,ts:'Representative screenshot evidence'}}];
  lbIndex = 0;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

function renderLuck() {{
  const list = document.getElementById('luck-list');
  const detail = document.getElementById('luck-detail');
  if (!list || !detail) return;
  if (!LUCK.bosses || LUCK.bosses.length === 0) {{
    list.innerHTML = '<p class="empty-note">No measurable bosses yet — luck needs live hiscores KC and a drop-table scrape (run "2 - Update Drop Tables").</p>';
    detail.innerHTML = '<p class="empty-note">Boss evidence will appear here once luck can be measured.</p>';
    return;
  }}
  const rows = LUCK.bosses.slice();
  if (luckSort === 'blessed')      rows.sort((a, b) => (a.gap - b.gap) || (b.z - a.z));
  else if (luckSort === 'cursed')  rows.sort((a, b) => (a.gap - b.gap) || (a.z - b.z));
  else if (luckSort === 'gp')      rows.sort((a, b) => (a.gap - b.gap) || (b.gp - a.gp));
  else if (luckSort === 'kc')      rows.sort((a, b) => (a.gap - b.gap) || (b.kc - a.kc));
  if (!rows.some(b => b.boss === selectedLuckBoss)) selectedLuckBoss = rows[0].boss;
  let html = '';
  rows.forEach(b => {{
    const color = luckColor(b);
    html += '<button class="luck-row ' + (b.boss === selectedLuckBoss ? 'active' : '') + '" data-luck-boss="' + b.boss + '"'
      + ' style="--score:' + (b.gap ? 0 : b.pct) + '%;--score-color:' + color + '" aria-pressed="' + (b.boss === selectedLuckBoss) + '">'
      + '<span class="luck-score">' + (b.gap ? '?' : b.pct) + '</span>'
      + '<span class="luck-row-copy"><strong>' + b.boss + '</strong><span>' + luckVerdict(b) + ' · ' + b.kc.toLocaleString() + ' KC</span>'
      + '<span class="luck-row-meter"><i></i></span>' + (b.gap ? '<em class="luck-gap">Excluded from headline</em>' : '') + '</span>'
      + '<span class="luck-row-numbers"><strong>' + b.actual + ' / ' + b.expected.toFixed(1) + '</strong><span>owned / expected</span>'
      + '<span>' + (b.gp_str ? b.gp_str + ' logged' : 'No GP shown') + '</span></span></button>';
  }});
  list.innerHTML = html;
  renderLuckDetail(LUCK.bosses.find(b => b.boss === selectedLuckBoss) || rows[0]);
}}
document.getElementById('luck-list')?.addEventListener('click', event => {{
  const row = event.target.closest('[data-luck-boss]');
  if (!row) return;
  selectedLuckBoss = row.dataset.luckBoss;
  renderLuck();
}});
renderLuck();

// ── Loot Log ──────────────────────────────────────────────────────────
const DROPS = {json.dumps(drops_json)};
let lootSortKey = 'value';

function fmtGP(v) {{
  if (v >= 1000000000) return (v / 1000000000).toFixed(2) + 'B';
  if (v >= 1000000) return (v / 1000000).toFixed(1) + 'M';
  if (v >= 1000) return (v / 1000).toFixed(0) + 'K';
  return v.toLocaleString();
}}

function sortLoot(key, btn) {{
  lootSortKey = key;
  document.querySelectorAll('.loot-sort-controls .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderLoot();
}}

function renderLoot() {{
  const grid = document.getElementById('loot-grid');
  if (!grid) return;
  const sorted = [...DROPS].sort((a, b) => lootSortKey === 'value' ? b.value - a.value : (b.ts_iso > a.ts_iso ? 1 : -1));
  grid.innerHTML = sorted.map((drop, idx) => {{
    const qty = drop.qty > 1 ? drop.qty + 'x ' : '';
    return '<article class="loot-row"><div class="loot-row-main"><button type="button" class="loot-thumb-btn" aria-label="Open drop screenshot" onclick="openLootEvidence(' + idx + ')"><img src="' + drop.src + '" alt="" loading="lazy"></button><div><strong>' + qty + drop.item + '</strong><span>Valuable drop screenshot</span></div></div>'
      + '<span>' + drop.ts + '</span><span class="loot-row-value">' + fmtGP(drop.value) + ' gp</span></article>';
  }}).join('');
}}

function openLootEvidence(idx) {{
  const sorted = [...DROPS].sort((a, b) => lootSortKey === 'value' ? b.value - a.value : (b.ts_iso > a.ts_iso ? 1 : -1));
  lbItems = sorted.map(d => ({{src: d.src, label: (d.qty > 1 ? d.qty + 'x ' : '') + d.item, ts: d.ts}}));
  lbIndex = idx;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

// ── Pets lightbox ────────────────────────────────────────────────────
const PET_DATA = {pets_json};
function openPetItem(idx) {{
  lbItems = PET_DATA;
  lbIndex = idx;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

// ── Hall of Fame lightbox ─────────────────────────────────────────────
const HOF_DATA = {hof_data_json};
function openHofItem(idx) {{
  lbItems = HOF_DATA;
  lbIndex = idx;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

// ── Boss Tab ──────────────────────────────────────────────────────────
let BOSS_DATA = [];
try {{ BOSS_DATA = {boss_cards_json}; }} catch(e) {{ console.error('Boss data parse error:', e); }}
let bossRendered = false;
let selectedBossIndex = 0;
let bossCategory = 'All';
let bossSortMode = 'evidence';
const BOSS_SHOT_ITEMS = [];

function renderBosses() {{
  if (!bossRendered) {{
    bossRendered = true;
    const categories = ['All', ...new Set(BOSS_DATA.map(b => b.category))];
    document.getElementById('boss-category-tabs').innerHTML = categories.map((cat, idx) => '<button class="' + (idx === 0 ? 'active' : '') + '" data-boss-category="' + cat + '">' + cat + '</button>').join('');
    document.getElementById('boss-category-tabs').addEventListener('click', event => {{
      const btn = event.target.closest('[data-boss-category]');
      if (!btn) return;
      bossCategory = btn.dataset.bossCategory;
      document.querySelectorAll('[data-boss-category]').forEach(item => item.classList.toggle('active', item === btn));
      renderBossDirectory();
    }});
    document.getElementById('boss-search').addEventListener('input', renderBossDirectory);
    const totalEvidence = BOSS_DATA.reduce((sum, b) => sum + b.evidence_count, 0);
    const valuableBosses = BOSS_DATA.filter(b => b.drops.length).length;
    const capturedTasks = BOSS_DATA.reduce((sum, b) => sum + (b.ca_captured || 0), 0);
    document.getElementById('boss-summary').innerHTML = [
      [BOSS_DATA.length, 'Boss stories', 'Screenshot-backed encounters'],
      [totalEvidence, 'Evidence items', 'Drops, logs and achievements'],
      [valuableBosses, 'Valuable-drop bosses', 'Named routed evidence'],
      [capturedTasks, 'Combat tasks captured', 'Screenshots, not completion rate']
    ].map(item => '<article><strong>' + item[0] + '</strong><span>' + item[1] + '</span><small>' + item[2] + '</small></article>').join('');
  }}
  renderBossDirectory();
}}

function toggleBossSort(btn) {{
  bossSortMode = bossSortMode === 'evidence' ? 'kc' : 'evidence';
  btn.textContent = bossSortMode === 'evidence' ? 'Most evidence' : 'Highest KC';
  renderBossDirectory();
}}

function renderBossDirectory() {{
  const grid = document.getElementById('boss-grid');
  if (!grid || BOSS_DATA.length === 0) {{
    if (grid) grid.innerHTML = '<p class="empty-note" style="padding:20px">No boss screenshots found.</p>';
    return;
  }}
  const search = document.getElementById('boss-search').value.trim().toLowerCase();
  let rows = BOSS_DATA.map((boss, index) => ({{boss, index}})).filter(item => (bossCategory === 'All' || item.boss.category === bossCategory) && (!search || item.boss.boss.toLowerCase().includes(search)));
  rows.sort((a, b) => bossSortMode === 'kc' ? (b.boss.kc - a.boss.kc) || (b.boss.evidence_count - a.boss.evidence_count) : (b.boss.evidence_count - a.boss.evidence_count) || (b.boss.kc - a.boss.kc));
  if (!rows.length) {{ grid.innerHTML = '<p class="empty-note">No bosses match this view.</p>'; return; }}
  if (!rows.some(item => item.index === selectedBossIndex)) selectedBossIndex = rows[0].index;
  grid.innerHTML = rows.map(item => {{
    const b = item.boss;
    const initials = b.boss.split(/\s+/).map(word => word[0]).join('').slice(0, 2);
    const visual = b.representative ? '<img src="' + b.representative + '" alt="" loading="lazy">' : '<span class="boss-card-monogram">' + initials + '</span>';
    return '<button class="boss-directory-card ' + (item.index === selectedBossIndex ? 'active' : '') + '" data-boss-index="' + item.index + '" onclick="selectBoss(' + item.index + ')">' + visual
      + '<span><strong>' + b.boss + '</strong><span>' + b.category + (b.kc ? ' · ' + b.kc.toLocaleString() + ' KC' : '') + '</span><small>' + b.evidence_count + ' evidence item' + (b.evidence_count === 1 ? '' : 's') + (b.gp_str ? ' · ' + b.gp_str + ' logged' : '') + '</small></span></button>';
  }}).join('');
  selectBoss(selectedBossIndex, false);
}}

function selectBoss(idx, rerender = true) {{
  selectedBossIndex = idx;
  document.querySelectorAll('.boss-directory-card').forEach(card => card.classList.toggle('active', Number(card.dataset.bossIndex) === idx));
  const b = BOSS_DATA[idx];
  if (!b) return;
  BOSS_SHOT_ITEMS.length = 0;
  const sections = [['Valuable Drops', b.drops], ['Collection Log', b.collection], ['Untradeables', b.untradeable], ['Combat Achievements' + (b.ca_total ? ' · ' + b.ca_captured + ' of ' + b.ca_total + ' screenshots captured' : ''), b.achievements]];
  let evidenceHtml = '';
  sections.forEach(section => {{
    if (!section[1] || !section[1].length) return;
    const cards = section[1].map(item => {{
      const shotIndex = BOSS_SHOT_ITEMS.length;
      BOSS_SHOT_ITEMS.push({{src:item.src,label:item.label,ts:item.ts}});
      return '<button class="boss-evidence-card" onclick="openBossIdx(' + shotIndex + ')"><img src="' + item.src + '" alt="" loading="lazy"><span>' + item.label + (item.value ? ' · ' + item.value : '') + '</span></button>';
    }}).join('');
    evidenceHtml += '<section class="boss-evidence-section"><h3>' + section[0] + '</h3><div class="boss-evidence-grid">' + cards + '</div></section>';
  }});
  const initials = b.boss.split(/\s+/).map(word => word[0]).join('').slice(0, 2);
  const representativeIndex = Math.max(0, BOSS_SHOT_ITEMS.findIndex(item => item.src === b.representative));
  const visual = b.representative ? '<button class="boss-hero-shot" onclick="openBossIdx(' + representativeIndex + ')" aria-label="Open ' + b.boss + ' screenshots"><img src="' + b.representative + '" alt=""></button>' : '<div class="boss-detail-monogram">' + initials + '</div>';
  document.getElementById('boss-detail-panel').innerHTML = '<div class="boss-detail-hero-new">' + visual + '<div><h2>' + b.boss + '</h2><p>' + (b.kc ? b.kc.toLocaleString() + ' tracked kills' : 'KC unavailable') + ' · ' + b.category + '</p><p>' + (b.gp_str ? b.gp_str + ' realized value logged' : 'No realized GP attributed') + '</p></div></div>'
    + '<div class="boss-evidence-stats"><div><strong>' + b.evidence_count + '</strong><span>Evidence items</span></div><div><strong>' + b.drops.length + '</strong><span>Valuable drops</span></div><div><strong>' + (b.ca_captured || 0) + (b.ca_total ? ' / ' + b.ca_total : '') + '</strong><span>CA screenshots / Wiki tasks</span></div></div>'
    + (evidenceHtml || '<p class="empty-note">No screenshots captured for this boss yet.</p>');
}}

function openBossIdx(idx) {{
  lbItems = BOSS_SHOT_ITEMS;
  lbIndex = idx;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

// ── This Week in Gielinor ──────────────────────────────────────────────
const THIS_WEEK_MEMORIES = {this_week_memories_json};
const MEMORY_PAGE_SIZE = 4;
let memoryPageIndex = 0;

function renderMemoryWeek() {{
  const section = document.getElementById('memory-week');
  const grid = document.getElementById('memory-grid');
  if (!THIS_WEEK_MEMORIES.length) {{
    section.style.display = 'none';
    return;
  }}
  section.style.display = 'block';
  const pageCount = Math.ceil(THIS_WEEK_MEMORIES.length / MEMORY_PAGE_SIZE);
  memoryPageIndex = ((memoryPageIndex % pageCount) + pageCount) % pageCount;
  const start = memoryPageIndex * MEMORY_PAGE_SIZE;
  const pageItems = THIS_WEEK_MEMORIES.slice(start, start + MEMORY_PAGE_SIZE);
  grid.innerHTML = pageItems.map(function(memory) {{
    return '<div class="memory-card" onclick="openMemoryItem(' + memory.idx + ')">'
      + '<img src="' + memory.src + '" alt="" loading="lazy">'
      + '<div class="memory-card-body">'
      + '<div class="memory-card-top"><span class="memory-badge" style="color:' + memory.color + '">'
      + memory.badge + '</span><span class="memory-year">' + memory.year + '</span></div>'
      + '<div class="memory-title">' + memory.title + '</div>'
      + '<div class="memory-sub">' + memory.sub + '</div>'
      + '<div class="memory-date">' + memory.ts_str + ' · ' + memory.years_ago_str + '</div>'
      + '</div></div>';
  }}).join('');
  document.getElementById('memory-page-label').textContent = (memoryPageIndex + 1) + ' of ' + pageCount;
}}

function changeMemoryPage(direction) {{
  if (!THIS_WEEK_MEMORIES.length) return;
  memoryPageIndex += direction;
  renderMemoryWeek();
}}

function openMemoryItem(idx) {{
  lbItems = THIS_WEEK_MEMORIES.map(function(memory) {{
    return {{src: memory.src, label: memory.title, ts: memory.ts_str + ' · ' + memory.years_ago_str}};
  }});
  lbIndex = idx;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

// ── Chronicle ─────────────────────────────────────────────────────────
const CHRONICLE = {chronicle_json};
const YEAR_RECAPS = {year_recaps_json};
let chronFilterActive = 'all';
let chronRendered = false;

function renderChronicle() {{
  if (chronRendered) return;
  chronRendered = true;
  renderMemoryWeek();
  buildChronicle();
}}

function setChronFilter(type, btn) {{
  chronFilterActive = type;
  document.querySelectorAll('.chron-filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  btn.classList.add('active');
  buildChronicle();
}}

function chronScrollTo(yr) {{
  var el = document.getElementById('chron-yr-' + yr);
  if (el) el.scrollIntoView({{behavior: 'smooth', block: 'start'}});
}}

function hideChronImg(img) {{ img.style.display = 'none'; }}

function openChronItem(idx) {{
  lbItems = CHRONICLE.filter(function(e) {{ return e.src; }}).map(function(e) {{ return {{src: e.src, label: e.title, ts: e.ts_str}}; }});
  var targetSrc = CHRONICLE[idx].src;
  lbIndex = lbItems.findIndex(function(item) {{ return item.src === targetSrc; }});
  if (lbIndex < 0) lbIndex = 0;
  showLb();
  document.getElementById('lightbox').classList.add('open');
}}

function buildChronicle() {{
  var filtered = chronFilterActive === 'all' ? CHRONICLE : CHRONICLE.filter(function(e) {{ return e.type === chronFilterActive; }});
  document.getElementById('chron-count').textContent = filtered.length + ' events';

  var byYear = {{}};
  filtered.forEach(function(e) {{
    var yr = e.ts_iso.substring(0, 4);
    if (!byYear[yr]) byYear[yr] = [];
    byYear[yr].push(e);
  }});
  var years = Object.keys(byYear).sort().reverse();

  var yearNav = document.getElementById('chron-year-nav');
  yearNav.innerHTML = years.map(function(yr) {{
    return '<button class="chron-year-chip" onclick="chronScrollTo(' + yr + ')">' + yr + '</button>';
  }}).join('');

  var html = '';
  years.forEach(function(yr) {{
    html += '<div class="chron-year-section" id="chron-yr-' + yr + '">';
    html += '<div class="chron-year-heading">' + yr + '</div>';
    var recap = YEAR_RECAPS[yr];
    if (recap && chronFilterActive === 'all') {{
      var chips = recap.chips.map(function(c) {{ return '<span class="yr-chip"><b>' + c[0] + '</b>' + c[1] + '</span>'; }}).join('');
      html += '<div class="yr-recap"><div class="yr-chips">' + chips + '</div><p class="yr-prose">' + recap.prose + '</p></div>';
    }}
    var ordered = byYear[yr].slice().sort(function(a, b) {{ return b.ts_iso.localeCompare(a.ts_iso); }});
    var storyGroups = [];
    ordered.forEach(function(event) {{
      var month = event.ts_iso.substring(0, 7);
      var last = storyGroups[storyGroups.length - 1];
      if (!last || last.month !== month || last.events.length >= 4) {{ last = {{month: month, events: []}}; storyGroups.push(last); }}
      last.events.push(event);
    }});
    storyGroups.forEach(function(group) {{
      var shots = group.events.filter(function(event) {{ return event.src; }});
      var media = shots.length ? '<div class="chron-story-media">' + shots.map(function(event) {{ return '<button onclick="openChronItem(' + event.idx + ')" aria-label="Open ' + event.title + ' screenshot"><img src="' + event.src + '" alt="" loading="lazy" onerror="hideChronImg(this)"></button>'; }}).join('') + '</div>' : '';
      var events = group.events.map(function(event) {{ return '<div class="chron-story-event"><strong>' + event.title + '</strong><span>' + event.badge + (event.sub ? ' · ' + event.sub : '') + ' · ' + event.ts_str + '</span></div>'; }}).join('');
      html += '<article class="chron-story"' + (media ? '' : ' style="grid-template-columns:1fr"') + '>' + media + '<div class="chron-story-body"><div class="chron-story-date">' + journeyMonthLabel(group.month) + '</div>' + events + '</div></article>';
    }});
    html += '</div>';
  }});

  document.getElementById('chron-body').innerHTML = html || '<p class="empty-note" style="padding:20px">No events match this filter.</p>';
}}

// ── Page switching ──────────────────────────────────────────────────
const PAGE_HEADINGS = {{
  stats: [{json.dumps(PLAYER_NAME)}, '— Account Overview'],
  max: ['Maxing Journey', '— The road ahead and account journey behind it'],
  luck: ['Luck', '— Probability, ownership and evidence gaps'],
  bosses: ['Bosses', '— Encounters, drops and achievements'],
  loot: ['Loot Log', '— The drops behind the wealth'],
  gallery: ['Gallery', '— Every screenshot has a story'],
  chronicle: ['Chronicle', '— The story of the account, year by year']
}};

function toggleNavigation() {{
  const rail = document.getElementById('side-rail');
  const scrim = document.getElementById('nav-scrim');
  const open = !rail.classList.contains('open');
  rail.classList.toggle('open', open);
  scrim.classList.toggle('open', open);
}}

function closeNavigation() {{
  document.getElementById('side-rail').classList.remove('open');
  document.getElementById('nav-scrim').classList.remove('open');
}}

function switchPage(id, btn) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + id).classList.add('active');
  btn.classList.add('active');
  const heading = PAGE_HEADINGS[id] || PAGE_HEADINGS.stats;
  document.getElementById('page-heading-title').textContent = heading[0];
  document.getElementById('page-heading-subtitle').textContent = heading[1];
  document.title = heading[0] + ' — OSRS Dashboard';
  closeNavigation();
  window.scrollTo({{top: 0, left: 0, behavior: 'instant'}});
  if (id === 'gallery' && activeItems.length === 0) filterGallery();
  if (id === 'loot') renderLoot();
  if (id === 'bosses') renderBosses();
  if (id === 'chronicle') renderChronicle();
}}

function goToPage(id) {{
  const btn = Array.from(document.querySelectorAll('#side-rail .nav-item')).find(item => item.getAttribute('onclick').includes("'" + id + "'"));
  if (btn) switchPage(id, btn);
}}

document.addEventListener('keydown', event => {{
  if (event.key === 'Escape') closeNavigation();
}});

// Initialize the static gallery immediately, then enable durable interaction
// when the private local service is available.
initializeDashboardApp();
</script>
</body>
</html>"""
    return html


def generate_dashboard():
    """Build the dashboard once and return a summary for the local service."""
    print(f"Player:   {PLAYER_NAME}")
    print(f"Scanning: {SCREENSHOTS_PATH}")

    if not Path(SCREENSHOTS_PATH).exists():
        print(f"\nSCREENSHOTS_PATH doesn't exist. Check that:")
        print(f"  - PLAYER_NAME ({PLAYER_NAME!r}) matches your RuneLite character name")
        print(f"  - RuneLite is installed and has saved at least one screenshot")
        print(f"  - Override SCREENSHOTS_PATH in config.py if your folder lives elsewhere")
        return {"ok": False, "message": "Screenshot folder not found."}

    data = scan_screenshots(SCREENSHOTS_PATH, merge_folders=MERGE_FOLDERS)

    if data["total"] == 0:
        print("No screenshots found in that folder.")
        return {"ok": False, "message": "No screenshots found."}

    print(f"Found {data['total']} screenshots across {len(data['categories'])} categories.")
    print(f"Level-up data parsed for {len(data['level_ups'])} skills.")

    hiscores = fetch_hiscores(PLAYER_NAME, debug=False)
    newly_seen_bosses = update_known_bosses(hiscores.get("boss_names", []))
    xp_history = merge_xp_history(update_xp_history(hiscores), MERGE_FOLDERS)
    favorite_paths = load_all_favorites(SCREENSHOTS_PATH, MERGE_FOLDERS)
    acquisitions = build_economic_acquisitions(data, VALUE_COMPONENT_OVERRIDES)
    if FORCE_BOSS_DATA_REFRESH:
        _force_bosses = [name for name in hiscores.get("boss_names", []) if name]
        print(f"\nRefreshing wiki drop tables for {len(_force_bosses)} bosses.")
        print("This runs one wiki lookup per boss. Ctrl+C stops it and keeps")
        print("whatever finished; the dashboard still builds either way.\n")
    else:
        _force_bosses = None
    discovery = refresh_catalog(
        newly_seen_bosses,
        acquisitions,
        VALUE_RECIPES,
        WIKI_DISCOVERY_CATALOG_FILE,
        force_bosses=_force_bosses,
        progress=_boss_refresh_progress if FORCE_BOSS_DATA_REFRESH else None,
    )
    if discovery.get("interrupted"):
        print("\nStopped early. Everything fetched so far was saved.\n")
    if discovery.get("refreshed_bosses"):
        print(f"Refreshed drop tables for {len(discovery['refreshed_bosses'])} bosses.")
    apply_discovered_boss_references(discovery.get("bosses"))
    all_value_recipes = list(VALUE_RECIPES) + [
        recipe for recipe in discovery.get("recipes", [])
        if recipe.get("id") not in {base["id"] for base in VALUE_RECIPES}
    ]
    if discovery.get("new_bosses"):
        print("Learned wiki references for: " + ", ".join(discovery["new_bosses"]))
    if discovery.get("new_recipes"):
        print("Learned indirect-value recipes: " + ", ".join(discovery["new_recipes"]))
    rare_boss_drop_items = {
        item
        for drops in _DROP_RATES.values()
        for item, rate in drops.items()
        if (probability := parse_rate_string(rate)) is not None and probability <= LUCK_MAX_P
    }
    economic_value = resolve_economic_value(
        acquisitions,
        VALUE_PRICE_CACHE_FILE,
        ECONOMIC_EVENTS_FILE,
        player_name=PLAYER_NAME,
        audit_item_keys=rare_boss_drop_items,
        recipes=all_value_recipes,
    )
    if economic_value.get("events"):
        print(
            f"Resolved {len(economic_value['events'])} indirect-value events "
            f"worth {economic_value['realized_total']:,} GP."
        )
    if economic_value.get("pending"):
        print(f"Tracked {len(economic_value['pending'])} incomplete value recipes.")
    if economic_value.get("unregistered_observed"):
        print(
            f"Value coverage audit: {len(economic_value['unregistered_observed'])} "
            "observed untradeable/collection items are not recipe components."
        )
    html = build_html(data, hiscores, xp_history, favorite_paths, economic_value, discovery)

    # Replace atomically so the service never serves a half-written page while
    # a refresh is running.
    output_path = Path(OUTPUT_FILE)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        f.write(html)
    os.replace(temp_path, output_path)

    print(f"\nDashboard saved to:\n  {OUTPUT_FILE}")
    return {
        "ok": True,
        "message": f"Refreshed {data['total']:,} screenshots.",
        "screenshots": data["total"],
        "favorites": len(favorite_paths),
        # Reported in in-app issue reports so a bug filed during a hiscores
        # outage is recognisable as one without a round of questions.
        "hiscores": bool(hiscores.get("skills")),
        "indirect_value": economic_value.get("realized_total", 0),
        "pending_value": economic_value.get("latent_total", 0),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def main():
    result = generate_dashboard()
    if not result.get("ok"):
        return

    # Auto-open in default browser unless --no-open is passed
    if "--no-open" not in sys.argv:
        webbrowser.open(f"file:///{OUTPUT_FILE}".replace("\\", "/"))


if __name__ == "__main__":
    main()
