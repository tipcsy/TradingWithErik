"""
Stratégia réteg.

A dashboard "váza" (megjelenítés, optimalizálás, futtatás, MT5 kapcsolat,
portfólió backteszt) stratégia-független. A konkrét stratégia ezen a
csomagon keresztül csatlakozik: deklarálja a saját oszlopait, kiszámítja a
megjelenítendő értékeket, kezeli a jelzéslogikát és megadja az optimalizálandó
paramétertartományt.

Új stratégia = EGY új modul a `strategy/` csomagban, ami a `Strategy` interfészt
implementálja. A regisztráció AUTOMATIKUS (a modul felderítése) — ezt a fájlt (a
vázat) NEM kell szerkeszteni. A `get_strategy()` adja vissza az aktívat (config-vezérelt).
"""

import importlib
import logging
import pkgutil

from strategy.base import (
    Strategy, Column, CountdownColumn, StrategyColumn, MarkerColumn,
    MarketData, Cell, Timeframe,
)

log = logging.getLogger(__name__)

# A strategy/ csomag NEM-stratégia segédmoduljai — a felderítés kihagyja őket. (Nem
# kötelező: a felderítés amúgy is csak a Strategy-alosztályokat regisztrálja; ez a
# lista pusztán az importjukat spórolja meg.)
_SKIP_MODULES = {"base", "settings", "visual", "ml_features", "ml_train"}

_REGISTRY: "dict[str, type] | None" = None   # név → Strategy-osztály (lazán felderítve)


def _registry() -> "dict[str, type]":
    """A `strategy/` csomag AUTOMATIKUS felderítése: végignézi a moduljait, és a
    talált `Strategy`-alosztályokat a `.name`-jük alapján regisztrálja. Egyszer fut
    (cache-elve). ÍGY egy új stratégia = EGY új modul a strategy/-ben — a registry-t
    (ezt a fájlt) nem kell módosítani. Determinisztikus (ábécé) sorrend; a be nem
    tölthető modult átugorja (figyelmeztetéssel)."""
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    import strategy as _pkg
    reg: dict = {}
    for _mi in pkgutil.iter_modules(_pkg.__path__):
        nm = _mi.name
        if nm.startswith("_") or nm in _SKIP_MODULES:
            continue
        try:
            mod = importlib.import_module(f"strategy.{nm}")
        except Exception as e:
            log.warning("Stratégia-modul nem tölthető be: strategy.%s (%s)", nm, e)
            continue
        for obj in vars(mod).values():
            if (isinstance(obj, type) and issubclass(obj, Strategy)
                    and obj is not Strategy):
                sn = getattr(obj, "name", None)
                if sn and sn not in reg:
                    reg[sn] = obj
    _REGISTRY = dict(sorted(reg.items()))
    return _REGISTRY


def registered_strategy_names() -> list[str]:
    """A felderített (ismert) stratégiák nevei, ábécé sorrendben. A MEGJELENÍTÉSI
    sorrendet a config `available_strategies` (whitelist) / az elsődleges stratégia
    felülírja — lásd `available_strategy_names`."""
    return list(_registry().keys())


# A stratégia-példányok gyorsítótára (stratégia-nevenként EGY példány; a példány
# állapotmentes az élő jelzésállapoton kívül, amit páronként külön tartunk).
_INSTANCES: dict[str, Strategy] = {}


def get_strategy_by_name(name: str) -> Strategy:
    """Stratégia-példány NÉV alapján (a felderített registry-ből, cache-elve).
    Új stratégia = egy új modul a strategy/-ben (Strategy-alosztály) — itt nincs mit írni."""
    if name not in _INSTANCES:
        cls = _registry().get(name)
        if cls is None:
            raise ValueError(f"Ismeretlen stratégia: {name!r}")
        _INSTANCES[name] = cls()
    return _INSTANCES[name]


def available_strategy_names(cfg: dict) -> list[str]:
    """A programban ELÉRHETŐVÉ tett stratégiák — a regisztráltak config-vezérelt
    whitelistje (config.json: `available_strategies`). Ez határozza meg, MIT kínál
    a per-pár választó és MIBŐL képződnek a dashboard-oszlopok. Hiány/üres/csupa-
    érvénytelen → az ÖSSZES regisztrált (visszafelé kompatibilis). A config
    sorrendjét megtartja, csak érvényes+egyedi neveket ad vissza."""
    reg = registered_strategy_names()
    want = cfg.get("available_strategies")
    if not want:
        # Nincs whitelist → az ÖSSZES felderített, de a config elsődleges stratégiája
        # ELÖL (a megszokott oszlopsorrend megőrzése; a többi ábécében). A primary-t
        # NYERSEN olvassuk (nem default_strategy_name-en át), hogy ne legyen ciklus.
        primary = (cfg.get("strategy", {}) or {}).get("name", "")
        if primary in reg:
            return [primary] + [n for n in reg if n != primary]
        return reg
    seen, res = set(), []
    for n in want:
        if n in reg and n not in seen:
            seen.add(n)
            res.append(n)
    return res or reg


def default_strategy_name(cfg: dict) -> str:
    """A config elsődleges/alapértelmezett stratégiája (config.json strategy.name):
    az a stratégia, amit egy pár akkor használ, ha nincs saját `strategies` listája.
    Ha az érték nincs az elérhetők (available_strategies) között, az első elérhetőre
    esik vissza — így egy kikapcsolt stratégia nem marad ‚láthatatlan alapértelmezett'."""
    name = (cfg.get("strategy", {}) or {}).get("name", "wpr_sma")
    avail = available_strategy_names(cfg)
    return name if name in avail else (avail[0] if avail else name)


def get_strategy(cfg: dict) -> Strategy:
    """Az ELSŐDLEGES stratégia példánya a config alapján (visszafelé kompatibilis).

    config.json:  "strategy": { "name": "wpr_sma" }   (alapértelmezett: wpr_sma)
    """
    return get_strategy_by_name(default_strategy_name(cfg))


def enabled_strategy_names(cfg: dict, symbol: str) -> list[str]:
    """Az adott instrumentumon ENGEDÉLYEZETT stratégiák nevei (több is lehet).

    Forrás: `pairs.<symbol>.strategies` névlista. Ha hiányzik/üres → az elsődleges
    stratégia (a jelenlegi, egy-stratégiás viselkedés bitazonos marad)."""
    pc = (cfg.get("pairs", {}) or {}).get(symbol, {}) or {}
    names = pc.get("strategies")
    if not names:
        return [default_strategy_name(cfg)]
    # Csak érvényes/ismert neveket adunk vissza, a config sorrendjében (egyediesítve).
    out, seen = [], set()
    for n in names:
        if n and n not in seen:
            out.append(n)
            seen.add(n)
    return out or [default_strategy_name(cfg)]


def strategies_for(cfg: dict, symbol: str) -> list[Strategy]:
    """Az instrumentumon engedélyezett stratégia-példányok (az elsődleges az első)."""
    return [get_strategy_by_name(n) for n in enabled_strategy_names(cfg, symbol)]


__all__ = [
    "Strategy", "Column", "CountdownColumn", "StrategyColumn", "MarkerColumn",
    "MarketData", "Cell", "Timeframe",
    "get_strategy", "get_strategy_by_name", "default_strategy_name",
    "enabled_strategy_names", "strategies_for", "registered_strategy_names",
    "available_strategy_names",
]
