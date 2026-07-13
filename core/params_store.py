"""
Optimalizált paraméterek tárolása — STRATÉGIA-HATÓKÖRŰ elrendezés.

Az optimalizált fájlok stratégiánkénti almappába kerülnek:
    data/optimized_params/<strategy>/<symbol>.json      (paraméterek + eredmény)
    data/optimized_params/<strategy>/<symbol>_trials.csv (összes trial)
    data/optimized_params/<strategy>/<symbol>_study.db   (optuna study)
    data/optimized_params/<strategy>/<symbol>_study.done (befejezés-marker)

Így több stratégia UGYANARRA a párra nem ütközik (a több-stratégiás dashboard
előfeltétele). Könnyű modul (nincs nehéz függősége: optuna/MT5), hogy a
live_trader és a backtest is használhassa a path-eket az optimizer import nélkül.

A path-helperek elfogadnak explicit `strategy` nevet; ha nincs, a modul-szintű
AKTÍV stratégiát használják, amit a belépési pontok (main/live/gui/backtest/
optimizer) a config alapján `set_active_strategy`-vel állítanak be.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
PARAMS_DIR = ROOT / "data" / "optimized_params"
PARAMS_DIR.mkdir(parents=True, exist_ok=True)

_ACTIVE_STRATEGY = "wpr_sma"


def set_active_strategy(name: str) -> None:
    """A path-helperek alapértelmezett stratégiája (ha a hívó nem ad explicitet)."""
    global _ACTIVE_STRATEGY
    if name:
        _ACTIVE_STRATEGY = name


def active_strategy() -> str:
    return _ACTIVE_STRATEGY


def _sname(strategy: str | None) -> str:
    return strategy or _ACTIVE_STRATEGY


def strategy_dir(strategy: str | None = None) -> Path:
    d = PARAMS_DIR / _sname(strategy)
    d.mkdir(parents=True, exist_ok=True)
    return d


def params_file(symbol: str, strategy: str | None = None) -> Path:
    return strategy_dir(strategy) / f"{symbol}.json"


def trials_file(symbol: str, strategy: str | None = None) -> Path:
    return strategy_dir(strategy) / f"{symbol}_trials.csv"


def study_db(symbol: str, strategy: str | None = None) -> Path:
    return strategy_dir(strategy) / f"{symbol}_study.db"


def done_marker(symbol: str, strategy: str | None = None) -> Path:
    return strategy_dir(strategy) / f"{symbol}_study.done"


def hours_file(symbol: str, strategy: str | None = None) -> Path:
    """A kereskedési órák (trade_hours) STRATÉGIA-hatókörű tárolója.

    Külön fájl (nem az optimalizált `{symbol}.json`-ban), hogy az optimalizáló
    újrafuttatása NE írja felül a kézzel beállított órákat."""
    return strategy_dir(strategy) / f"{symbol}_hours.json"


def load_trade_hours(symbol: str, strategy: str | None = None) -> list[int] | None:
    """A stratégia-hatókörű kereskedési órák listája (0..23), vagy None ha nincs
    ilyen fájl (ilyenkor a hívó a régi config.json szimbólum-szintű értékére, majd
    a sess-tartományra eshet vissza — lásd `resolve_trade_hours`)."""
    p = hours_file(symbol, strategy)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        th = data.get("trade_hours")
        if th is None:
            return None
        return [int(h) for h in th]
    except Exception as e:
        log.debug("trade_hours olvasás hiba (%s): %s", p.name, e)
        return None


def save_trade_hours(symbol: str, hours, strategy: str | None = None) -> None:
    """A stratégia-hatókörű kereskedési órák atomikus kiírása (temp→replace)."""
    p = hours_file(symbol, strategy)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"symbol": symbol, "strategy": _sname(strategy),
                   "trade_hours": [int(h) for h in hours]},
                  f, indent=2, ensure_ascii=False)
    tmp.replace(p)


def resolve_trade_hours(symbol: str, strategy: str | None = None,
                        legacy=None) -> list[int] | None:
    """A tényleges kereskedési órák FELOLDÁSA (olvasók közös logikája):
      1. stratégia-hatókörű `{symbol}_hours.json` (ha van) — EZ nyer;
      2. különben a `legacy` (a config.json `pairs.<sym>.trade_hours`, szimbólum-
         szintű — visszafelé kompatibilis, több stratégia közös alapja);
      3. None → a hívó a sess_start/sess_end tartományra esik vissza.

    Így egy még nem migrált párnál a régi közös óra érvényes, de amint egy
    stratégiánál MENTED az órákat, onnantól annál a stratégiánál a saját fájlja
    dönt (a többi stratégiáé érintetlen)."""
    hrs = load_trade_hours(symbol, strategy)
    if hrs is not None:
        return hrs
    return legacy


def migrate_flat_layout(strategy: str | None = None) -> int:
    """A RÉGI lapos elrendezés (fájlok közvetlenül a PARAMS_DIR-ben) átmozgatása a
    stratégia-almappába. Idempotens: csak a gyökérben lévő fájlt mozgatja, és csak
    ha az almappában még NINCS (nem ír felül). Visszaad: mozgatott fájlok száma."""
    dst_dir = strategy_dir(strategy)
    moved = 0
    try:
        for f in PARAMS_DIR.iterdir():
            if not f.is_file():
                continue
            target = dst_dir / f.name
            if target.exists():
                continue
            try:
                f.replace(target)
                moved += 1
            except Exception as e:
                log.debug("migráció kihagyva (%s): %s", f.name, e)
    except Exception as e:
        log.debug("params-migráció hiba: %s", e)
    if moved:
        log.info("Params-migráció: %d fájl → %s", moved, dst_dir)
    return moved
