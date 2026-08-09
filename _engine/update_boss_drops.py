"""
update_boss_drops.py
────────────────────
Scrapes OSRS Wiki drop tables for every boss tracked by the dashboard and writes
a fresh BOSS_DROPS dict to boss_drops_generated.py next to this script.

Run locally on your Windows machine (the Cowork sandbox can't reach the wiki):
    python update_boss_drops.py

After it finishes:
  - The dashboard imports boss_drops_generated.py automatically on next run, so
    you just need to refresh the dashboard.
  - If you want to inspect or hand-edit the output, open boss_drops_generated.py.

How it works:
  - Fetches each boss's raw wikitext via the MediaWiki parse API
    (action=parse&prop=wikitext).
  - Extracts {{DropsLine|...|name=...}} templates using a regex.
  - Filters out generic noise (runes, raw food, ores, herbs, logs, seeds) so
    only notable drops remain.
  - Adds a polite 0.5s delay between requests.

If a boss page name on the wiki doesn't match the hiscores name verbatim, add
an entry to NAME_OVERRIDES.
"""

import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Boss list comes from the dashboard engine — single source of truth.
# load_known_bosses() returns the seed list plus every boss discovered from
# the live hiscores API (cached in known_bosses.json), so a boss that ships
# in-game is in this scrape list automatically after the next dashboard
# refresh. (A hand-maintained copy lived here until July 2026 and drifted
# 9 entries behind; importing makes that impossible.)
sys.path.insert(0, str(Path(__file__).parent))
from osrs_dashboard import load_known_bosses

BOSSES = load_known_bosses()

# Wiki page name overrides (when the page title differs from the hiscores name).
NAME_OVERRIDES = {
    "Barrows Chests": "Barrows",
    "Chambers of Xeric": "Chambers of Xeric",
    "Chambers of Xeric: Challenge Mode": "Chambers of Xeric/Challenge Mode",
    "Theatre of Blood: Hard Mode": "Theatre of Blood/Hard Mode",
    "Tombs of Amascut: Expert Mode": "Tombs of Amascut/Expert Mode",
    "Phosani's Nightmare": "Phosani's Nightmare",
    "Lunar Chests": "Lunar Chest",
    "Tormented Demons": "Tormented Demon",
}

# Drops to skip — generic resource loot that just clutters the dict and never
# triggers a RuneLite "Valuable drop" screenshot anyway.
SKIP_PATTERNS = [
    r"^coins$",
    r"^bones$", r"^big bones$", r"^.*bones$",
    r"^cooked .*$", r"^raw .*$",
    r"^uncut .*$",
    r"^.* ore$",
    r"^.*runes?$", r"^pure essence$",
    r"^.* arrows?$", r"^.* bolts?$", r"^.* darts?$",
    r"^.* logs?$", r"^.* seed$", r"^.* sapling$",
    r"^.* herb$", r"^grimy .*$", r"^clean .*$",
    r"^vial.*$",
    r"^.* potion.*\(\d\)$",
    r"^super .*\(\d\)$", r"^.* potion\(\d\)$", r"^prayer potion\(\d\)$",
    r"^.*\(noted\)$",
    r"^.* bolt tips?$",
    r"^.* charm$",
    r"^.*ed body$", r"^.*ed legs$",
    r"^teleport to .*$", r"^.* teleport$",
    r"^casket$", r"^.* spice$",
    r"^uncharged .*$",
    r"^nothing$",
]

API_URL = "https://oldschool.runescape.wiki/api.php"
USER_AGENT = "OSRS-Dashboard-Scraper/2.0 (personal use)"


# ── Auto-categorization ────────────────────────────────────────────────
# Wiki page category tags → dashboard boss-tab categories. Priority order,
# first match wins — tuned July 2026 against the wiki tags of all ~70
# tracked bosses. Notes from that calibration:
#   - Nearly every boss carries the generic "Slayer monsters" tag (Zulrah,
#     Vorkath, KBD...), so it is deliberately NOT mapped — only the on-task
#     tag is a trustworthy Slayer signal.
#   - "Bosses" as a final fallback lands new bosses in Solo Bosses, the
#     dashboard's generic bucket, instead of "Other".
#   - Manual BOSS_CATEGORIES placements in the engine always beat these
#     hints, so a mapping miss can never move a curated boss.
WIKI_CATEGORY_MAP = [
    ("Raids", "Raids"),
    ("Desert Treasure II - The Fallen Empire", "Desert Treasure 2"),
    ("God Wars Dungeon", "God Wars Dungeon"),
    ("Wilderness", "Wilderness"),
    ("Inferno", "Caves & Colosseum"),
    ("TzHaar Fight Cave", "Caves & Colosseum"),
    ("Fortis Colosseum", "Caves & Colosseum"),
    ("Farming", "Skilling Bosses"),
    ("Mining", "Skilling Bosses"),
    ("Slayer monsters that can only be fought on task", "Slayer"),
    ("Bosses", "Solo Bosses"),
]


