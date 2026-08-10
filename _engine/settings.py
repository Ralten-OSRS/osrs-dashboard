#!/usr/bin/env python3
"""Machine-local launcher settings.

This cannot live beside the screenshots like `xp_history.json` does, because
the whole point is to remember which screenshot folder to use — the folder is
not known until after the setting is read. So it goes in the per-user
application data directory instead.

**One account record is one dashboard.** RuneLite names its screenshot folder
after the account name *and* the world type, as `<Name>-<Mode>`, so a single
character can own several folders: a main folder, a folder per league, a beta
folder. Two different relationships hide in that set and they are not the same
thing:

* A **rename** produces a folder with a new root name and no link to the old
  one. Those folders are one account's history split in two, and they merge —
  that is `also_folders`.
* A **game mode** produces a folder that is the same character but a separate
  progression universe. Its XP, drops and wealth must never pool into the main
  account's numbers, for exactly the reason an ironman's must not. Those get
  their own record, linked by `character_id` so the interface can offer to
  switch between them.

Because each record is a dashboard, merging is entirely contained in one
record's `also_folders` and nothing has to be derived at read time.

**Nothing here is inferred from folder names.** The `-<Mode>` suffix is a real
convention today, but it belongs to RuneLite and can change in any release —
this project already lost that bet once when a screenshot subfolder was renamed
and its contents silently fell out of the dashboard. So the suffix is only ever
used to pre-tick a checkbox the user confirms during setup; what gets stored
here is the user's answer, as explicit folder lists. If the convention changes,
setup offers nothing and the user ticks the boxes themselves.

Nothing here ever ships inside the executable. The engine stays neutral
(DESIGN.md non-negotiable #10); this file is written on the user's own machine
and describes only their own setup.
"""

import json
import os
import sys
from pathlib import Path

# 2 adds `mode`, `character_id` and `hiscores_name`. Version 1 files are
# upgraded in memory on read and never rewritten just to bump the number, so
# downgrading to an older build keeps working: version 1's reader picks the
# keys it knows and ignores the rest.
SCHEMA_VERSION = 2
APP_FOLDER_NAME = "OSRS Dashboard"

# The mode value for an ordinary account folder — the one the hiscores know.
MAIN_MODE = "main"


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


def _clean_folder_list(value, exclude=()):
    """Strings only, stripped, de-duplicated, order preserved, never the primary.

    A folder listed both as the primary and as a merge target would be scanned
    twice, which double-counts every screenshot in it. That is a silent wrong
    number rather than an error, so it is filtered here rather than trusted.
    """
    cleaned = []
    seen = {name for name in exclude}
    for item in value or []:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return cleaned


def _upgrade(entry):
    """Normalise one stored record, filling in anything a version 1 file lacks.

    Returns None when the entry cannot be trusted at all, which is only when it
    has no primary folder — without that there is no dashboard to build.
    """
    if not isinstance(entry, dict):
        return None
    primary = entry.get("primary_folder")
    if not isinstance(primary, str) or not primary.strip():
        return None
    primary = primary.strip()

    display = entry.get("display_name")
    display = display.strip() if isinstance(display, str) and display.strip() else primary

    mode = entry.get("mode")
    mode = mode.strip() if isinstance(mode, str) and mode.strip() else MAIN_MODE

    # A version 1 record predates game-mode awareness, so it is a main account
    # standing alone. Keying the character on its own primary folder gives it a
    # stable identity that later mode records can be attached to.
    character = entry.get("character_id")
    character = character.strip() if isinstance(character, str) and character.strip() else primary

    # Only a main account has a name the hiscores can answer for. A league or
    # beta folder is a real character but lives on a separate hiscores endpoint
    # this app does not call, so the correct value is "don't ask" rather than a
    # folder name that would return an empty result and read as a dry account.
    hiscores = entry.get("hiscores_name")
    if isinstance(hiscores, str) and hiscores.strip():
        hiscores = hiscores.strip()
    elif "hiscores_name" in entry and hiscores is None:
        hiscores = None
    else:
        hiscores = primary if mode == MAIN_MODE else None

    prefs = entry.get("preferences")

    return {
        "display_name": display,
        "primary_folder": primary,
        "also_folders": _clean_folder_list(entry.get("also_folders"), exclude=(primary,)),
        "mode": mode,
        "character_id": character,
        "hiscores_name": hiscores,
        "preferences": prefs if isinstance(prefs, dict) else {},
    }


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
    claimed = set()
    for entry in data.get("accounts") or []:
        upgraded = _upgrade(entry)
        if upgraded is None:
            continue
        # Two records claiming the same primary folder would both write their
        # dashboard to it. Keep the first and drop the rest.
        if upgraded["primary_folder"] in claimed:
            continue
        claimed.add(upgraded["primary_folder"])
        accounts.append(upgraded)

    # Deliberately no rule here refusing a merge because the folder is also
    # some other record's primary. An earlier version had one, to stop a
    # folder counting toward two dashboards, and it broke the exact case this
    # feature exists for: after a rename the old folder *always* has its own
    # record, because the user was using it right up until they renamed. The
    # declaration was silently discarded and the merge never happened. Two
    # dashboards sharing a folder is not a double count anyway — they are
    # separate dashboards. Ownership is resolved when the user declares a
    # former name, by absorbing it (see describe_account), not by filtering
    # here where the user's intent is no longer visible.

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
        entry = _upgrade({
            "display_name": display_name or folder,
            "primary_folder": folder,
        })
        data["accounts"].append(entry)
    elif display_name:
        entry["display_name"] = display_name
    data["last_used"] = folder
    save(data)
    return entry


