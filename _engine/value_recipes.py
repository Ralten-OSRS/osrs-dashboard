"""Stable economic transformation rules for the OSRS Dashboard.

Drop tables tell the dashboard what a boss can award. These recipes describe
what otherwise valueless, untradeable awards can become. Market inputs are not
required in screenshot history; their live cost is subtracted from the output
so only value unlocked by the boss drop is credited.
"""


VALUE_RECIPES = [
    {
        "id": "soulreaper_axe",
        "label": "Soulreaper axe",
        "output": "Soulreaper axe",
        "components": {
            "Eye of the duke": 1,
            "Leviathan's lure": 1,
            "Siren's staff": 1,
            "Executioner's axe head": 1,
        },
        "market_inputs": {"Blood rune": 2000},
    },
    {
        "id": "abyssal_bludgeon",
        "label": "Abyssal bludgeon",
        "output": "Abyssal bludgeon",
        "components": {
            "Bludgeon axon": 1,
            "Bludgeon claw": 1,
            "Bludgeon spine": 1,
        },
        "market_inputs": {},
    },
    {
        "id": "brimstone_ring",
        "label": "Brimstone ring",
        "output": "Brimstone ring",
        "components": {
            "Hydra's eye": 1,
            "Hydra's fang": 1,
            "Hydra's heart": 1,
        },
        "market_inputs": {},
    },
    {
        "id": "noxious_halberd",
        "label": "Noxious halberd",
        "output": "Noxious halberd",
        "components": {
            "Noxious blade": 1,
            "Noxious point": 1,
            "Noxious pommel": 1,
        },
        "market_inputs": {},
    },
    {
        "id": "ultor_ring",
        "label": "Ultor ring",
        "output": "Ultor ring",
        "components": {"Ultor vestige": 1},
        "market_inputs": {"Chromium ingot": 3, "Berserker ring": 1},
    },
    {
        "id": "bellator_ring",
        "label": "Bellator ring",
        "output": "Bellator ring",
        "components": {"Bellator vestige": 1},
        "market_inputs": {"Chromium ingot": 3, "Warrior ring": 1},
    },
    {
        "id": "magus_ring",
        "label": "Magus ring",
        "output": "Magus ring",
        "components": {"Magus vestige": 1},
        "market_inputs": {"Chromium ingot": 3, "Seers ring": 1},
    },
    {
        "id": "venator_ring",
        "label": "Venator ring",
        "output": "Venator ring",
        "components": {"Venator vestige": 1},
        "market_inputs": {"Chromium ingot": 3, "Archers ring": 1},
    },
    {
        "id": "etched_elder_venator_fang",
        "label": "Etched elder venator fang",
        "output": "Etched elder venator fang",
        "components": {"Elder venator fang": 1},
        "market_inputs": {},
    },
    {
        "id": "etched_araxyte_fang",
        "label": "Etched araxyte fang",
        "output": "Etched araxyte fang",
        "components": {"Araxyte fang": 1},
        "market_inputs": {},
    },
]


def recipe_item_names(recipes=None):
    """Return every market-priced output/input referenced by the registry."""
    names = set()
    for recipe in (recipes if recipes is not None else VALUE_RECIPES):
        names.add(recipe["output"])
        names.update(recipe.get("market_inputs", {}))
    return names


def component_names(recipes=None):
    """Return normalized component display names used by registered recipes."""
    return {
        item
        for recipe in (recipes if recipes is not None else VALUE_RECIPES)
        for item in recipe.get("components", {})
    }