def map_wiki_categories(cats):
    """Map a page's wiki category tags to a dashboard category (or None)."""
    cat_set = set(cats)
    for wiki_tag, dashboard_cat in WIKI_CATEGORY_MAP:
        if wiki_tag in cat_set:
            return dashboard_cat
    return None


def fetch_wikitext(page_name):
    """Fetch raw wikitext + category tags for a wiki page via MediaWiki parse API.

    redirects=1 tells the API to auto-follow #REDIRECT pages, which is what
    most "wrong title" cases turn out to be (e.g. 'Crazy Archaeologist'
    redirects to 'Crazy archaeologist'). Categories ride along in the same
    request — no extra network calls.
    """
    url = (
        f"{API_URL}?action=parse&page={quote(page_name)}"
        f"&prop=wikitext%7Ccategories&format=json&formatversion=2&redirects=1"
    )
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(data["error"].get("info", "unknown error"))
    parse = data.get("parse") or {}
    cats = [
        c.get("category", "").replace("_", " ")
        for c in parse.get("categories", [])
        if not c.get("hidden")
    ]
    return parse.get("wikitext") or "", cats


_EXPR_RE = re.compile(r"\{\{#expr:([^{}]+?)(?:\s+round\s+\d+)?\}\}")


def _normalize_rarity(rarity):
    """Evaluate wiki {{#expr:...}} math templates inside rarity strings.

    Bosses like Alchemical Hydra express exact rates as arithmetic
    ("1/{{#expr:1000/(1999/2000*1999/2000) round 1}}") because of their kill
    mechanics — without evaluating these, all their uniques lose their rates
    and vanish from the luck engine. Only pure arithmetic is evaluated."""
    def _ev(m):
        expr = m.group(1)
        if not re.fullmatch(r"[\d\s./*()+-]+", expr):
            return m.group(0)
        try:
            return str(round(eval(expr, {"__builtins__": {}}), 1))
        except Exception:
            return m.group(0)
    return _EXPR_RE.sub(_ev, rarity)


def parse_drops_from_wikitext(text):
    """Extract item names AND rarity strings from drop-table templates.

    Looks for any of: {{DropsLine|...}}, {{Drops line|...}}, {{DropsTableLine|...}},
    {{Drop|...}}. For each match we look for a |name= parameter (optionally
    falling back to a positional first arg). Rarity strings (|rarity=) are
    kept raw (e.g. "1/512", "3 x 1/508.8", "Always") — the luck engine
    normalizes them at analysis time; this just gets them on disk.

    Returns (items list, {item: rarity_str} dict).
    """
    items = []
    rates = {}
    # Match the start of a drops-line template, then non-greedy until the next }}.
    # The character class handles single-level nested templates like {{Var|x}}
    # by allowing balanced {{...}} inside.
    pattern = re.compile(
        r"\{\{\s*(?:[Dd]rops[ _]?[Ll]ine|[Dd]ropsTableLine|[Dd]rop)\b"
        r"((?:[^{}]|\{\{[^{}]*\}\})*?)"
        r"\}\}",
        re.DOTALL,
    )
    for match in pattern.finditer(text):
        params = match.group(1)
        # Try named param first, then positional (first arg after the |).
        name_match = re.search(r"\bname\s*=\s*([^|\n\r}]+)", params)
        if not name_match:
            name_match = re.search(r"^\s*\|\s*([^=|\n\r}]+?)\s*(?:\||$)", params)
        if name_match:
            name = name_match.group(1).strip()
            # Strip wiki link formatting: [[Page|Display]] → Display, [[Page]] → Page
            name = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", name)
            name = re.sub(r"\[\[([^\]]+)\]\]", r"\1", name)
            # Strip HTML comments and stray template fragments
            name = re.sub(r"<!--.*?-->", "", name, flags=re.DOTALL).strip()
            name = re.sub(r"\s+", " ", name).strip()
            if name and not name.startswith("{") and len(name) < 80:
                items.append(name)
                rarity_match = re.search(
                    r"\brarity\s*=\s*((?:[^|{}\n\r]|\{\{[^{}]*\}\})+)", params)
                if rarity_match and name not in rates:
                    rarity = rarity_match.group(1).strip()
                    rarity = _normalize_rarity(rarity)
                    rarity = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", rarity)
                    rarity = re.sub(r"\[\[([^\]]+)\]\]", r"\1", rarity)
                    rarity = re.sub(r"\s+", " ", rarity).strip()
                    if rarity and len(rarity) < 40:
                        rates[name] = rarity
    return items, rates