def describe_account(folder, also_folders=None, mode=None, character_id=None,
                     display_name=None, hiscores_name=None):
    """Write the grouping the user declared during setup. Returns the record.

    Every field the caller leaves out keeps whatever the record already had, so
    this can be used to set the merge list without disturbing the mode, or the
    reverse. Passing `hiscores_name=False` clears it — `None` means "unchanged"
    here, and a league record genuinely wants a stored null.
    """
    data = load()
    entry = get_account(data, folder)
    if entry is None:
        entry = _upgrade({"primary_folder": folder, "display_name": display_name or folder})
        data["accounts"].append(entry)

    if display_name:
        entry["display_name"] = display_name
    if mode:
        entry["mode"] = mode

    inherited_character = None
    if also_folders is not None:
        entry["also_folders"] = _clean_folder_list(
            also_folders,
            exclude=(entry["primary_folder"],),
        )
        # Declaring a folder to be this account under a former name absorbs it.
        # Before the rename that folder was an account in its own right, so a
        # record for it exists and would otherwise go on claiming to be a
        # separate character with its own hiscores name. The surviving record
        # takes over, and inherits the absorbed identity so the account's
        # history stays continuous rather than restarting at the new name.
        #
        # Nothing on disk is touched: the folder keeps its own screenshots,
        # favourites and XP history, so un-declaring it restores it as a
        # standalone account exactly as it was.
        absorbed = [
            other for other in data["accounts"]
            if other is not entry and other["primary_folder"] in entry["also_folders"]
        ]
        for other in absorbed:
            if inherited_character is None:
                inherited_character = other["character_id"]
            data["accounts"].remove(other)
            if data.get("last_used") == other["primary_folder"]:
                data["last_used"] = entry["primary_folder"]

    if character_id:
        entry["character_id"] = character_id
    elif inherited_character:
        entry["character_id"] = inherited_character

    if hiscores_name is False:
        entry["hiscores_name"] = None
    elif hiscores_name:
        entry["hiscores_name"] = hiscores_name

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


def siblings(data, character_id, exclude_folder=None):
    """Every other dashboard belonging to the same character, main mode first.

    This is what the interface offers as "other modes you have played" — a
    switch target, never something merged into the current dashboard.
    """
    found = [
        entry for entry in data.get("accounts") or []
        if entry.get("character_id") == character_id
        and entry["primary_folder"] != exclude_folder
    ]
    found.sort(key=lambda e: (e.get("mode") != MAIN_MODE, e["display_name"].lower()))
    return found


def account_folders(entry, base):
    """Every screenshot folder this dashboard reads, primary folder first.

    The primary folder is where the dashboard and its data files are written,
    so it leads and everything else is merged history behind it. Folders that
    no longer exist are dropped rather than reported: a user who deleted an old
    screenshot folder should still get a dashboard from what remains.
    """
    base = Path(base)
    folders = [base / entry["primary_folder"]]
    folders.extend(base / name for name in entry.get("also_folders", []))
    return [path for path in folders if path.is_dir()]


def declared_roots(entry, base):
    """Resolved directories this account is permitted to serve files from.

    The local service refuses any path that escapes its root, which is what
    stops a page asking for arbitrary files. Merging a renamed account means
    screenshots genuinely do live outside that root, so the guard needs the
    specific list of folders the user declared rather than a relaxed rule. This
    is that list, resolved once so the comparison is against real paths and not
    against strings a request could be crafted to match.
    """
    return [path.resolve() for path in account_folders(entry, base)]
