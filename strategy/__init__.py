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


def default_strategy_name(cfg: dict) -> str:
    """A config elsődleges/alapértelmezett stratégiája (config.json strategy.name)."""
    return (cfg.get("strategy", {}) or {}).get("name", "wpr_sma")


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
]
