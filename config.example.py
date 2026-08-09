"""
Personal config for the OSRS Dashboard.

To use:
  1. Copy this file to `config.py` (same folder).
  2. Set PLAYER_NAME to your in-game character name.
  3. (Optional) Override SCREENSHOTS_PATH if your RuneLite screenshots
     folder is somewhere other than the default.

Once `config.py` exists, the dashboard reads from it instead of the defaults
baked into osrs_dashboard.py — so future updates to the project don't
overwrite your settings.
"""

from pathlib import Path

# Your OSRS character name (case-sensitive — must match RuneLite's folder).
PLAYER_NAME = "YourCharacterName"

# RuneLite's standard screenshots folder. Only override if you've changed
# RuneLite's screenshot directory in its settings.
SCREENSHOTS_PATH = str(Path.home() / ".runelite" / "screenshots" / PLAYER_NAME)

# Road to Max tab (optional). Active skills are detected automatically from
# your XP gains — whatever you've actually been training gets the "Active"
# badge. Set ACTIVE_SKILLS only if you want to override that manually.
ACTIVE_SKILLS = []

# Luck tab: loot you KNOW you have from before you enabled RuneLite
# screenshots. The luck engine can only see screenshot evidence — anything
# you got before that is invisible and the boss shows "no screenshot
# evidence". List attested items here (boss → {item: copies}) and they
# count as owned. Example:
# LUCK_OWNED_OVERRIDES = {
#     "Kraken": {"Kraken tentacle": 1, "Pet kraken": 2},
# }
LUCK_OWNED_OVERRIDES = {}

# One-time additions for known untradeable components that are not present in
# your RuneLite screenshot history. Leave empty for normal use. Values are
# extra copies beyond screenshot evidence. Include the date you recorded the
# attestation (keep it fixed forever — it anchors the wealth ledger):
# VALUE_COMPONENT_OVERRIDES = {
#     "Eye of the duke": {"qty": 1, "attested_on": "2026-07-17"},
# }
# A bare count like {"Eye of the duke": 1} also works.
VALUE_COMPONENT_OVERRIDES = {}
