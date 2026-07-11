"""
Piac-stratégia (MarketStrategy) — pluggable PIAC-OSZTÁLYOZÓK könnyű registere.

A kereskedő-stratégiákkal szimmetrikus, DE instrumentumonként EGY (a piac-képnek
egy koherens forrása kell). Config: `pairs.<sym>.market_strategy` = a név vagy
hiányzik/`""`/`"none"` = NINCS. A `market_viz` (bool) dönti, hogy a chart-sávon
megjelenjen-e.

Jelenleg egyetlen osztályozó: `regime` (core.regime, ADX/DI/ATR → 8 kategória).
Új piac-stratégia = egy név ide + a classify/legend ág. A dashboard és a viz
GENERIKUSAN kezeli (kód + címke + szín), így az osztályozó cserélhető.
"""

from __future__ import annotations

from core import regime

# A regisztrált piac-osztályozók (a GUI ebből kínál választékot).
_REGISTERED = ("regime",)
NAME_HU = {"regime": "Regime (ADX/DI/ATR)"}

# Megjelenítés kategóriánként: (rövid címke, szemantikus szín-név a dashboardhoz).
_DISPLAY = {
    regime.CLEAN_BULL:    ("Sz.Bika",    "green"),
    regime.CLEAN_BEAR:    ("Sz.Medve",   "red"),
    regime.VOLATILE_BULL: ("Id.Bika",    "yellow"),
    regime.VOLATILE_BEAR: ("Id.Medve",   "yellow"),
    regime.RANGING:       ("Oldalazás",  "white"),
    regime.DEAD:          ("Érdektelen", "muted"),
    regime.UNCERTAIN:     ("Bizonyt.",   "red"),
    regime.TRANSITION:    ("Átmenet",    "muted"),
    regime.UNCATEGORIZED: ("—",          "muted"),
}


def registered_market_names() -> list[str]:
    return list(_REGISTERED)


def market_name_of(cfg_pair: dict) -> "str | None":
    """A pár kiválasztott piac-stratégiája (`pairs.<sym>.market_strategy`), vagy
    None, ha nincs / ismeretlen."""
    name = (cfg_pair or {}).get("market_strategy")
    if not name or name in ("none", "Nincs"):
        return None
    return name if name in _REGISTERED else None


def classify_series(name: str, df15):
    """Per-gyertya kategória-sorozat az adott osztályozóval (None, ha ismeretlen)."""
    if name == "regime":
        return regime.classify(df15)
    return None


def latest_category(name: str, df15) -> "str | None":
    """A LEGUTOLSÓ gyertya piac-kategóriája (a dashboard-kijelzéshez)."""
    s = classify_series(name, df15)
    if s is None or len(s) == 0:
        return None
    return s.iloc[-1]


def code(name: str, category: str) -> int:
    """Kategória → egész kód (a viz STATE-sorához / a Bands szín-indexéhez)."""
    if name == "regime":
        return regime.code(category)
    return 0


def display(category: str) -> tuple:
    """Kategória → (rövid címke, szemantikus szín-név) a dashboard-cellához."""
    return _DISPLAY.get(category, ("—", "muted"))