def should_skip(item_name):
    n = item_name.lower().strip()
    if not n:
        return True
    for pat in SKIP_PATTERNS:
        if re.match(pat, n):
            return True
    return False


def filter_items(items):
    """Dedupe + drop generic resources."""
    seen = set()
    out = []
    for it in items:
        key = it.lower()
        if key in seen or should_skip(it):
            continue
        seen.add(key)
        out.append(it)
    return sorted(out)


def try_page(page_name):
    """Fetch a page; return (items, rates dict, wikitext length, category tags)."""
    try:
        wt, cats = fetch_wikitext(page_name)
        items, rates = parse_drops_from_wikitext(wt)
        return items, rates, len(wt), cats
    except Exception:
        return [], {}, 0, []


def fetch_all():
    boss_drops = {}
    boss_hints = {}
    boss_rates = {}
    for i, boss in enumerate(BOSSES, 1):
        page = NAME_OVERRIDES.get(boss, boss)
        print(f"[{i:>2}/{len(BOSSES)}] {boss:40s}", end=" ", flush=True)
        try:
            # Pages we'll try in order. Many raids and complex bosses keep their
            # drops on a /Drops or /Loot subpage; we accumulate items from all
            # of them so a boss with split drop tables still gets full coverage.
            pages_to_try = [page, f"{page}/Drops", f"{page}/Loot"]
            all_items = []
            all_rates = {}
            total_chars = 0
            sources = []
            for p in pages_to_try:
                items, rates, chars, cats = try_page(p)
                if p == page:
                    # Category hint comes from the MAIN page only — subpage
                    # tags are sparse and unrepresentative.
                    hint = map_wiki_categories(cats)
                    if hint:
                        boss_hints[boss] = hint
                if items:
                    all_items.extend(items)
                    sources.append(p.split("/")[-1] if "/" in p else "main")
                for _n, _r in rates.items():
                    all_rates.setdefault(_n, _r)
                total_chars += chars
                time.sleep(0.4)

            filtered = filter_items(all_items)
            # Keep build output ASCII-safe for the Windows console used by
            # the clan-package launcher; a Unicode arrow aborts the scrape
            # under legacy code pages before any table can be refreshed.
            cat_note = f" -> {boss_hints[boss]}" if boss in boss_hints else " -> (no category hint)"
            if filtered:
                boss_drops[boss] = filtered
                kept = {n: all_rates[n] for n in filtered if n in all_rates}
                if kept:
                    boss_rates[boss] = kept
                src_note = f"  [{', '.join(sources)}]" if sources else ""
                print(f"{len(filtered):>3} items ({len(kept)} rates){src_note}{cat_note}")
            else:
                print(f"(none parsed; total wikitext {total_chars} chars across {len(pages_to_try)} pages){cat_note}")
        except (URLError, HTTPError) as e:
            print(f"network error: {e}")
        except Exception as e:
            print(f"failed: {e}")
    return boss_drops, boss_hints, boss_rates


# ── Combat Achievements ────────────────────────────────────────────────
# The wiki's CA task tables are template-generated, so raw wikitext is
# empty — we fetch the RENDERED page (prop=text) and parse the rows, which
# carry a stable data-ca-task-id attribute. Column 1 = monster, column 2 =
# task name. This gives the engine an authoritative task→monster map,
# replacing filename keyword guessing.
CA_TIERS = ["Easy", "Medium", "Hard", "Elite", "Master", "Grandmaster"]


def fetch_ca_tasks():
    """Return {task_name: monster} across all Combat Achievement tiers."""
    tasks = {}
    print(f"\nFetching Combat Achievement tasks ({len(CA_TIERS)} tiers)...")
    for tier in CA_TIERS:
        page = f"Combat Achievements/{tier}"
        url = (f"{API_URL}?action=parse&page={quote(page)}"
               f"&prop=text&format=json&formatversion=2&redirects=1")
        req = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            html_text = (data.get("parse") or {}).get("text") or ""
        except Exception as e:
            print(f"  {tier}: fetch failed ({e})")
            continue
        rows = re.findall(r'<tr data-ca-task-id="\d+">(.*?)</tr>', html_text, re.DOTALL)
        n = 0
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if len(cells) < 2:
                continue
            monster = unescape(re.sub(r"<[^>]+>", "", cells[0])).strip()
            task = unescape(re.sub(r"<[^>]+>", "", cells[1])).strip()
            if task and monster:
                tasks[task] = monster
                n += 1
        print(f"  {tier}: {n} tasks")
        time.sleep(0.4)
    return tasks


