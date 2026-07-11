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


def get_strategy(cfg: dict) -> Strategy:
    """Az aktív stratégia példánya a config alapján.

    config.json:  "strategy": { "name": "wpr_sma" }   (alapértelmezett: wpr_sma)
    """
    name = (cfg.get("strategy", {}) or {}).get("name", "wpr_sma")
    if name == "wpr_sma":
        from strategy.wpr_sma import WprSmaStrategy
        return WprSmaStrategy()
    raise ValueError(f"Ismeretlen stratégia: {name!r}")


__all__ = [
    "Strategy", "Column", "CountdownColumn", "StrategyColumn", "MarkerColumn",
    "MarketData", "Cell", "Timeframe", "get_strategy",
]
