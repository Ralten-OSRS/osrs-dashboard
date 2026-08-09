"""Resolve indirect boss-drop value without pretending it is bank value."""

import json
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from value_recipes import VALUE_RECIPES, component_names, recipe_item_names


PRICE_MAPPING_URL = "https://prices.runescape.wiki/api/v1/osrs/mapping"
PRICE_LATEST_URL = "https://prices.runescape.wiki/api/v1/osrs/latest"
PRICE_CACHE_HOURS = 24
USER_AGENT = "OSRS-Dashboard/3.0 (personal clan tool)"


def normalize_item(value):
    return " ".join(str(value or "").strip().lower().split())


def _read_json(path, default):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (OSError, ValueError, TypeError):
        return default


def _write_json_atomic(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def _fetch_json(url):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _cache_is_fresh(payload):
    try:
        updated = datetime.fromisoformat(payload.get("updated_at", ""))
    except (TypeError, ValueError):
        return False
    return datetime.now() - updated < timedelta(hours=PRICE_CACHE_HOURS)


def load_price_book(cache_file, recipes=None):
    """Return a small name->price book, refreshing it at most once per day."""
    cached = _read_json(cache_file, {})
    wanted_names = recipe_item_names(recipes)
    if isinstance(cached, dict) and _cache_is_fresh(cached):
        cached_prices = cached.get("prices", {})
        if all(name in cached_prices for name in wanted_names):
            return cached_prices, cached.get("updated_at"), False

    try:
        mapping = _fetch_json(PRICE_MAPPING_URL)
        latest = (_fetch_json(PRICE_LATEST_URL) or {}).get("data", {})
        wanted = {normalize_item(name): name for name in wanted_names}
        prices = {}
        for item in mapping if isinstance(mapping, list) else []:
            canonical = wanted.get(normalize_item(item.get("name")))
            if not canonical:
                continue
            quote = latest.get(str(item.get("id")), {})
            high = quote.get("high")
            low = quote.get("low")
            if isinstance(high, (int, float)) and isinstance(low, (int, float)):
                price = int(round((high + low) / 2))
            elif isinstance(high, (int, float)):
                price = int(high)
            elif isinstance(low, (int, float)):
                price = int(low)
            else:
                continue
            if price >= 0:
                prices[canonical] = price
        if prices:
            payload = {
                "version": 1,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "prices": prices,
            }
            _write_json_atomic(cache_file, payload)
            return prices, payload["updated_at"], False
    except (OSError, ValueError, TypeError, HTTPError, URLError):
        pass

    if isinstance(cached, dict):
        return cached.get("prices", {}), cached.get("updated_at"), True
    return {}, None, True


def dedupe_acquisitions(events, seconds=3):
    """Merge multiple RuneLite screenshots that prove the same acquisition."""
    ordered = sorted(
        (event for event in events if event.get("timestamp") and event.get("item")),
        key=lambda event: event["timestamp"],
    )
    merged = []
    last_by_item = {}
    for event in ordered:
        item_key = normalize_item(event["item"])
        previous_index = last_by_item.get(item_key)
        if previous_index is not None:
            previous = merged[previous_index]
            delta = abs((event["timestamp"] - previous["timestamp"]).total_seconds())
            if delta <= seconds:
                previous["value"] = max(int(previous.get("value", 0)), int(event.get("value", 0)))
                previous["qty"] = max(int(previous.get("qty", 1)), int(event.get("qty", 1)))
                previous["sources"] = sorted(set(previous.get("sources", [])) | {event.get("source", "")})
                if event.get("rel_path") and not previous.get("rel_path"):
                    previous["rel_path"] = event["rel_path"]
                continue
        copy = dict(event)
        copy["item_key"] = item_key
        copy["qty"] = max(1, int(copy.get("qty", 1)))
        copy["value"] = max(0, int(copy.get("value", 0)))
        copy["sources"] = [copy.get("source", "")]
        merged.append(copy)
        last_by_item[item_key] = len(merged) - 1
    return merged


def _recipe_cost(recipe, price_book):
    output_price = price_book.get(recipe["output"])
    if not isinstance(output_price, (int, float)):
        return None, None
    input_cost = 0
    for item, quantity in recipe.get("market_inputs", {}).items():
        price = price_book.get(item)
        if not isinstance(price, (int, float)):
            return int(output_price), None
        input_cost += int(price) * int(quantity)
    return int(output_price), input_cost


def resolve_economic_value(events, price_cache_file, ledger_file, player_name=None,
                           audit_item_keys=None, recipes=None):
    """Resolve completed recipes, current latent progress, and frozen values."""
    recipes = list(recipes) if recipes is not None else list(VALUE_RECIPES)
    acquisitions = dedupe_acquisitions(events)
    prices, prices_updated_at, stale_prices = load_price_book(price_cache_file, recipes)
    existing_payload = _read_json(ledger_file, {})
    existing_events = {
        event.get("id"): event
        for event in existing_payload.get("events", [])
        if isinstance(event, dict) and event.get("id")
    } if isinstance(existing_payload, dict) else {}

    recipe_by_component = defaultdict(list)
    queues = defaultdict(deque)
    for recipe in recipes:
        for component in recipe.get("components", {}):
            recipe_by_component[normalize_item(component)].append(recipe)

    resolved = []
    completion_counts = defaultdict(int)

    def can_complete(recipe):
        return all(
            len(queues[normalize_item(item)]) >= int(quantity)
            for item, quantity in recipe.get("components", {}).items()
        )

    def _is_attested(event):
        sources = event.get("sources") or [event.get("source", "")]
        return all(source == "attested" for source in sources)

    def _evidence_for(consumed):
        """Return newest-first, screenshot-backed proof for an assembly."""
        evidence = []
        seen_paths = set()
        for event in sorted(consumed, key=lambda item: item["timestamp"], reverse=True):
            rel_path = str(event.get("rel_path") or "").strip()
            if not rel_path or rel_path in seen_paths:
                continue
            seen_paths.add(rel_path)
            evidence.append({
                "item": event.get("item", "Component"),
                "rel_path": rel_path,
                "timestamp": event["timestamp"].isoformat(),
            })
        return evidence

    def complete(recipe):
        consumed = []
        for item, quantity in recipe.get("components", {}).items():
            queue = queues[normalize_item(item)]
            for _ in range(int(quantity)):
                consumed.append(queue.popleft())
        # Date the completion by real screenshot evidence whenever any exists.
        # Attested config overrides carry synthetic timestamps; letting one of
        # those set completed_at would change the frozen event's id (and thus
        # re-price it) whenever the attested timestamp changed.
        evidenced = [e["timestamp"] for e in consumed if not _is_attested(e)]
        completed_at = max(evidenced or [event["timestamp"] for event in consumed])
        completion_counts[recipe["id"]] += 1
        event_id = f"{recipe['id']}:{completed_at.isoformat()}:{completion_counts[recipe['id']]}"
        frozen = existing_events.get(event_id)
        if frozen:
            # Values and dates stay frozen, but older ledger rows can be safely
            # enriched with the screenshot evidence now available in the scan.
            enriched = dict(frozen)
            evidence = _evidence_for(consumed)
            if evidence:
                enriched["evidence"] = evidence
            resolved.append(enriched)
            return

        output_price, market_input_cost = _recipe_cost(recipe, prices)
        if output_price is None or market_input_cost is None:
            # Put evidence back so the completion can resolve on a later refresh.
            for event in consumed:
                queues[event["item_key"]].appendleft(event)
            completion_counts[recipe["id"]] -= 1
            return
        credited_components = sum(int(event.get("value", 0)) for event in consumed)
        unlocked_value = max(0, output_price - market_input_cost - credited_components)
        resolved.append({
            "id": event_id,
            "recipe_id": recipe["id"],
            "label": recipe["label"],
            "output": recipe["output"],
            "completed_at": completed_at.isoformat(),
            "value": unlocked_value,
            "output_price": output_price,
            "market_input_cost": market_input_cost,
            "component_credit": credited_components,
            "backfilled": completed_at.date() < datetime.now().date(),
            "evidence": _evidence_for(consumed),
        })

    for acquisition in acquisitions:
        item_key = acquisition["item_key"]
        if item_key not in recipe_by_component:
            continue
        for _ in range(acquisition["qty"]):
            single = dict(acquisition)
            single["qty"] = 1
            single["value"] = int(acquisition.get("value", 0)) // acquisition["qty"]
            queues[item_key].append(single)
        for recipe in recipe_by_component[item_key]:
            while can_complete(recipe):
                before = len(resolved)
                complete(recipe)
                if len(resolved) == before:
                    break

    pending = []
    latent_total = 0
    for recipe in recipes:
        required_total = sum(int(quantity) for quantity in recipe.get("components", {}).values())
        have_total = 0
        duplicates = 0
        detail = []
        for item, quantity in recipe.get("components", {}).items():
            queue = queues[normalize_item(item)]
            have = len(queue)
            need = int(quantity)
            satisfied = min(have, need)
            have_total += satisfied
            duplicates += max(0, have - need)
            # Acquisition timestamps of the satisfied copies (queue order is
            # chronological) so the chart can reconstruct pending value over time.
            acquired = sorted(
                event["timestamp"].isoformat()
                for event in list(queue)[:satisfied]
            )
            evidence = []
            for event in list(queue)[:satisfied]:
                evidence.append({
                    "item": event.get("item", item),
                    "timestamp": event["timestamp"].isoformat(),
                    "rel_path": str(event.get("rel_path") or ""),
                    "attested": _is_attested(event),
                })
            detail.append({"item": item, "have": have, "need": need,
                           "acquired": acquired, "evidence": evidence})
        if have_total <= 0:
            continue
        output_price, market_input_cost = _recipe_cost(recipe, prices)
        progress = have_total / required_total if required_total else 0
        estimated = None
        net_output = None
        if output_price is not None and market_input_cost is not None:
            net_output = max(0, output_price - market_input_cost)
            estimated = int(round(net_output * progress))
            latent_total += estimated
        pending.append({
            "recipe_id": recipe["id"],
            "label": recipe["label"],
            "output": recipe["output"],
            "have": have_total,
            "need": required_total,
            "duplicates": duplicates,
            "estimated_value": estimated,
            # This is a current-price preview of the value the completed
            # transformation would credit. It is intentionally separate from
            # the partial pending estimate and never enters realized GP.
            "projected_completed_value": net_output,
            "net_output": net_output,
            "detail": detail,
        })

    resolved.sort(key=lambda event: event.get("completed_at", ""))
    ledger_payload = {
        "version": 1,
        "player": player_name or (existing_payload.get("player") if isinstance(existing_payload, dict) else None),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "events": resolved,
    }
    existing_player = existing_payload.get("player") if isinstance(existing_payload, dict) else None
    if (ledger_payload["events"] != list(existing_events.values())
            or ledger_payload["player"] != existing_player):
        _write_json_atomic(ledger_file, ledger_payload)

    registered = {normalize_item(item) for item in component_names(recipes)}
    audit_keys = {normalize_item(item) for item in (audit_item_keys or [])}
    observed_unregistered = sorted({
        event["item"]
        for event in acquisitions
        if event.get("source") in {"untradeable", "collection"}
        and event["item_key"] not in registered
        and event["item_key"] in audit_keys
    })

    return {
        "events": resolved,
        "realized_total": sum(int(event.get("value", 0)) for event in resolved),
        "pending": pending,
        "latent_total": latent_total,
        "prices_updated_at": prices_updated_at,
        "prices_stale": stale_prices,
        "unregistered_observed": observed_unregistered,
    }
