"""
Per-stratégia megjelenítési kapcsolók (Vizualizáció / Kötés-réteg).

Eddig a két kapcsoló PÁR-szintű volt (`pairs.<sym>.viz_enabled` és `show_trades`,
a sorban a „V" és „K" gomb), így egy több-stratégiás páron nem lehetett külön
kikapcsolni az egyik stratégia rajzát. Ez a modul stratégiánkéntire bontja őket:

    "pairs": { "EURUSD": {
        "strategies":      ["wpr_sma", "ml_ai"],          # AKTÍV (változatlan)
        "strategy_viz":    {"wpr_sma": true,  "ml_ai": false},
        "strategy_trades": {"wpr_sma": true,  "ml_ai": false},
        "viz_enabled": true, "show_trades": false          # legacy visszaesés
    } }

Az AKTÍV stratégiák forrása VÁLTOZATLANUL a `strategies` névlista
(`strategy.enabled_strategy_names`) — ezt a modul nem duplikálja, hogy ne legyen
két igazságforrás arra, mi fut.

Visszafelé kompatibilitás: ha a stratégia nem szerepel a per-stratégia térképben,
a PÁR-szintű legacy kulcs dönt (`viz_enabled` / `show_trades`, alap: True). Egy
régi config.json tehát bitazonosan viselkedik, amíg a felhasználó hozzá nem nyúl
a táblázathoz. Nincs migráció — a legacy kulcsokat szándékosan nem töröljük.
"""

from __future__ import annotations

# (per-stratégia térkép kulcsa, legacy pár-szintű kulcs) tengelyenként.
VIZ    = ("strategy_viz", "viz_enabled")
TRADES = ("strategy_trades", "show_trades")


def _pair(cfg: dict, symbol: str) -> dict:
    pc = (cfg.get("pairs") or {}).get(symbol)
    return pc if isinstance(pc, dict) else {}


def _on(cfg: dict, symbol: str, strategy_name: str, axis: tuple) -> bool:
    """Egy tengely (VIZ/TRADES) állapota az adott pár+stratégia párosra."""
    per_key, legacy_key = axis
    pc  = _pair(cfg, symbol)
    per = pc.get(per_key)
    if isinstance(per, dict) and strategy_name in per:
        return bool(per[strategy_name])
    return bool(pc.get(legacy_key, True))     # legacy pár-szintű, alap: látszik


def viz_on(cfg: dict, symbol: str, strategy_name: str) -> bool:
    """Látszik-e ennek a stratégiának a VIZUALIZÁCIÓJA ezen az instrumentumon."""
    return _on(cfg, symbol, strategy_name, VIZ)


def trades_on(cfg: dict, symbol: str, strategy_name: str) -> bool:
    """Látszik-e ennek a stratégiának a JEL-REPLAY (kötés) rétege. A tényleges
    MT5-kötések ettől FÜGGETLENÜL mindig látszanak (ez a jel-replay kapcsolója)."""
    return _on(cfg, symbol, strategy_name, TRADES)


def any_viz_on(cfg: dict, symbol: str, strategy_names) -> bool:
    """Kell-e egyáltalán viz-t írni erre a szimbólumra: legalább egy stratégia
    rajza látszik-e. Ez a PÁR-szintű kapu (throttle / CLEAR) — ha egyik sem
    látszik, a motor meg sem nyitja az írási utat."""
    return any(viz_on(cfg, symbol, n) for n in strategy_names)


def set_on(cfg: dict, symbol: str, strategy_name: str, axis: tuple, value: bool):
    """Egy tengely beállítása per stratégia (a hívó menti a configot). A legacy
    pár-szintű kulcshoz NEM nyúl: az marad a térképben nem szereplő (pl. később
    hozzáadott) stratégiák visszaesése."""
    per_key, _ = axis
    pc = cfg.setdefault("pairs", {}).setdefault(symbol, {})
    per = pc.get(per_key)
    if not isinstance(per, dict):
        per = {}
        pc[per_key] = per
    per[strategy_name] = bool(value)


def prune(cfg: dict, symbol: str, known_names) -> None:
    """A per-stratégia térképekből kidobja az ismeretlen (törölt/kikapcsolt)
    stratégia-neveket, hogy a config ne gyűjtsön szemetet. Üres térképet töröl."""
    known = set(known_names)
    pc = _pair(cfg, symbol)
    for per_key, _ in (VIZ, TRADES):
        per = pc.get(per_key)
        if not isinstance(per, dict):
            continue
        for n in [n for n in per if n not in known]:
            per.pop(n, None)
        if not per:
            pc.pop(per_key, None)
