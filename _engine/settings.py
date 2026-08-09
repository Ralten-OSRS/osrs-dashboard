#!/usr/bin/env python3
"""Machine-local launcher settings.

This cannot live beside the screenshots like `xp_history.json` does, because
the whole point is to remember which screenshot folder to use — the folder is
not known until after the setting is read. So it goes in the per-user
application data directory instead.

The stored shape is an account *record*, not a single remembered name. That is
deliberate and it is for Issue #1: RuneLite names its screenshot folder after
the current display name, so changing your name splits one account's history
across folders. `also_folders` is empty today and unused, but having it in the
schema from the first release means name-change support is a scan-and-merge
change rather than a migration applied to every existing user's settings file.

Nothing here ever ships inside the executable. The engine stays neutral
(DESIGN.md non-negotiable #10); this file is written on the user's own machine
and describes only their own setup.
"""

import json
import os
import sys
from pathlib import Path

SCHEMA_VERSION = 1
APP_FOLDER_NAME = "OSRS Dashboard"


def settings_dir():
    """Per-user application data, with sane fallbacks off Windows."""
    base = os.environ.get("LOCALAPPDATA")
    if not base and sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    if not base:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_FOLDER_NAME


def settings_path():
    return settings_dir() / "settings.json"


def _blank():
    return {"version": SCHEMA_VERSION, "accounts": [], "last_used": None}


def load():
    """Read settings, tolerating anything. Never raises.

    A corrupt or hand-edited settings file must not stop someone opening their
    dashboard. The worst outcome allowed here is behaving like a first run.
    """
    try:
        with settings_path().open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()

    accounts = []
    for entry in data.get("accounts") or []:
        if not isinstance(entry, dict):
            continue
        primary = entry.get("primary_folder")
        if not isinstance(primary, str) or not primary.strip():
            continue
        display = entry.get("display_name")
        also = [f for f in (entry.get("also_folders") or []) if isinstance(f, str) and f.strip()]
        prefs = entry.get("preferences")
        accounts.append({
            "display_name": display if isinstance(display, str) and display.strip() else primary,
            "primary_folder": primary,
            "also_folders": also,
            "preferences": prefs if isinstance(prefs, dict) else {},
        })

    # A remembered choice is honoured only when it names a real account.
    # Anything else becomes "ask me" — deliberately, and never a guess at a
    # different account. Null is a meaningful value here: it is what
    # forget_last() writes so the picker runs again, so it must not be
    # helpfully repaired back into a choice.
    last_used = data.get("last_used")
    if not isinstance(last_used, str) or not any(a["primary_folder"] == last_used for a in accounts):
        last_used = None

    return {"version": SCHEMA_VERSION, "accounts": accounts, "last_used": last_used}


def save(data):
    """Write settings atomically. Returns True on success, False otherwise.

    Failure is survivable: the app just asks which character again next time.
    """
    path = settings_path()
    temp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        return True
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False


def get_account(data, folder):
    for entry in data.get("accounts") or []:
        if entry["primary_folder"] == folder:
            return entry
    return None


def remember_account(folder, display_name=None):
    """Record this character as the one to use next time. Returns the record."""
    data = load()
    entry = get_account(data, folder)
    if entry is None:
        entry = {
            "display_name": display_name or folder,
            "primary_folder": folder,
            "also_folders": [],
            "preferences": {},
        }
        data["accounts"].append(entry)
    elif display_name:
        entry["display_name"] = display_name
    data["last_used"] = folder
    save(data)
    return entry


def last_account():
    """The account to reuse, or None when there is nothing to reuse."""
    data = load()
    if not data["last_used"]:
        return None
    return get_account(data, data["last_used"])


def forget_last():
    """Drop the remembered choice without discarding the account record."""
    data = load()
    data["last_used"] = None
    save(data)


def account_folders(entry, base):
    """Every screenshot folder belonging to this account, primary first.

    Only the primary folder is used today. `also_folders` is read here so that
    Issue #1 has one place to change rather than several.
    """
    base = Path(base)
    folders = [base / entry["primary_folder"]]
    folders.extend(base / name for name in entry.get("also_folders", []))
    return [path for path in folders if path.is_dir()]
