"""
Stratégia réteg.

A dashboard "váza" (megjelenítés, optimalizálás, futtatás, MT5 kapcsolat,
portfólió backteszt) stratégia-független. A konkrét stratégia ezen a
csomagon keresztül csatlakozik: deklarálja a saját oszlopait, kiszámítja a
megjelenítendő értékeket, kezeli a jelzéslogikát és megadja az optimalizálandó
paramétertartományt.

Új stratégia = egy új modul, ami a `Strategy` interfészt implementálja.
A `get_strategy()` adja vissza az aktívat (config-vezérelt).
"""

from strategy.base import (
    Strategy, Column, CountdownColumn, StrategyColumn, MarkerColumn,
    MarketData, Cell, Timeframe,
)


# A regisztrált (ismert) stratégiák nevei — a dashboard ezekből képez EGY-EGY
# jelölő-oszlopot (fejléc = stratégia neve). Új stratégia = egy név ide + egy ág a
# get_strategy_by_name-ben + egy modul a strategy/-ben.
_REGISTERED = ("wpr_sma", "ml_ai")


def registered_strategy_names() -> list[str]:
    """A dashboard-oszlopokhoz: az összes ismert stratégia neve (sorrendben)."""
    return list(_REGISTERED)


# A stratégia-példányok gyorsítótára (stratégia-nevenként EGY példány; a példány
# állapotmentes az élő jelzésállapoton kívül, amit páronként külön tartunk).
_INSTANCES: dict[str, Strategy] = {}


def get_strategy_by_name(name: str) -> Strategy:
    """Stratégia-példány NÉV alapján (a registry-ből, cache-elve).
    Új stratégia = egy új ág itt (és egy új modul a `strategy/`-ben)."""
    if name not in _INSTANCES:
        if name == "wpr_sma":
            from strategy.wpr_sma import WprSmaStrategy
            _INSTANCES[name] = WprSmaStrategy()
        elif name == "ml_ai":
            from strategy.ml_ai import MlAiStrategy
            _INSTANCES[name] = MlAiStrategy()
        else:
            raise ValueError(f"Ismeretlen stratégia: {name!r}")
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
