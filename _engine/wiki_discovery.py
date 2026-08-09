"""Safe, account-local discovery of new boss references and value recipes.

This is intentionally a data compiler, not a free-form guesser.  It only
enrols a recipe when the OSRS Wiki renders one clear Products row: a tradeable
output with a fixed set of materials whose tradeability can all be verified.
Everything else remains a visible candidate for a later retry instead of
silently changing the GP log.
"""

import html
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


WIKI_API_URL = "https://oldschool.runescape.wiki/api.php"
USER_AGENT = "OSRS-Dashboard/3.1 (personal clan tool)"
MAX_ITEM_CHECKS_PER_REFRESH = 6
RECHECK_DAYS = 30
CATALOG_VERSION = 2
PERMANENT_ITEM_STATUSES = {"assembly", "direct_unpriced", "excluded", "non_assembly"}


def normalize_item(value):
    return " ".join(str(value or "").strip().lower().split())


def _read_json(path, default):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def _write_json_atomic(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _fetch_wiki_page(page_name):
    url = (
        f"{WIKI_API_URL}?action=parse&page={quote(page_name)}"
        "&prop=wikitext%7Ctext%7Ccategories&format=json&formatversion=2&redirects=1"
    )
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(payload["error"].get("info", "unknown wiki error"))
    parsed = payload.get("parse") or {}
    return {
        "wikitext": parsed.get("wikitext") or "",
        "html": parsed.get("text") or "",
        "categories": [
            item.get("category", "").replace("_", " ")
            for item in parsed.get("categories", [])
            if not item.get("hidden")
        ],
    }


def _strip_html(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(html.unescape(value).split())


def _wiki_links(cell_html):
    links = []
    for match in re.finditer(r"<a([^>]*)href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", cell_html, re.I | re.S):
        href = html.unescape(match.group(2))
        label = _strip_html(match.group(3))
        if not label:
            title = re.search(r"\btitle=[\"']([^\"']+)[\"']", match.group(0), re.I)
            label = html.unescape(title.group(1)) if title else ""
        if not label or "/w/" not in href:
            continue
        target = href.split("/w/", 1)[1].split("#", 1)[0]
        if target.lower().startswith(("file:", "template:", "category:")):
            continue
        links.append((html.unescape(target.replace("_", " ")), label, match.start()))
    return links


def _quantity_before(cell_html, link_start):
    prefix = _strip_html(cell_html[:link_start])
    numbers = re.findall(r"\d[\d,]*", prefix)
    return int(numbers[-1].replace(",", "")) if numbers else 1


def _tradeable_from_html(page_html):
    match = re.search(
        r"<th[^>]*>.*?Tradeable.*?</th>\s*<td[^>]*>(.*?)</td>",
        page_html or "", re.I | re.S,
    )
    if not match:
        return None
    value = _strip_html(match.group(1)).lower()
    if value.startswith("yes"):
        return True
    if value.startswith("no"):
        return False
    return None


def _products_from_html(page_html):
    """Return rendered Product rows as {output, materials} dictionaries."""
    heading = re.search(r"id=[\"']Products[\"']", page_html or "", re.I)
    if not heading:
        return []
    section = page_html[heading.end():]
    next_heading = re.search(r"<h2\b", section, re.I)
    if next_heading:
        section = section[:next_heading.start()]
    table_match = re.search(r"<table\b[^>]*>(.*?)</table>", section, re.I | re.S)
    if not table_match:
        return []
    table = table_match.group(1)
    header_match = re.search(r"<tr\b[^>]*>(.*?)</tr>", table, re.I | re.S)
    headers = []
    if header_match:
        for match in re.finditer(r"<th\b([^>]*)>(.*?)</th>", header_match.group(1), re.I | re.S):
            colspan = re.search(r"\bcolspan=[\"']?(\d+)", match.group(1), re.I)
            headers.extend([_strip_html(match.group(2)).lower()] * int(colspan.group(1) if colspan else 1))
    material_index = next((index for index, value in enumerate(headers) if "material" in value), None)
    rows = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.I | re.S)
        if len(cells) < 2:
            continue
        output_links = _wiki_links(cells[0]) + _wiki_links(cells[1] if len(cells) > 1 else "")
        if not output_links:
            continue
        output = output_links[0][1]
        materials_cell = cells[material_index] if material_index is not None and material_index < len(cells) else cells[-1]
        materials = {}
        material_blocks = re.findall(r"<li\b[^>]*>(.*?)</li>", materials_cell, re.I | re.S)
        for block in material_blocks or [materials_cell]:
            links = _wiki_links(block)
            if not links:
                continue
            _target, label, start = links[-1]
            quantity = _quantity_before(block, start)
            materials[label] = materials.get(label, 0) + quantity
        if output and materials:
            rows.append({"output": output, "materials": materials})
    return rows


def _parse_drops(wikitext):
    """Small, targeted equivalent of the full drop-table parser."""
    generic = ("bones", "rune", "ore", "raw ", "cooked ", "seed", "log", "herb")
    pattern = re.compile(
        r"\{\{\s*(?:[Dd]rops[ _]?[Ll]ine|[Dd]ropsTableLine|[Dd]rop)\b"
        r"((?:[^{}]|\{\{[^{}]*\}\})*?)\}\}", re.S,
    )
    drops, rates = [], {}
    for match in pattern.finditer(wikitext or ""):
        params = match.group(1)
        name_match = re.search(r"\bname\s*=\s*([^|\n\r}]+)", params)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        name = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", name)
        name = re.sub(r"\[\[([^\]]+)\]\]", r"\1", name)
        name = " ".join(name.split())
        lowered = name.lower()
        if not name or name.startswith("{") or any(token in lowered for token in generic):
            continue
        if name not in drops:
            drops.append(name)
        rarity = re.search(r"\brarity\s*=\s*([^|\n\r}]+)", params)
        if rarity and name not in rates:
            rates[name] = " ".join(rarity.group(1).split())
    return sorted(drops), rates


def _catalog(payload):
    if not isinstance(payload, dict):
        payload = {}
    return {
        "version": max(int(payload.get("version") or 1), CATALOG_VERSION),
        "updated_at": payload.get("updated_at"),
        "bosses": payload.get("bosses") if isinstance(payload.get("bosses"), dict) else {},
        "recipes": payload.get("recipes") if isinstance(payload.get("recipes"), list) else [],
        "item_checks": payload.get("item_checks") if isinstance(payload.get("item_checks"), dict) else {},
    }


def _recently_checked(record, now):
    if not isinstance(record, dict) or not record.get("classification"):
        return False
    if record.get("classification") in PERMANENT_ITEM_STATUSES:
        return True
    if record.get("status") == "failed":
        return False
    try:
        checked = datetime.fromisoformat(record.get("checked_at", ""))
    except (TypeError, ValueError):
        return False
    return now - checked < timedelta(days=RECHECK_DAYS)


def _recipe_id(output):
    slug = re.sub(r"[^a-z0-9]+", "_", normalize_item(output)).strip("_")
    return f"wiki_{slug}"[:72]


def _discover_recipe(seed_item, item_cache, fetch_page=None):
    """Return one safe recipe or a status explaining why no rule was enrolled."""
    seed_key = normalize_item(seed_item)

    def page_for(item):
        key = normalize_item(item)
        if key not in item_cache:
            payload = (fetch_page or _fetch_wiki_page)(item)
            item_cache[key] = {
                "display": item,
                "tradeable": _tradeable_from_html(payload["html"]),
                "html": payload["html"],
                "categories": payload.get("categories") or [],
                "wikitext": payload.get("wikitext") or "",
            }
        return item_cache[key]

    seed_page = page_for(seed_item)
    candidates = []
    for product in _products_from_html(seed_page["html"]):
        material_keys = {normalize_item(item) for item in product["materials"]}
        if seed_key not in material_keys:
            continue
        output_page = page_for(product["output"])
        if output_page["tradeable"] is not True:
            continue
        components, market_inputs = {}, {}
        unresolved = False
        for material, quantity in product["materials"].items():
            material_page = page_for(material)
            if material_page["tradeable"] is False:
                components[material] = int(quantity)
            elif material_page["tradeable"] is True:
                market_inputs[material] = int(quantity)
            else:
                unresolved = True
                break
        if not unresolved and any(normalize_item(item) == seed_key for item in components):
            candidates.append({
                "id": _recipe_id(product["output"]),
                "label": product["output"],
                "output": product["output"],
                "components": components,
                "market_inputs": market_inputs,
                "source": {"kind": "wiki_products", "seed_item": seed_item},
            })

    signatures = {
        (recipe["output"].lower(), tuple(sorted((normalize_item(k), v) for k, v in recipe["components"].items())),
         tuple(sorted((normalize_item(k), v) for k, v in recipe["market_inputs"].items())))
        for recipe in candidates
    }
    if len(signatures) != 1:
        return None, "ambiguous" if signatures else "no_clear_recipe", seed_page
    return candidates[0], "enrolled", seed_page


def _source_classification(seed_page):
    """Classify a successful Wiki lookup without assigning any GP value."""
    source_text = " ".join(seed_page.get("categories") or []) + " " + seed_page.get("wikitext", "")
    source_text = source_text.lower()
    if "treasure trails" in source_text or "clue scroll" in source_text:
        return "excluded", "Treasure Trails reward"
    if re.search(r"\bpet(s)?\b", source_text):
        return "excluded", "pet or cosmetic unlock"
    if seed_page.get("tradeable") is True:
        return "direct_unpriced", "tradeable direct reward without dated value evidence"
    return "non_assembly", "no verified assembly path"


def classify_item(seed_item, item_cache=None, fetch_page=None):
    """Return a durable, conservative classification for one observed item.

    A live Wiki failure remains retryable.  A successfully parsed page is
    either an exact assembly, a direct reward, or permanently non-assembly.
    """
    item_cache = item_cache if item_cache is not None else {}
    recipe, status, seed_page = _discover_recipe(seed_item, item_cache, fetch_page)
    if recipe:
        return recipe, "assembly", "verified fixed assembly"
    if status == "ambiguous":
        return None, "ambiguous", "wiki shows multiple possible recipes"
    classification, reason = _source_classification(seed_page)
    return None, classification, reason


def refresh_catalog(new_bosses, acquisitions, existing_recipes, catalog_file, now=None,
                    force_bosses=None, progress=None):
    """Refresh account-local discovery data and return dynamic recipes/references.

    `new_bosses` are fetched once and then skipped on every later run, which is
    what keeps an ordinary refresh cheap. `force_bosses` overrides that skip so
    a boss already in the catalog gets re-read from the wiki — this is the only
    way a user picks up items added to a boss's drop table after their copy was
    built. It costs one wiki request per boss, so it must stay opt-in.

    `progress(position, total, boss)` is called before each fetch. Ctrl+C stops
    the sweep and keeps whatever was already collected.
    """
    now = now or datetime.now()
    catalog = _catalog(_read_json(catalog_file, {}))
    changed = False
    discovered_bosses = []
    refreshed_bosses = []
    interrupted = False

    known = {normalize_item(name) for name in catalog["bosses"]}
    forced = {normalize_item(name) for name in (force_bosses or []) if normalize_item(name)}

    queue, queued = [], set()
    for boss in list(new_bosses or []) + list(force_bosses or []):
        key = normalize_item(boss)
        if not key or key in queued:
            continue
        if key in known and key not in forced:
            continue
        queue.append(boss)
        queued.add(key)

    try:
        for position, boss in enumerate(queue, 1):
            key = normalize_item(boss)
            if progress:
                progress(position, len(queue), boss)
            try:
                page = _fetch_wiki_page(boss)
                drops, rates = _parse_drops(page["wikitext"])
            except (OSError, ValueError, HTTPError, URLError, RuntimeError):
                continue
            if drops:
                # Reuse the existing key when this boss is already catalogued,
                # so a casing difference can't create a duplicate entry.
                target = next(
                    (name for name in catalog["bosses"] if normalize_item(name) == key),
                    boss,
                )
                was_known = key in known
                catalog["bosses"][target] = {
                    "drops": drops,
                    "rates": rates,
                    "discovered_at": now.isoformat(timespec="seconds"),
                }
                (refreshed_bosses if was_known else discovered_bosses).append(target)
                known.add(key)
                changed = True
            time.sleep(0.25)
    except KeyboardInterrupt:
        # Partial progress is still worth keeping; it is written out below.
        interrupted = True

    registered = {
        normalize_item(component)
        for recipe in list(existing_recipes or []) + list(catalog["recipes"])
        for component in recipe.get("components", {})
    }
    latest_items = {}
    for event in acquisitions or []:
        if event.get("source") not in {"untradeable", "collection"}:
            continue
        item = event.get("item")
        timestamp = event.get("timestamp")
        if not item or not timestamp or normalize_item(item) in registered:
            continue
        key = normalize_item(item)
        if key not in latest_items or timestamp > latest_items[key]["timestamp"]:
            latest_items[key] = {"item": item, "timestamp": timestamp}

    item_cache = {}
    checked, enrolled, ambiguous = 0, [], []
    for candidate in sorted(latest_items.values(), key=lambda value: value["timestamp"], reverse=True):
        if checked >= MAX_ITEM_CHECKS_PER_REFRESH:
            break
        key = normalize_item(candidate["item"])
        if _recently_checked(catalog["item_checks"].get(key), now):
            continue
        checked += 1
        try:
            recipe, classification, reason = classify_item(candidate["item"], item_cache)
        except (OSError, ValueError, HTTPError, URLError, RuntimeError):
            recipe, classification, reason = None, "failed", "wiki lookup failed, will retry"
        status = "enrolled" if recipe else classification
        record = {
            "item": candidate["item"],
            "status": status,
            "classification": classification,
            "reason": reason,
            "checked_at": now.isoformat(timespec="seconds"),
        }
        catalog["item_checks"][key] = record
        changed = True
        if recipe:
            known_outputs = {normalize_item(value.get("output")) for value in catalog["recipes"]}
            if normalize_item(recipe["output"]) not in known_outputs:
                recipe["source"]["discovered_at"] = now.isoformat(timespec="seconds")
                catalog["recipes"].append(recipe)
                enrolled.append(recipe["label"])
                registered.update(normalize_item(item) for item in recipe["components"])
        elif classification == "ambiguous":
            ambiguous.append(candidate["item"])
        time.sleep(0.25)

    if changed:
        catalog["updated_at"] = now.isoformat(timespec="seconds")
        _write_json_atomic(catalog_file, catalog)
    return {
        "recipes": catalog["recipes"],
        "bosses": catalog["bosses"],
        "new_bosses": discovered_bosses,
        "refreshed_bosses": refreshed_bosses,
        "interrupted": interrupted,
        "new_recipes": enrolled,
        "ambiguous_items": ambiguous,
        "checked_items": checked,
        "queued_items": max(0, len(latest_items) - checked),
        "item_checks": catalog["item_checks"],
    }
