"""
audit_shared_drops.py
─────────────────────
Scans BOSS_DROPS (manual + auto-generated) and finds items that appear in
multiple bosses' drop lists but haven't been classified into GLOBAL_SHARED_DROPS
or CATEGORY_SHARED_DROPS yet.

Output: a sorted report listing each candidate item, the bosses it appears
under, and a suggestion (Global / Category-shared / Specific boss).

Run locally:
    python audit_shared_drops.py

Then either:
  - Edit GLOBAL_SHARED_DROPS / CATEGORY_SHARED_DROPS in osrs_dashboard.py
    based on the report.
  - Or open a Cowork session and ask Claude to walk through the candidates
    with you interactively (Claude can ask one-by-one and update the file).
"""

from collections import defaultdict
from pathlib import Path
import sys

# Reuse the dashboard's data so we audit exactly what it uses
sys.path.insert(0, str(Path(__file__).parent))
from osrs_dashboard import (
    BOSS_DROPS,
    BOSS_CATEGORIES,
    GLOBAL_SHARED_DROPS,
    CATEGORY_SHARED_DROPS,
    BOSS_TO_CATEGORY,
)


def find_candidates():
    """Return list of (item_lower, original_name, bosses, categories, suggestion)."""
    # Build item → list of bosses it appears under
    item_to_bosses = defaultdict(list)
    item_original_name = {}
    for boss, items in BOSS_DROPS.items():
        for item in items:
            key = item.lower()
            item_to_bosses[key].append(boss)
            if key not in item_original_name:
                item_original_name[key] = item

    # Already-categorized items — skip these
    already_global = set(GLOBAL_SHARED_DROPS)
    already_category = {it for items in CATEGORY_SHARED_DROPS.values() for it in items}
    already_classified = already_global | already_category

    candidates = []
    for key, bosses in item_to_bosses.items():
        if key in already_classified:
            continue
        if len(bosses) < 2:
            continue

        unique_bosses = sorted(set(bosses))
        unique_categories = sorted({
            BOSS_TO_CATEGORY.get(b, "Other") for b in unique_bosses
        })

        # Suggest where it should go
        if len(unique_categories) == 1:
            cat = unique_categories[0]
            cat_bosses = next(
                (cb for cn, cb in BOSS_CATEGORIES if cn == cat),
                [],
            )
            real_cat_bosses = [b for b in cat_bosses
                               if not (b == "Common Drops" or b.startswith("Shared "))]
            if len(unique_bosses) == len(real_cat_bosses) and real_cat_bosses:
                suggestion = f"CATEGORY-SHARED ({cat}) — drops from every boss in {cat}"
            elif len(unique_bosses) >= max(2, len(real_cat_bosses) // 2):
                suggestion = f"likely CATEGORY-SHARED ({cat})"
            else:
                suggestion = f"appears in {len(unique_bosses)} {cat} bosses — your call"
        elif len(unique_categories) >= 3 or len(unique_bosses) >= 5:
            suggestion = "likely GLOBAL — drops from many bosses across categories"
        else:
            suggestion = f"spans {len(unique_categories)} categories — your call"

        candidates.append((key, item_original_name[key], unique_bosses, unique_categories, suggestion))

    # Sort by number of bosses descending, then alphabetical
    candidates.sort(key=lambda x: (-len(x[2]), x[1]))
    return candidates


def main():
    print("Auditing BOSS_DROPS for shared items not yet classified...\n")
    candidates = find_candidates()

    if not candidates:
        print("Nothing to flag — every multi-boss item is already in a shared list.")
        return

    print(f"Found {len(candidates)} candidate(s). Sorted by how many bosses each touches:\n")
    print("=" * 90)
    for key, name, bosses, cats, suggestion in candidates:
        print(f"\n{name}  ({len(bosses)} bosses, {len(cats)} categor{'y' if len(cats)==1 else 'ies'})")
        print(f"  Suggestion: {suggestion}")
        print(f"  Categories: {', '.join(cats)}")
        bosses_str = ", ".join(bosses)
        if len(bosses_str) > 80:
            bosses_str = bosses_str[:77] + "..."
        print(f"  Bosses:     {bosses_str}")

    print("\n" + "=" * 90)
    print(f"\n{len(candidates)} item(s) flagged.")
    print("\nNext steps:")
    print("  1. Skim the list above. Items the script confidently labels GLOBAL or")
    print("     CATEGORY-SHARED are usually safe to add directly.")
    print("  2. Open osrs_dashboard.py and add entries to either:")
    print("     - GLOBAL_SHARED_DROPS  (item drops from many bosses across categories)")
    print("     - CATEGORY_SHARED_DROPS[<category>]  (item drops from any boss in one category)")
    print("  3. Or open a Cowork session and ask Claude to walk through the report")
    print("     with you — Claude can ask about each item and edit the file.")


if __name__ == "__main__":
    main()