def format_dict(drops):
    """Pretty-print BOSS_DROPS = {...} in the same style as osrs_dashboard.py."""
    lines = ["BOSS_DROPS = {"]
    for boss in sorted(drops):
        items = drops[boss]
        items_str = ", ".join(json.dumps(x) for x in items)
        line = f"    {json.dumps(boss)}: [{items_str}],"
        if len(line) > 110:
            wrapped = ",\n        ".join(json.dumps(x) for x in items)
            line = f"    {json.dumps(boss)}: [\n        {wrapped},\n    ],"
        lines.append(line)
    lines.append("}")
    return "\n".join(lines)


def main():
    print(f"Fetching boss drops from OSRS Wiki ({len(BOSSES)} bosses)...\n")
    drops, hints, rates = fetch_all()
    ca_tasks = fetch_ca_tasks()

    out_path = Path(__file__).parent / "boss_drops_generated.py"
    total_rates = sum(len(v) for v in rates.values())
    total_items = sum(len(v) for v in drops.values())

    # Never replace a healthy generated database with an empty or severely
    # partial network response. The thresholds are deliberately far below the
    # normal corpus (1,000+ items/rates and 600+ CA tasks) while still catching
    # blocked-network and wiki-error failure modes.
    minimums = {
        "bosses with drops": (len(drops), 20),
        "total items": (total_items, 100),
        "category hints": (len(hints), 10),
        "drop rates": (total_rates, 100),
        "combat achievement tasks": (len(ca_tasks), 100),
    }
    failed = [f"{name} {actual} < {minimum}" for name, (actual, minimum) in minimums.items()
              if actual < minimum]
    if failed:
        print("\n[ERROR] Wiki refresh was incomplete; existing generated data was preserved.")
        for detail in failed:
            print(f"  - {detail}")
        return 1

    header = (
        "# Auto-generated by update_boss_drops.py - do not edit by hand.\n"
        f"# {len(drops)} bosses, "
        f"{total_items} total items, "
        f"{len(hints)} category hints, {total_rates} drop rates, "
        f"{len(ca_tasks)} combat achievement tasks.\n\n"
    )
    hints_lines = ["", "", "# Category hints from wiki page tags (see WIKI_CATEGORY_MAP in",
                   "# update_boss_drops.py). Manual BOSS_CATEGORIES placements in the",
                   "# engine always take precedence over these.",
                   "BOSS_CATEGORY_HINTS = {"]
    for b in sorted(hints):
        hints_lines.append(f"    {json.dumps(b)}: {json.dumps(hints[b])},")
    hints_lines.append("}")
    rates_lines = ["", "", "# Raw drop-rate strings from the wiki's DropsLine templates, kept",
                   "# verbatim (e.g. \"1/512\", \"3 x 1/508.8\", \"Always\"). Groundwork for",
                   "# the luck engine — normalized at analysis time, not here.",
                   "BOSS_DROP_RATES = {"]
    for b in sorted(rates):
        rates_lines.append(f"    {json.dumps(b)}: {json.dumps(rates[b], sort_keys=True)},")
    rates_lines.append("}")
    ca_lines = ["", "", "# Combat Achievement tasks from the wiki (task → monster).",
                "# Authoritative mapping for attributing CA screenshots to bosses;",
                "# the engine falls back to name heuristics for tasks not listed.",
                "CA_TASKS = {"]
    for t in sorted(ca_tasks):
        ca_lines.append(f"    {json.dumps(t)}: {json.dumps(ca_tasks[t])},")
    ca_lines.append("}")
    temp_path = out_path.with_name(out_path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        f.write(header)
        f.write(format_dict(drops))
        f.write("\n".join(hints_lines))
        f.write("\n".join(rates_lines))
        f.write("\n".join(ca_lines))
        f.write("\n")
    temp_path.replace(out_path)

    print(f"\n[OK] Wrote {out_path}")
    print(f"  {len(drops)} bosses with drops")
    print(f"  {total_items} total items")
    print(f"  {len(hints)} category hints")
    print(f"  {total_rates} drop rates captured")
    print(f"  {len(ca_tasks)} combat achievement tasks mapped")
    print("\nThe dashboard will pick this up automatically on next refresh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
